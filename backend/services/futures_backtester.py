"""
Futures backtester — extends the native backtester with leverage, liquidation,
and short positions.

Key differences from spot:
  • Leverage amplifies P&L: pnl_pct = price_move_pct × leverage
  • Liquidation: if price moves 100/leverage % against you → full margin loss
  • Both LONG and SHORT positions supported
  • Funding fee: 0.03% per 8h applied to open positions (KuCoin default)
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Optional

import pandas as pd

import bisect

from backend.services.native_backtester import (
    add_indicators, load_futures_ohlcv, load_funding_history,
    _signal_miss_candle_short, _signal_miss_candle_long,
    _signal_macd_crossover, _signal_rsi_bollinger, _signal_ema_scalping,
    _guess_strategy,
)

# Fallback funding fee per 8-hour period if KuCoin's history endpoint
# returns no data for the range (e.g. very old contracts). The real
# applied rate comes from /api/v1/contract/funding-rates per settlement.
FUNDING_RATE_FALLBACK = 0.0003   # 0.03%
CANDLES_PER_8H = {"1m": 480, "5m": 96, "15m": 32, "30m": 16, "1h": 8, "4h": 2}


def _funding_rate_for_ts(funding_sorted: list[tuple[int, float]],
                         bar_ts_secs: int) -> float:
    """Binary-search the most recent funding rate at or before `bar_ts_secs`.

    Funding history is sorted by timepoint; we want the rate that was last
    settled before this bar — that's what would have actually been charged
    on a position open at this moment.
    """
    if not funding_sorted:
        return FUNDING_RATE_FALLBACK
    # `bisect_right` finds first index where ts > bar_ts; we want the one
    # right before that.
    timestamps = [t for (t, _) in funding_sorted]
    idx = bisect.bisect_right(timestamps, bar_ts_secs) - 1
    if idx < 0:
        return FUNDING_RATE_FALLBACK
    return funding_sorted[idx][1]


def _calc_liquidation(entry: float, direction: str, leverage: int) -> float:
    """Return the liquidation price (simplified isolated margin)."""
    mm = 0.005   # 0.5% maintenance margin
    if direction == "long":
        return round(entry * (1 - 1.0 / leverage + mm), 6)
    else:
        return round(entry * (1 + 1.0 / leverage - mm), 6)


COMMISSION_RATE = 0.0   # No commission (user preference — pure P&L accuracy)


def run_futures_backtest(
    strategy_name: str,
    pairs: list[str],
    timeframe: str,
    timerange: str,
    leverage: int = 10,
    starting_balance: float = 1000.0,
    stoploss_pct: float = 3.0,        # % e.g. 3 → -3%
    take_profit_pct: float = 1.5,     # % e.g. 1.5 → +1.5%
    risk_per_trade: float = 0.05,     # fraction of balance used as margin per trade
    generated_code: str | None = None,  # user's IStrategy Python class (Freqtrade-style)
) -> dict:
    """
    Run a leveraged futures backtest matching TradingView's methodology:

    TradingView parity fixes applied:
      1. Commission: 0.05% per side (entry + exit) on position value
      2. Entry timing: signal fires at bar[i] close → entry at bar[i+1] OPEN
         (matches TradingView's default calc_on_every_tick=false behaviour)
      3. SL/TP same-bar resolution: if both hit in same candle, use bar open
         to determine which was hit first (TradingView default logic)
      4. Funding fee: 0.03% per 8h on position value (KuCoin standard)

    Returns a dict matching the shape expected by the frontend results component.
    """
    # ── Parse timerange ───────────────────────────────────────────────────
    try:
        parts = timerange.split("-")
        start_ts = int(datetime(int(parts[0][:4]), int(parts[0][4:6]),
                                int(parts[0][6:8])).timestamp())
        end_ts   = int(datetime(int(parts[1][:4]), int(parts[1][4:6]),
                                int(parts[1][6:8])).timestamp())
    except Exception:
        return {"error": f"Invalid timerange '{timerange}'. Use YYYYMMDD-YYYYMMDD."}

    # ── Pick the signal function for this run ──────────────────────────
    # Priority:
    #   1. If the user supplied generated_code (their authored IStrategy
    #      class), exec it and read enter_long/enter_short signal columns.
    #      This is what runs for ALL strategies the user creates via the
    #      Strategy Editor — their actual code, not a name-match heuristic.
    #   2. Otherwise (built-in template names like "SMCStrategyTV") fall
    #      back to _guess_strategy which maps the name to one of the
    #      hardcoded Python signal functions in native_backtester.
    use_user_strategy = bool(generated_code and generated_code.strip())
    user_strategy_error: str | None = None
    signal_fn = None
    if not use_user_strategy:
        signal_fn = _guess_strategy(strategy_name)
    all_trades: list[dict] = []
    balance = starting_balance
    candles_per_8h = CANDLES_PER_8H.get(timeframe, 32)
    # Sanity-check tallies surfaced in the response so the user can see if
    # the kline range was incomplete (KuCoin sometimes has gaps on older
    # data) and the funding history loaded as expected.
    data_diagnostics: dict[str, dict] = {}

    for pair in pairs:
        # ── Load FUTURES OHLCV (not spot — see comment in native_backtester) ─
        try:
            df = load_futures_ohlcv(pair, timeframe, start_ts, end_ts)
        except Exception as e:
            return {"error": f"Futures data download failed for {pair}: {e}"}
        # ── Load real funding rates from KuCoin (replaces hardcoded 0.03%) ─
        funding_sorted = load_funding_history(pair, start_ts, end_ts)
        # Per-pair coverage diagnostics
        tf_secs_per_bar = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                           "1h": 3600, "4h": 14400, "1d": 86400}.get(timeframe, 900)
        expected_bars = max(1, (end_ts - start_ts) // tf_secs_per_bar)
        data_diagnostics[pair] = {
            "candles_loaded":    len(df),
            "candles_expected":  int(expected_bars),
            "coverage_pct":      round(100.0 * len(df) / expected_bars, 1),
            "funding_records":   len(funding_sorted),
            "funding_source":    "kucoin_history" if funding_sorted else "fallback_0.03%",
            "signal_source":     "user_strategy" if use_user_strategy else f"builtin:{strategy_name}",
        }
        df = add_indicators(df)

        # ── User-strategy path: exec their generated_code and pre-populate
        # enter_long / enter_short signal columns on the dataframe ─────
        if use_user_strategy:
            try:
                from backend.services.strategy_runner import (
                    evaluate_strategy, make_signal_fn_from_df,
                )
                df = evaluate_strategy(generated_code, df)
                signal_fn = make_signal_fn_from_df(
                    df, leverage, stoploss_pct, take_profit_pct,
                )
                # Surface the count of fired signals in diagnostics so the
                # user can tell at a glance whether their strategy actually
                # produces entries on this data.
                el_count = int((df.get("enter_long", 0) == 1).sum()) if "enter_long" in df.columns else 0
                es_count = int((df.get("enter_short", 0) == 1).sum()) if "enter_short" in df.columns else 0
                data_diagnostics[pair]["entry_signals_long"]  = el_count
                data_diagnostics[pair]["entry_signals_short"] = es_count
                # Edge count (0→1 transitions) = unique trade opportunities,
                # which is what TradingView's strategy.entry() actually acts
                # on. A signal staying True for 20 bars is ONE entry, not 20.
                # The diff trick: shift by 1 and count rows where prev=0, cur=1.
                if "enter_long" in df.columns:
                    el = df["enter_long"].fillna(0).astype(int)
                    el_edges = int(((el == 1) & (el.shift(1).fillna(0) == 0)).sum())
                else:
                    el_edges = 0
                if "enter_short" in df.columns:
                    es = df["enter_short"].fillna(0).astype(int)
                    es_edges = int(((es == 1) & (es.shift(1).fillna(0) == 0)).sum())
                else:
                    es_edges = 0
                data_diagnostics[pair]["entry_clusters_long"]  = el_edges
                data_diagnostics[pair]["entry_clusters_short"] = es_edges
                # Echo back what methods the user's class defines so the UI
                # can show them when 0 signals fire (helps debug "why aren't
                # my entries triggering?").
                data_diagnostics[pair]["strategy_class"]   = df.attrs.get("strategy_class")
                data_diagnostics[pair]["strategy_methods"] = df.attrs.get("strategy_methods", [])
                data_diagnostics[pair]["signal_columns"]   = df.attrs.get("signal_columns", [])
                # When zero entries fire, include the first 800 chars of the
                # user's generated code in the diagnostics so it's possible
                # to see at a glance whether the strategy actually has
                # populate_entry_trend / produces enter_long signals.
                if (el_count == 0 and es_count == 0):
                    snippet = (generated_code or "").strip()
                    if len(snippet) > 800:
                        snippet = snippet[:800] + "\n... (truncated)"
                    data_diagnostics[pair]["code_preview"] = snippet
            except Exception as e:
                # Two sub-cases:
                #
                #  a) User's code has populate_* hooks but raised at runtime.
                #     → real error, show in red.
                #
                #  b) User's code is a plain Python class without populate_*
                #     hooks (legacy strategies authored as Pine Script ports
                #     for the name-matched signal-pattern path). The runner
                #     deliberately raises RuntimeError with "has none of the
                #     populate_* hooks" so we fall back gracefully here.
                #     → expected behaviour, show in neutral colour.
                user_strategy_error = str(e)
                is_intended_fallback = "populate_* hooks" in user_strategy_error
                tag = "name-match" if is_intended_fallback else "user code failed"
                data_diagnostics[pair]["signal_source"]   = (
                    f"builtin:{strategy_name} ({tag})"
                )
                data_diagnostics[pair]["user_code_error"] = user_strategy_error
                data_diagnostics[pair]["fallback_intended"] = is_intended_fallback
                signal_fn = _guess_strategy(strategy_name)

        in_trade      = False
        pending_entry = None     # (direction, entry_px, sl, tp, liq, margin) from previous bar
        entry_price   = sl = tp = liq_price = None
        direction     = None
        entry_date    = None
        candles_held  = 0
        margin        = 0.0
        # Cooldown: minimum bars between trades.
        # SMCStrategyTV uses NO cooldown (matches TradingView behaviour exactly —
        # TV allows back-to-back trades when conditions re-trigger).
        # Other strategies use a 2-day cooldown to prevent signal noise.
        tf_secs_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                       "1h": 3600, "4h": 14400}
        tf_secs = tf_secs_map.get(timeframe, 900)
        # Cooldown policy:
        #   • User-strategy path: NO cooldown. The user's enter_long/enter_short
        #     columns are now edge-only (see strategy_runner.make_signal_fn_from_df).
        #     Every 0→1 transition is treated as its own entry signal — matching
        #     TradingView's strategy.entry() semantics. An artificial cooldown
        #     here would suppress legitimate back-to-back setups.
        #   • Built-in name-matched strategies: keep the legacy cooldown so
        #     existing backtests don't shift.
        if use_user_strategy:
            cooldown_bars = 0
        elif strategy_name in ("SMCStrategyTV",):
            cooldown_bars = 4
        else:
            cooldown_secs = 2 * 24 * 3600   # 2-day cooldown for other strategies
            cooldown_bars = max(1, int(cooldown_secs / tf_secs))
        cooldown_remain = 0
        # Track how many signal bars we skipped because we were already
        # in a trade — surfaced in diagnostics so the user can see the gap
        # between "26 signal bars" and "3 trades opened".
        skipped_in_trade   = 0
        skipped_cooldown   = 0
        trades_opened_long = 0
        trades_opened_short = 0

        n = len(df)
        for i in range(3, n):
            row  = df.iloc[i]
            bar_o = row["open"]
            lo, hi = row["low"], row["high"]

            # ── A. Execute pending entry at this bar's OPEN ───────────────
            # (TradingView: signal fires at bar[i-1] close → entry at bar[i] open)
            if pending_entry is not None and not in_trade:
                direction, entry_price, sl, tp, liq_price, margin, use_signal_sltp = pending_entry
                pending_entry = None
                # Use actual open price as fill (matches TradingView's next-bar-open fill)
                actual_fill = bar_o

                if use_signal_sltp:
                    # SMCStrategyTV: keep structural SL/TP from the signal as-is.
                    # SL/TP are absolute price levels (swing-based), not relative
                    # to entry price — keep them even though fill is at next bar open.
                    entry_price = actual_fill
                    # sl and tp already set from signal
                else:
                    # Fixed-% strategies: recalculate SL/TP from actual fill price
                    entry_price = actual_fill
                    sl_dist = abs(entry_price * stoploss_pct / 100)
                    tp_dist = abs(entry_price * take_profit_pct / 100)
                    if direction == "long":
                        sl = entry_price - sl_dist
                        tp = entry_price + tp_dist
                    else:
                        sl = entry_price + sl_dist
                        tp = entry_price - tp_dist

                liq_price = _calc_liquidation(entry_price, direction, leverage)
                entry_date   = row["date"]
                candles_held = 0
                in_trade     = True
                # Entry commission: 0.05% of position value
                pos_value     = margin * leverage
                entry_commission = pos_value * COMMISSION_RATE
                balance      -= entry_commission

            # ── B. Manage open position ───────────────────────────────────
            if in_trade:
                candles_held += 1

                # Funding fee every 8h (on position value = margin × leverage).
                # Use the REAL historical funding rate KuCoin would have
                # applied at this bar's timestamp — not a constant. Falls
                # back to 0.03% only if KuCoin's history endpoint returned
                # nothing for this range.
                funding_cost = 0.0
                if candles_held % candles_per_8h == 0:
                    pos_value     = margin * leverage
                    bar_ts_secs   = int(row["date"].timestamp())
                    applied_rate  = _funding_rate_for_ts(funding_sorted, bar_ts_secs)
                    # Funding charged on longs when rate>0, credited when rate<0
                    # (and vice-versa for shorts). Backtest from holder side:
                    signed_rate   = applied_rate if direction == "long" else -applied_rate
                    funding_cost  = pos_value * signed_rate

                # Check liquidation first (instant full loss)
                liquidated = False
                if direction == "long" and lo <= liq_price:
                    exit_p     = liq_price
                    pnl_abs    = -margin
                    liquidated = True
                elif direction == "short" and hi >= liq_price:
                    exit_p     = liq_price
                    pnl_abs    = -margin
                    liquidated = True

                if not liquidated:
                    # SL/TP resolution — TradingView logic:
                    # If both SL and TP are hit in the same bar, check bar open
                    # to determine which was crossed first.
                    exited   = False
                    exit_rsn = ""
                    if direction == "long":
                        sl_hit = lo <= sl
                        tp_hit = hi >= tp
                        if sl_hit and tp_hit:
                            # Use bar open to decide order
                            if abs(bar_o - tp) < abs(bar_o - sl):
                                exit_p = tp; exit_rsn = "take_profit"  # TP closer to open → TP first
                            else:
                                exit_p = sl; exit_rsn = "stop_loss"
                            exited = True
                        elif sl_hit:
                            exit_p = sl; exit_rsn = "stop_loss"; exited = True
                        elif tp_hit:
                            exit_p = tp; exit_rsn = "take_profit"; exited = True
                    else:  # short
                        sl_hit = hi >= sl
                        tp_hit = lo <= tp
                        if sl_hit and tp_hit:
                            if abs(bar_o - tp) < abs(bar_o - sl):
                                exit_p = tp; exit_rsn = "take_profit"
                            else:
                                exit_p = sl; exit_rsn = "stop_loss"
                            exited = True
                        elif sl_hit:
                            exit_p = sl; exit_rsn = "stop_loss"; exited = True
                        elif tp_hit:
                            exit_p = tp; exit_rsn = "take_profit"; exited = True

                    if not exited:
                        # Did the strategy emit a NEW entry signal this bar?
                        # If so we close the current trade at this bar's close
                        # (TradingView-style "new signal flips position") so
                        # every signal edge actually results in a trade.
                        # Without this, signals that fire during an open
                        # position get silently dropped by the `continue`
                        # below — which is exactly what was causing
                        # "26 long edges → only 3 long trades".
                        peek = signal_fn(df, i) if use_user_strategy else None
                        if peek is not None:
                            exit_p   = float(row["close"])
                            exit_rsn = "new_signal"
                            exited   = True
                        else:
                            balance -= funding_cost
                            continue

                    # Compute leveraged P&L
                    if direction == "long":
                        price_move_pct = (exit_p - entry_price) / entry_price
                    else:
                        price_move_pct = (entry_price - exit_p) / entry_price

                    leveraged_pnl_pct = price_move_pct * leverage
                    pos_value         = margin * leverage
                    exit_commission   = pos_value * COMMISSION_RATE
                    pnl_abs = margin * leveraged_pnl_pct - funding_cost - exit_commission
                    pnl_abs = max(pnl_abs, -margin)   # cap loss at full margin

                balance += pnl_abs
                balance  = max(balance, 0)

                profit_pct = (pnl_abs / margin * 100) if margin > 0 else 0

                all_trades.append({
                    "pair":        pair,
                    "direction":   direction,
                    "leverage":    leverage,
                    "open_date":   str(entry_date),
                    "close_date":  str(row["date"]),
                    "entry":       round(float(entry_price), 4),
                    "open_rate":   round(float(entry_price), 4),
                    "close_rate":  round(float(exit_p), 4),
                    "sl_price":    round(float(sl), 4),
                    "tp_price":    round(float(tp), 4),
                    "liq_price":   round(float(liq_price), 4),
                    "margin":      round(float(margin), 4),
                    "profit_pct":  round(float(profit_pct), 3),
                    "profit_abs":  round(float(pnl_abs), 4),
                    "exit_reason": "liquidated" if liquidated else exit_rsn,
                    "balance":     round(float(balance), 2),
                    "candles_held": candles_held,
                })
                in_trade = False
                cooldown_remain = cooldown_bars   # start cooldown after each trade

            # Decrement cooldown each bar
            if cooldown_remain > 0 and not in_trade:
                cooldown_remain -= 1

            # ── C. Check for new entry signal (only when flat + cooldown elapsed) ──
            # Track WHY signals are skipped for diagnostics. We call signal_fn
            # unconditionally so we can count "would have entered but blocked";
            # the cost is negligible (it's just an array lookup).
            sig_peek = signal_fn(df, i) if i >= 3 else None
            if sig_peek is not None:
                if in_trade or pending_entry is not None:
                    skipped_in_trade += 1
                elif cooldown_remain > 0:
                    skipped_cooldown += 1

            if not in_trade and pending_entry is None and cooldown_remain == 0:
                if balance <= 0:
                    break
                sig = sig_peek
                if sig:
                    sig_entry, sl_raw, tp_raw, sig_dir = sig
                    if sig_dir == "long":
                        trades_opened_long += 1
                    else:
                        trades_opened_short += 1

                    # SMCStrategyTV uses structural (dynamic) SL/TP from signal.
                    # All other strategies use the user-defined fixed SL/TP %.
                    if strategy_name in ("SMCStrategyTV",):
                        sig_sl = sl_raw
                        sig_tp = tp_raw
                    else:
                        # Override with user-defined SL/TP %
                        sl_dist = sig_entry * stoploss_pct / 100
                        tp_dist = sig_entry * take_profit_pct / 100
                        if sig_dir == "long":
                            sig_sl = sig_entry - sl_dist
                            sig_tp = sig_entry + tp_dist
                        else:
                            sig_sl = sig_entry + sl_dist
                            sig_tp = sig_entry - tp_dist

                    sig_liq = _calc_liquidation(sig_entry, sig_dir, leverage)
                    sig_margin = balance * risk_per_trade
                    use_signal_sltp = strategy_name in ("SMCStrategyTV",)
                    # Queue entry for execution at NEXT bar's open
                    pending_entry = (sig_dir, sig_entry, sig_sl, sig_tp, sig_liq, sig_margin, use_signal_sltp)

        # End of per-pair bar loop — write per-pair signal-disposition counts
        # so the UI can show the breakdown of "signal bars → clusters → trades
        # opened → skipped because in-trade / cooldown". Without this, the
        # raw "26 long / 28 short" figure looks like a bug.
        data_diagnostics[pair]["trades_opened_long"]   = trades_opened_long
        data_diagnostics[pair]["trades_opened_short"]  = trades_opened_short
        data_diagnostics[pair]["signals_skipped_in_trade"] = skipped_in_trade
        data_diagnostics[pair]["signals_skipped_cooldown"] = skipped_cooldown
        data_diagnostics[pair]["cooldown_bars"]        = cooldown_bars

    # ── Compute aggregate metrics ─────────────────────────────────────────
    if not all_trades:
        return {
            "metrics": {
                "total_trades":    0,
                "winning_trades":  0,
                "losing_trades":   0,
                "liquidations":    0,
                "long_trades":     0,
                "short_trades":    0,
                "win_rate":        0.0,
                "total_profit_pct": 0.0,
                "total_profit_abs": 0.0,
                "final_balance":   starting_balance,
                "max_drawdown":    0.0,
                "avg_leverage_pnl": 0.0,
                "leverage":        leverage,
            },
            "trades":  [],
            "equity_curve": [{"date": "", "balance": starting_balance}],
            "data_quality": data_diagnostics,
            "user_strategy_error": user_strategy_error,
        }

    wins         = sum(1 for t in all_trades if t["profit_abs"] > 0)
    losses       = len(all_trades) - wins
    liquidations = sum(1 for t in all_trades if t["exit_reason"] == "liquidated")
    longs        = sum(1 for t in all_trades if t["direction"] == "long")
    shorts       = sum(1 for t in all_trades if t["direction"] == "short")
    win_rate     = wins / len(all_trades) if all_trades else 0

    total_profit_abs = balance - starting_balance
    total_profit_pct = (total_profit_abs / starting_balance * 100) if starting_balance else 0

    # Max drawdown from equity curve
    balances = [starting_balance] + [t["balance"] for t in all_trades]
    peak = starting_balance
    max_dd = 0.0
    for b in balances:
        if b > peak:
            peak = b
        dd = (peak - b) / peak if peak > 0 else 0
        if dd > max_dd:
            max_dd = dd

    avg_pnl = sum(t["profit_pct"] for t in all_trades) / len(all_trades)

    equity_curve = [{"date": t["close_date"], "balance": t["balance"]} for t in all_trades]

    return {
        "metrics": {
            "total_trades":     len(all_trades),
            "winning_trades":   wins,
            "losing_trades":    losses,
            "liquidations":     liquidations,
            "long_trades":      longs,
            "short_trades":     shorts,
            "win_rate":         round(win_rate, 4),
            "total_profit_pct": round(total_profit_pct, 2),
            "total_profit_abs": round(total_profit_abs, 4),
            "final_balance":    round(balance, 2),
            "max_drawdown":     round(max_dd * 100, 2),
            "avg_leverage_pnl": round(avg_pnl, 3),
            "leverage":         leverage,
            "starting_balance": starting_balance,
        },
        "trades":              all_trades,
        "equity_curve":        equity_curve,
        # Per-pair data-coverage report so the user can spot incomplete
        # backtests at a glance (e.g. KuCoin returned only 60% of expected
        # candles or no funding history was found for the range).
        "data_quality":        data_diagnostics,
        # If the user's strategy code errored, surface it here. The
        # backtest still completes with the name-match fallback so the
        # user always sees a number, but with a clear warning of why
        # their custom code wasn't used.
        "user_strategy_error": user_strategy_error,
    }
