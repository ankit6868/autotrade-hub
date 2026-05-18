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

# ── Production-grade market-realism constants ─────────────────────────────
#
# Real KuCoin futures retail (VIP0) fee schedule, published at
# https://www.kucoin.com/vip/level. These ARE deducted from the simulated
# balance — backtests are only meaningful if they include the same costs
# the strategy will pay in production. Treat the result as a realistic
# net-of-fees P&L, not a hypothetical gross figure.
#
# Application rules per fill type:
#   - ENTRY        : taker (strategy entries are market orders by default)
#   - STOP_LOSS    : taker (stop triggers market sell on KuCoin futures)
#   - LIQUIDATION  : taker (forced market liquidation)
#   - TAKE_PROFIT  : maker (TP set as a limit order, fills passively)
#
# This is conservative — a strategy that places limit entries (post-only)
# would pay maker on entry too, slightly improving net P&L. We assume
# market entries because that's what most strategy.entry() calls produce.

# KuCoin futures fee tiers (public schedule, retail VIP0):
KUCOIN_TAKER_FEE = 0.0006        # 0.06% — market orders, SL, liquidation
KUCOIN_MAKER_FEE = 0.0002        # 0.02% — limit fills (TP)

# Slippage in basis points (1bp = 0.01%). Applied AGAINST the position on
# exits. Stops typically fill worse than the trigger price; take-profits
# usually fill at or near the limit (favourable book) so we apply less.
SLIPPAGE_BPS_STOP  = 5    # 5 bps = 0.05% adverse slippage on stop_loss
SLIPPAGE_BPS_TP    = 2    # 2 bps = 0.02% slippage on take_profit
SLIPPAGE_BPS_LIQ   = 15   # 15 bps on liquidation — books are typically thin
SLIPPAGE_BPS_ENTRY = 2    # 2 bps on entry (market fill)
SLIPPAGE_BPS_FLIP  = 5    # 5 bps when force-closing on new signal (market exit)

# Fallback funding fee per settlement if KuCoin's history endpoint
# returns no data for the range. Real applied rate comes from
# /api/v1/contract/funding-rates per settlement.
FUNDING_RATE_FALLBACK = 0.0003   # 0.03%

# KuCoin funding settlements happen at fixed UTC clock times, not at
# arbitrary 8h intervals from trade open. We check these for each open
# position bar-by-bar and apply funding when a settlement falls inside
# the bar's window.
FUNDING_SETTLEMENT_HOURS_UTC = (0, 8, 16)   # 00:00, 08:00, 16:00 UTC


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


def _funding_settlements_in_window(window_start: int, window_end: int) -> list[int]:
    """Return UTC timestamps of any funding settlements occurring strictly
    inside (window_start, window_end] — used to charge funding on the bars
    that actually contain a 00/08/16 UTC settlement, instead of every Nth
    bar from trade open (which is what the legacy code did and which can
    mis-charge by up to 8 hours depending on entry time).
    """
    if window_end <= window_start:
        return []
    # Walk forward in 1h chunks from the first hour after window_start
    # until we pass window_end. 8 candidate hours per day → cheap.
    out = []
    start_dt = datetime.utcfromtimestamp(window_start)
    # Round up to next hour
    next_hour_ts = (window_start // 3600 + 1) * 3600
    cur = next_hour_ts
    while cur <= window_end:
        hour = datetime.utcfromtimestamp(cur).hour
        if hour in FUNDING_SETTLEMENT_HOURS_UTC:
            out.append(cur)
        cur += 3600
    return out


# Maintenance-margin schedule for liquidation. KuCoin uses a tiered
# system where larger positions have higher maintenance margin. For the
# starting balances this app supports ($100–$10k paper), the smallest
# tier (0.5%) is correct. Documented here so a future bump to position-
# sizing tiers is one place to change.
def _maintenance_margin_for_notional(notional_usdt: float) -> float:
    """Return the maintenance margin fraction for a given notional size."""
    # KuCoin BTC perpetual tier schedule (truncated; matches retail sizes):
    if   notional_usdt < 50_000:    return 0.005    # 0.5%
    elif notional_usdt < 250_000:   return 0.01     # 1%
    elif notional_usdt < 1_000_000: return 0.025    # 2.5%
    else:                           return 0.05     # 5%


def _calc_liquidation(entry: float, direction: str, leverage: int,
                      notional_usdt: float = 0.0) -> float:
    """Return the liquidation price using tiered maintenance margin."""
    mm = _maintenance_margin_for_notional(notional_usdt)
    if direction == "long":
        return round(entry * (1 - 1.0 / leverage + mm), 6)
    else:
        return round(entry * (1 + 1.0 / leverage - mm), 6)


def _apply_slippage(price: float, direction: str, side: str, bps: float) -> float:
    """Apply slippage to an exit price.

    Slippage is ADVERSE — it makes the trade worse than the trigger level.
    For a long exit: slippage moves the fill DOWN (sells lower).
    For a short exit: slippage moves the fill UP (buys higher).
    """
    if bps <= 0:
        return price
    factor = bps / 10_000.0
    if side == "exit":
        if direction == "long":
            return price * (1 - factor)   # sell lower
        else:
            return price * (1 + factor)   # buy higher
    else:   # entry
        if direction == "long":
            return price * (1 + factor)   # buy higher
        else:
            return price * (1 - factor)   # sell lower


COMMISSION_RATE = KUCOIN_TAKER_FEE  # Real KuCoin taker fee, deducted from
                                     # balance. Per-fill rate (taker vs maker)
                                     # is decided at the call-site based on
                                     # whether the exit was SL/liq (taker) or
                                     # TP (maker).


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
    force_slider_sltp: bool = False,    # when True, override strategy-defined structural SL/TP
                                        # with slider values. Used by auto-tune so each grid
                                        # cell actually tests a different SL/TP combo even
                                        # for strategies like SMCStrategyTV that normally
                                        # use their own structural levels.
    max_concurrent_positions: int = 1,  # TradingView-default pyramiding=0 → only 1 position
                                        # open at a time. Set to N to allow up to N positions
                                        # to stack (matches Pine's `pyramiding = N-1`). The
                                        # earlier "concurrent" mode (∞) inflated trade counts
                                        # because the same condition firing 4 bars in a row
                                        # would open 4 separate positions instead of 1.
    deduct_real_costs: bool = False,    # When True, funding fees + KuCoin taker/maker fees
                                        # are deducted from the simulated balance — gives a
                                        # production-grade net P&L. When False (default), the
                                        # P&L reflects ONLY price action × leverage — useful
                                        # for evaluating the strategy's edge in isolation,
                                        # without the friction of execution costs. Slippage
                                        # is always applied because it's a fill-quality
                                        # assumption (not a cost the exchange collects).
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
    # Sanity-check tallies surfaced in the response so the user can see if
    # the kline range was incomplete (KuCoin sometimes has gaps on older
    # data) and the funding history loaded as expected.
    data_diagnostics: dict[str, dict] = {}

    # Resolve buffer: extra candles fetched AFTER `end_ts` so positions
    # opened near the end of the backtest can still hit their SL/TP/
    # liquidation rather than being excluded as "still open". Capped at
    # 30 days so we don't blow up downloads on small backtests.
    RESOLVE_BUFFER_SECS = 30 * 24 * 3600

    for pair in pairs:
        tf_secs_per_bar = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                           "1h": 3600, "4h": 14400, "1d": 86400}.get(timeframe, 900)
        # ── Load FUTURES OHLCV (not spot — see comment in native_backtester) ─
        # Extended range = user's window + resolve buffer.
        fetch_end_ts = end_ts + RESOLVE_BUFFER_SECS
        try:
            df = load_futures_ohlcv(pair, timeframe, start_ts, fetch_end_ts)
        except Exception as e:
            return {"error": f"Futures data download failed for {pair}: {e}"}
        # ── Load real funding rates over the same extended range ─────────
        funding_sorted = load_funding_history(pair, start_ts, fetch_end_ts)
        # Per-pair coverage diagnostics — measured against the USER'S
        # requested window, not the extended fetch. Buffer bars are an
        # implementation detail; the user only cares whether their
        # requested period is well-covered.
        in_window_mask = df["date"].astype("int64") // 10**9 <= end_ts
        in_window_count = int(in_window_mask.sum())
        expected_bars = max(1, (end_ts - start_ts) // tf_secs_per_bar)
        # Index of the last bar inside the user's window — beyond this
        # the main loop only manages open positions, no new entries.
        last_in_window_idx = in_window_count - 1
        data_diagnostics[pair] = {
            "candles_loaded":    in_window_count,
            "candles_expected":  int(expected_bars),
            "coverage_pct":      round(100.0 * in_window_count / expected_bars, 1),
            "funding_records":   len(funding_sorted),
            "funding_source":    "kucoin_history" if funding_sorted else "fallback_0.03%",
            "signal_source":     "user_strategy" if use_user_strategy else f"builtin:{strategy_name}",
            "resolve_buffer_bars": int(len(df) - in_window_count),
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
                # If the user's strategy class declares its OWN stoploss /
                # minimal_roi, prefer those over slider values. The slider
                # values usually come from stale DB column defaults; the
                # class is the source of truth for its own risk math. Without
                # this, a strategy authored for 1:3 RR gets backtested at
                # 1:0.5 RR (DB default 3% SL / 1.5% TP), which is
                # mathematically guaranteed to lose at any normal win rate.
                class_sl = df.attrs.get("class_stoploss_pct")
                class_tp = df.attrs.get("class_take_profit_pct")
                if class_sl is not None:
                    data_diagnostics[pair]["override_sl_from_class"] = (
                        f"{class_sl}% (slider was {stoploss_pct}%)"
                    )
                    stoploss_pct = class_sl
                if class_tp is not None:
                    data_diagnostics[pair]["override_tp_from_class"] = (
                        f"{class_tp}% (slider was {take_profit_pct}%)"
                    )
                    take_profit_pct = class_tp
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

                # Even when code execution fails, still try to extract the
                # strategy's intended SL/TP from the source via regex so the
                # backtest doesn't run with stale DB-default values that
                # invert the RR ratio. Same sanity-bounds as the AST path:
                # values like -0.99 (no-stop) or 100 (placeholder ROI) are
                # IGNORED so we don't apply nonsense overrides.
                import re
                m_sl = re.search(
                    r"^\s*stoploss\s*=\s*(-?\d+(?:\.\d+)?)",
                    generated_code or "", re.MULTILINE,
                )
                if m_sl:
                    parsed_sl = abs(float(m_sl.group(1)))
                    if 0.001 <= parsed_sl <= 0.25:
                        data_diagnostics[pair]["override_sl_from_class"] = (
                            f"{parsed_sl*100}% (slider was {stoploss_pct}%, parsed from source)"
                        )
                        stoploss_pct = parsed_sl * 100
                    else:
                        data_diagnostics[pair]["class_stoploss_ignored"] = (
                            f"{parsed_sl*100:.1f}% — outside sane range, keeping slider {stoploss_pct}%"
                        )
                m_tp = re.search(
                    r"minimal_roi\s*=\s*\{\s*[\"']0[\"']\s*:\s*(\d+(?:\.\d+)?)",
                    generated_code or "",
                )
                if m_tp:
                    parsed_tp = float(m_tp.group(1))
                    if 0.001 <= parsed_tp <= 0.50:
                        data_diagnostics[pair]["override_tp_from_class"] = (
                            f"{parsed_tp*100}% (slider was {take_profit_pct}%, parsed from source)"
                        )
                        take_profit_pct = parsed_tp * 100
                    else:
                        data_diagnostics[pair]["class_take_profit_ignored"] = (
                            f"{parsed_tp*100:.1f}% — outside sane range, keeping slider {take_profit_pct}%"
                        )

        # ── Concurrent-position state ─────────────────────────────────
        # Every entry signal opens a NEW position alongside any already-
        # open ones (subject to free-margin checks). Each position runs
        # independently until it hits its own SL / TP / liquidation —
        # signals NEVER force-close existing positions. This matches
        # TradingView pyramiding>=N behaviour and the user's explicit
        # requirement that "every signal should be its own trade and
        # exit only on strategy rules, not on the next signal arriving".
        open_positions: list[dict] = []   # active trades
        pending_entries: list[tuple]= []  # to fill at next bar's open
        committed_margin = 0.0            # sum of margin across open_positions
        tf_secs_map = {"1m": 60, "5m": 300, "15m": 900, "30m": 1800,
                       "1h": 3600, "4h": 14400}
        tf_secs = tf_secs_map.get(timeframe, 900)
        # Per-pair counters for the data-quality panel.
        trades_opened_long  = 0
        trades_opened_short = 0
        skipped_no_margin   = 0   # signal fired but free margin < threshold
        # Legacy fields kept for response-shape compatibility.
        skipped_in_trade    = 0
        skipped_cooldown    = 0
        cooldown_bars       = 0   # not used in concurrent mode
        # How often the engine actually used the strategy's SL/TP vs falling
        # back to slider values — surfaced in diagnostics so the user can
        # see at a glance whether the slider is doing anything.
        sltp_from_signal    = 0
        sltp_from_slider    = 0

        n = len(df)
        for i in range(3, n):
            # Once we're past the user's window AND every position has
            # resolved, there's nothing left to simulate — exit early so
            # we don't burn through 30 days of buffer bars uselessly.
            if i > last_in_window_idx and not open_positions and not pending_entries:
                break
            row   = df.iloc[i]
            bar_o = row["open"]
            lo, hi = row["low"], row["high"]
            bar_ts_secs  = int(row["date"].timestamp())
            bar_start_ts = bar_ts_secs - tf_secs_per_bar

            # ── A. Open any pending entries at THIS bar's OPEN ────────────
            # (Signal at bar[i-1] close → fill at bar[i] open; matches TV.)
            new_pending: list[tuple] = []
            for pe in pending_entries:
                (sig_dir, _, sig_sl, sig_tp, _, sig_margin, use_signal_sltp,
                 signal_bar_idx, signal_bar_ts, sig_tp2) = pe

                # Free-margin check: don't open if we can't afford the margin.
                free_margin = balance - committed_margin
                if sig_margin > free_margin:
                    if free_margin > 1.0:
                        sig_margin = free_margin   # cap
                    else:
                        skipped_no_margin += 1
                        continue

                entry_price = _apply_slippage(bar_o, sig_dir, "entry",
                                              SLIPPAGE_BPS_ENTRY)

                if use_signal_sltp:
                    sl, tp = sig_sl, sig_tp
                else:
                    sl_dist = abs(entry_price * stoploss_pct / 100)
                    tp_dist = abs(entry_price * take_profit_pct / 100)
                    if sig_dir == "long":
                        sl = entry_price - sl_dist
                        tp = entry_price + tp_dist
                    else:
                        sl = entry_price + sl_dist
                        tp = entry_price - tp_dist

                pos_value = sig_margin * leverage
                liq_price = _calc_liquidation(entry_price, sig_dir, leverage, pos_value)

                # ── KuCoin entry fee (taker — market order) ─────────────
                # Tracked per-trade always; deducted from balance only when
                # the user enabled "realistic costs". In pure-strategy mode
                # the fee is shown as 0 in the trade record so the trade
                # P&L reflects price action only.
                entry_fee_real = pos_value * KUCOIN_TAKER_FEE
                if deduct_real_costs:
                    entry_fee = entry_fee_real
                    balance  -= entry_fee
                else:
                    entry_fee = 0.0
                committed_margin += sig_margin

                open_positions.append({
                    "direction":    sig_dir,
                    "entry_price":  entry_price,
                    "sl":           sl,
                    "tp":           tp,
                    "liq_price":    liq_price,
                    "margin":       sig_margin,
                    "entry_date":   row["date"],
                    "entry_bar_ts": bar_ts_secs,
                    "candles_held": 0,
                    "funding_paid":     0.0,
                    "slippage_paid":    abs(bar_o - entry_price) * (pos_value / max(bar_o, 1e-9)),
                    "fees_paid":        entry_fee,   # accumulates exit fee at close
                    # Signal-trace fields — let the user prove the chain
                    # signal-bar → fill-bar → exit-bar without ambiguity.
                    "signal_bar_index": signal_bar_idx,
                    "signal_bar_ts":    signal_bar_ts,
                    "entry_bar_index":  i,
                    "sltp_source":      "strategy" if use_signal_sltp else "slider",
                    # Multi-TP state. tp2 is None for single-TP strategies
                    # (everything currently in use except the new SMCStrategyTV
                    # institutional model). When set:
                    #   • tp1 fill closes tp1_close_pct of margin, moves SL
                    #     to entry (breakeven trail)
                    #   • tp2 fill closes the remainder
                    # remaining_pct is what fraction of the original margin
                    # is still in the trade; starts at 1.0.
                    "tp2":              sig_tp2,
                    "tp1_close_pct":    0.5 if sig_tp2 is not None else 1.0,
                    "remaining_pct":    1.0,
                    "tp1_hit":          False,
                    "partial_pnl":      0.0,
                    "partial_fees":     0.0,
                    "partial_slippage": 0.0,
                    "partial_exits":    [],   # for trade record / signal trace
                })
            pending_entries = new_pending

            # ── B. Manage every open position (independent SL/TP/liq) ─────
            still_open: list[dict] = []
            for pos in open_positions:
                pos["candles_held"] += 1
                direction    = pos["direction"]
                entry_price  = pos["entry_price"]
                sl, tp       = pos["sl"], pos["tp"]
                liq_price    = pos["liq_price"]
                margin       = pos["margin"]
                entry_date   = pos["entry_date"]

                # Funding settlements that fall inside this bar's time window.
                # Always computed (so the diagnostic field is populated even
                # in pure-strategy mode), but only subtracted from leg P&L
                # when the user enabled "realistic costs".
                funding_cost = 0.0
                window_lo = (max(bar_start_ts, pos["entry_bar_ts"])
                             if pos["candles_held"] == 1 else bar_start_ts)
                settlements = _funding_settlements_in_window(window_lo, bar_ts_secs)
                if settlements:
                    pos_value = margin * leverage
                    for settle_ts in settlements:
                        applied_rate = _funding_rate_for_ts(funding_sorted, settle_ts)
                        signed_rate  = applied_rate if direction == "long" else -applied_rate
                        funding_cost += pos_value * signed_rate
                    pos["funding_paid"] += funding_cost
                # In pure-strategy mode, zero-out funding_cost so it doesn't
                # affect P&L (it's still tracked for transparency).
                if not deduct_real_costs:
                    funding_cost_for_pnl = 0.0
                else:
                    funding_cost_for_pnl = funding_cost

                # ── Exit detection with optional multi-TP partial close ──
                # When pos["tp2"] is None: single-TP path — TP closes 100%.
                # When pos["tp2"] is set:
                #   1. TP1 hit (first time) → close tp1_close_pct of margin,
                #      move SL to entry (breakeven trail), keep position open.
                #   2. SL hit → close remaining_pct.
                #   3. TP2 hit → close remaining_pct.
                # Liquidation always closes everything.
                #
                # We process at most ONE partial event per bar — even if both
                # TP1 and TP2 hit in the same candle, TP1 fires first and
                # the remainder rolls to the next bar. That's pessimistic but
                # honest: without tick data we can't know the intra-bar order.
                liquidated   = False
                exit_slippage_bps = 0
                raw_exit_p   = None
                exit_rsn     = ""
                partial_event = False   # True when we close part but not all

                tp = pos["tp"]
                tp2 = pos.get("tp2")
                has_tp2 = tp2 is not None and not pos["tp1_hit"]

                if direction == "long" and lo <= liq_price:
                    raw_exit_p = liq_price
                    liquidated = True
                    exit_slippage_bps = SLIPPAGE_BPS_LIQ
                elif direction == "short" and hi >= liq_price:
                    raw_exit_p = liq_price
                    liquidated = True
                    exit_slippage_bps = SLIPPAGE_BPS_LIQ
                else:
                    # TP1 is only checkable BEFORE the first partial fires.
                    # Once tp1_hit=True, the runner targets TP2 (or SL@BE).
                    # This prevents TP1 from re-triggering on the remainder
                    # and producing fake "take_profit" labels when the
                    # multi-TP intent was never fulfilled.
                    if direction == "long":
                        sl_hit  = lo <= sl
                        tp1_hit = (not pos["tp1_hit"]) and (hi >= tp)
                        tp2_hit = (pos["tp1_hit"] and tp2 is not None and hi >= tp2)
                    else:
                        sl_hit  = hi >= sl
                        tp1_hit = (not pos["tp1_hit"]) and (lo <= tp)
                        tp2_hit = (pos["tp1_hit"] and tp2 is not None and lo <= tp2)

                    if has_tp2 and sl_hit and tp1_hit:
                        # Both hit same bar — use "closer to open" heuristic.
                        if abs(bar_o - tp) < abs(bar_o - sl):
                            raw_exit_p = tp; exit_rsn = "take_profit_1"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                            partial_event = True
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif has_tp2 and tp1_hit:
                        raw_exit_p = tp; exit_rsn = "take_profit_1"
                        exit_slippage_bps = SLIPPAGE_BPS_TP
                        partial_event = True
                    elif sl_hit and tp1_hit:
                        # Single-TP path: same-bar SL+TP heuristic.
                        if abs(bar_o - tp) < abs(bar_o - sl):
                            raw_exit_p = tp; exit_rsn = "take_profit"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp2_hit and sl_hit:
                        # Same-bar SL@BE and TP2: heuristic on bar open.
                        if abs(bar_o - tp2) < abs(bar_o - sl):
                            raw_exit_p = tp2; exit_rsn = "take_profit_2"
                            exit_slippage_bps = SLIPPAGE_BPS_TP
                        else:
                            raw_exit_p = sl; exit_rsn = "stop_loss"
                            exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp2_hit:
                        raw_exit_p = tp2; exit_rsn = "take_profit_2"
                        exit_slippage_bps = SLIPPAGE_BPS_TP
                    elif sl_hit:
                        raw_exit_p = sl; exit_rsn = "stop_loss"
                        exit_slippage_bps = SLIPPAGE_BPS_STOP
                    elif tp1_hit:
                        raw_exit_p = tp; exit_rsn = "take_profit"
                        exit_slippage_bps = SLIPPAGE_BPS_TP

                # Still running this bar → carry forward. Funding only hits
                # balance in realistic-costs mode.
                if raw_exit_p is None:
                    balance -= funding_cost_for_pnl
                    still_open.append(pos)
                    continue

                # ── Compute the leg P&L (full or partial) ────────────────
                exit_p = _apply_slippage(raw_exit_p, direction, "exit",
                                         exit_slippage_bps)
                # close_pct = fraction of the ORIGINAL margin being closed
                # on this event. For full exits and TP2/SL after partial,
                # this is whatever's left (remaining_pct).
                if partial_event:
                    close_pct = pos["tp1_close_pct"]
                else:
                    close_pct = pos["remaining_pct"]
                leg_margin = margin * close_pct
                leg_pos_value = leg_margin * leverage

                if liquidated:
                    leg_pnl = -leg_margin
                else:
                    if direction == "long":
                        price_move_pct = (exit_p - entry_price) / entry_price
                    else:
                        price_move_pct = (entry_price - exit_p) / entry_price
                    leveraged_pnl_pct = price_move_pct * leverage
                    # Funding only subtracts from P&L when realistic-costs
                    # mode is on (funding_cost_for_pnl is zeroed otherwise).
                    leg_pnl = leg_margin * leveraged_pnl_pct - funding_cost_for_pnl
                    leg_pnl = max(leg_pnl, -leg_margin)

                units = leg_pos_value / max(entry_price, 1e-9)
                leg_slippage = abs(raw_exit_p - exit_p) * units
                pos["slippage_paid"] += leg_slippage

                # KuCoin fees on this leg's notional — tracked always,
                # only deducted from balance in realistic-costs mode.
                if exit_rsn in ("take_profit", "take_profit_1", "take_profit_2"):
                    leg_fee_real = leg_pos_value * KUCOIN_MAKER_FEE
                else:
                    leg_fee_real = leg_pos_value * KUCOIN_TAKER_FEE
                if deduct_real_costs:
                    leg_fee = leg_fee_real
                    pos["fees_paid"] += leg_fee
                    balance -= leg_fee
                else:
                    leg_fee = 0.0

                # Record this leg.
                pos["partial_pnl"] += leg_pnl
                pos["partial_exits"].append({
                    "bar_index":  i,
                    "reason":     exit_rsn,
                    "price":      round(float(exit_p), 4),
                    "close_pct":  round(float(close_pct), 4),
                    "pnl":        round(float(leg_pnl), 4),
                })

                # Release this leg's margin, apply P&L to balance.
                committed_margin -= leg_margin
                balance += leg_pnl
                balance = max(balance, 0)

                if partial_event:
                    # Position still has remaining_pct left. Move SL to
                    # entry (breakeven trail — locks in zero downside on
                    # the rest), and continue.
                    pos["tp1_hit"] = True
                    pos["remaining_pct"] -= close_pct
                    pos["sl"] = entry_price
                    sl = entry_price
                    still_open.append(pos)
                    continue

                # Position fully closed — assemble the aggregate trade record.
                #
                # P&L bookkeeping (single source of truth):
                #   • leg_pnl is gross of leg-level fees, computed per-leg
                #   • pos["partial_pnl"] = SUM of leg_pnl values
                #   • pos["fees_paid"]    = entry_fee + SUM of leg exit fees
                #     (each pushed when the leg fires)
                #   • Net P&L the user sees = partial_pnl - fees_paid
                #     (funding is already inside partial_pnl via leg_pnl)
                net_pnl_abs = pos["partial_pnl"] - pos["fees_paid"]
                profit_pct  = (net_pnl_abs / margin * 100) if margin > 0 else 0

                # Weighted-average exit price across all legs — single
                # representative "close_rate" for the trade row, even when
                # there were 2-3 fills.
                total_close_pct = sum(leg["close_pct"] for leg in pos["partial_exits"])
                if total_close_pct > 0:
                    wavg_exit = sum(
                        leg["price"] * leg["close_pct"]
                        for leg in pos["partial_exits"]
                    ) / total_close_pct
                else:
                    wavg_exit = exit_p

                # Headline exit reason for the row.
                if len(pos["partial_exits"]) > 1:
                    reasons = [leg["reason"] for leg in pos["partial_exits"]]
                    if "take_profit_2" in reasons:
                        final_reason = "multi_tp_completed"
                    elif "stop_loss" in reasons:
                        final_reason = "tp1_then_stop"
                    else:
                        final_reason = reasons[-1]
                else:
                    final_reason = "liquidated" if liquidated else exit_rsn
                    if final_reason == "take_profit_1":
                        # Single-leg take-profit on a multi-TP-enabled trade
                        # would imply TP1 closed everything (close_pct=1.0),
                        # which only happens for single-TP setups. Normalise.
                        final_reason = "take_profit"
                all_trades.append({
                    "pair":        pair,
                    "direction":   direction,
                    "leverage":    leverage,
                    "open_date":   str(entry_date),
                    "close_date":  str(row["date"]),
                    "entry":       round(float(entry_price), 4),
                    "open_rate":   round(float(entry_price), 4),
                    "close_rate":  round(float(wavg_exit), 4),
                    "sl_price":    round(float(sl), 4),
                    "tp_price":    round(float(tp), 4),
                    "tp2_price":   round(float(pos["tp2"]), 4) if pos.get("tp2") else None,
                    "liq_price":   round(float(liq_price), 4),
                    "margin":      round(float(margin), 4),
                    "profit_pct":  round(float(profit_pct), 3),
                    "profit_abs":  round(float(net_pnl_abs), 4),     # NET of all costs
                    "exit_reason": final_reason,
                    "balance":     round(float(balance), 2),
                    "candles_held": pos["candles_held"],
                    "funding_paid":      round(float(pos["funding_paid"]),    4),
                    "slippage_paid":     round(float(pos["slippage_paid"]),   4),
                    "fees_paid":         round(float(pos["fees_paid"]),       4),  # real KuCoin
                                                                                    # fees DEDUCTED
                    "exit_slippage_bps": int(exit_slippage_bps),
                    # Signal-trace: prove this trade came from a real signal.
                    "signal_bar_index": pos["signal_bar_index"],
                    "entry_bar_index":  pos["entry_bar_index"],
                    "exit_bar_index":   i,
                    "sltp_source":      pos["sltp_source"],
                    # Multi-TP partial-close trace. Single-TP trades have
                    # exactly one entry in this list with close_pct=1.0.
                    # Multi-TP trades typically have 2 entries (TP1 then
                    # SL-at-BE or TP2). Lets the user see the actual fill
                    # sequence behind a multi_tp_completed trade.
                    "partial_exits":    pos["partial_exits"],
                })
            open_positions = still_open

            # ── C. Check for new entry signal ─────────────────────────────
            # Buffer bars (past the user's end_ts) only manage existing
            # positions — no new entries — so the trade list reflects only
            # signals fired within the requested window.
            in_window = i <= last_in_window_idx
            sig = signal_fn(df, i) if (i >= 3 and in_window) else None
            if sig is not None:
                # Accept either 4-tuple (entry, sl, tp, dir) or 5-tuple
                # (entry, sl, tp1, tp2, dir). Multi-TP unlocks partial-close
                # behaviour in the engine: TP1 closes 50% and moves SL to
                # entry (breakeven trail), TP2 closes the remainder.
                if len(sig) == 5:
                    sig_entry, sl_raw, tp_raw, tp2_raw, sig_dir = sig
                else:
                    sig_entry, sl_raw, tp_raw, sig_dir = sig
                    tp2_raw = None
                if balance <= 0:
                    # Wiped out — no more trades possible.
                    continue

                # Pyramiding cap — matches TradingView's `pyramiding` setting.
                # Default 1 = single position at a time (most common). When a
                # strategy fires the same condition for several bars in a row,
                # the unlimited-concurrent mode would open 4 separate trades
                # with near-identical outcomes; the user reads those as "4
                # trades" when conceptually it's the same setup. The cap
                # collapses those into ONE trade — matches how you'd actually
                # trade by hand or via Pine Script default.
                #
                # We count BOTH already-open positions AND pending entries
                # that haven't filled yet, so a cluster of signals at the
                # same bar can't all sneak through together.
                open_in_dir = sum(1 for p in open_positions if p["direction"] == sig_dir)
                pending_in_dir = sum(1 for pe in pending_entries if pe[0] == sig_dir)
                total_in_dir = open_in_dir + pending_in_dir
                if total_in_dir >= max_concurrent_positions:
                    # The signal fired, but we already hold the max number of
                    # positions in this direction. Tracked under "in-trade".
                    skipped_in_trade += 1
                    continue

                free_margin = balance - committed_margin
                sig_margin = balance * risk_per_trade
                # Cap by free margin; skip if can't even commit $1.
                if sig_margin > free_margin:
                    if free_margin > 1.0:
                        sig_margin = free_margin
                    else:
                        skipped_no_margin += 1
                        continue

                if sig_dir == "long":
                    trades_opened_long += 1
                else:
                    trades_opened_short += 1

                # Every built-in signal function returns its OWN (entry, sl,
                # tp, dir). Those values are the strategy's design intent —
                # MissCandle ports use prev-candle high/low, SMC uses swing
                # pivots + 2R, MACD/RSI use fixed-% targets matched to the
                # signal's expected hold time. The previous behaviour (only
                # SMCStrategyTV was whitelisted) silently discarded the
                # SL/TP from every other strategy and forced slider values,
                # which is why a real-SMC backtest showed every trade
                # exiting at exactly slider_sl × leverage (e.g. -15.49%) —
                # totally inconsistent with what an SMC strategy actually does.
                #
                # New default: honour whatever the signal function returned,
                # IF the values look plausible (positive risk, capped at 25%
                # so a buggy signal can't blow up the run). The slider only
                # wins when:
                #   • the strategy is user-authored (no signal function — we
                #     don't reach this code path for those; signal_fn comes
                #     from make_signal_fn_from_df which already builds SL/TP
                #     from sliders), or
                #   • force_slider_sltp=True (auto-tune grid sweep).
                sig_tp2: float | None = None
                if force_slider_sltp:
                    sl_dist = sig_entry * stoploss_pct / 100
                    tp_dist = sig_entry * take_profit_pct / 100
                    if sig_dir == "long":
                        sig_sl = sig_entry - sl_dist
                        sig_tp = sig_entry + tp_dist
                    else:
                        sig_sl = sig_entry + sl_dist
                        sig_tp = sig_entry - tp_dist
                    use_signal_sltp = False
                    # Force-slider mode IGNORES multi-TP — user explicitly
                    # asked for fixed % SL/TP, so we use slider values only.
                    sig_tp2 = None
                else:
                    # Sanity-cap the strategy's SL distance: anything wider
                    # than 25% of entry price is almost certainly a bug
                    # (would imply 250% leveraged loss at 10x — instant
                    # liquidation) and we fall back to slider values rather
                    # than open the trade with garbage levels.
                    risk_dist = abs(sig_entry - sl_raw)
                    if risk_dist > 0 and risk_dist <= sig_entry * 0.25:
                        sig_sl, sig_tp = sl_raw, tp_raw
                        use_signal_sltp = True
                        # TP2 only honoured when (a) strategy returned it
                        # AND (b) it's further from entry than TP1 in the
                        # trade direction. We validated direction in
                        # make_signal_fn_from_df; defensive re-check here.
                        if tp2_raw is not None:
                            tp2_f = float(tp2_raw)
                            if sig_dir == "long" and tp2_f > sig_tp:
                                sig_tp2 = tp2_f
                            elif sig_dir == "short" and tp2_f < sig_tp:
                                sig_tp2 = tp2_f
                    else:
                        sl_dist = sig_entry * stoploss_pct / 100
                        tp_dist = sig_entry * take_profit_pct / 100
                        if sig_dir == "long":
                            sig_sl = sig_entry - sl_dist
                            sig_tp = sig_entry + tp_dist
                        else:
                            sig_sl = sig_entry + sl_dist
                            sig_tp = sig_entry - tp_dist
                        use_signal_sltp = False
                        sig_tp2 = None

                if use_signal_sltp:
                    sltp_from_signal += 1
                else:
                    sltp_from_slider += 1

                sig_liq = _calc_liquidation(sig_entry, sig_dir, leverage,
                                            sig_margin * leverage)
                # Carry the bar index where the signal fired AND the bar
                # timestamp through to the open position so the trade
                # record can prove the chain: "signal fired at bar X
                # (time T1) → entry filled at bar X+1 (time T2) → exit
                # at bar Y (time T3)". The user can match these against
                # the strategy's signal function to verify nothing fired
                # without a real condition.
                signal_bar_idx = i
                signal_bar_ts  = bar_ts_secs
                pending_entries.append(
                    (sig_dir, sig_entry, sig_sl, sig_tp, sig_liq,
                     sig_margin, use_signal_sltp, signal_bar_idx, signal_bar_ts,
                     sig_tp2)
                )

        # ── End of bar loop: handle leftover open positions ───────────────
        # Trades still open when the data window ends NEVER hit their
        # strategy-defined exit (SL/TP/liquidation), so their P&L is
        # unrealised — including them in the trade table would mix real
        # strategy outcomes with arbitrary mark-to-market snapshots and
        # distort win-rate / drawdown / avg P&L.
        # Correct behaviour: release the committed margin so balance is
        # honest, record the count for the diagnostics panel, but DON'T
        # add them to the trade list or affect realised P&L.
        trades_still_open_at_end = len(open_positions)
        unrealised_pnl_at_end    = 0.0
        if open_positions:
            last_row   = df.iloc[-1]
            last_close = float(last_row["close"])
            for pos in open_positions:
                direction   = pos["direction"]
                entry_price = pos["entry_price"]
                margin      = pos["margin"]
                # For partially-closed multi-TP trades, the remaining
                # margin is `margin × remaining_pct`. Mark-to-market
                # only on that remainder; the closed portion was already
                # released to balance when each TP fired.
                remaining_margin = margin * pos.get("remaining_pct", 1.0)
                if direction == "long":
                    move_pct = (last_close - entry_price) / entry_price
                else:
                    move_pct = (entry_price - last_close) / entry_price
                unrealised_pnl_at_end += max(
                    remaining_margin * move_pct * leverage, -remaining_margin
                )
                committed_margin -= remaining_margin
            open_positions = []

        # End of per-pair bar loop — write per-pair signal-disposition counts
        # so the UI can show the breakdown of "signal bars → clusters → trades
        # opened → skipped because in-trade / cooldown". Without this, the
        # raw "26 long / 28 short" figure looks like a bug.
        data_diagnostics[pair]["trades_opened_long"]   = trades_opened_long
        data_diagnostics[pair]["trades_opened_short"]  = trades_opened_short
        data_diagnostics[pair]["signals_skipped_in_trade"] = skipped_in_trade
        data_diagnostics[pair]["signals_skipped_cooldown"] = skipped_cooldown
        data_diagnostics[pair]["cooldown_bars"]        = cooldown_bars
        # Pyramiding / position-model diagnostics
        data_diagnostics[pair]["signals_skipped_no_margin"] = skipped_no_margin
        data_diagnostics[pair]["max_concurrent_positions"]  = max_concurrent_positions
        data_diagnostics[pair]["position_model"] = (
            "single" if max_concurrent_positions == 1
            else f"pyramiding_{max_concurrent_positions}"
        )
        data_diagnostics[pair]["trades_still_open_at_end"]  = trades_still_open_at_end
        data_diagnostics[pair]["unrealised_pnl_at_end"]     = round(unrealised_pnl_at_end, 4)
        # How the engine decided SL/TP per trade. When sltp_from_signal
        # dominates, the slider is functionally inert for this strategy —
        # the UI uses this to explain "your slider is for reference; this
        # strategy defines its own SL/TP per signal".
        data_diagnostics[pair]["sltp_from_signal"]          = sltp_from_signal
        data_diagnostics[pair]["sltp_from_slider"]          = sltp_from_slider

        # Effective SL / TP range across actual trades for this pair.
        # When the strategy returns structural levels, the slider value
        # tells the user nothing — they need to know "actually, your trades
        # ran SL between X% and Y% with avg Z%". This is what closes the
        # gap between "I set 1.5% SL" and "trade exited at -9.89%".
        pair_trades = [t for t in all_trades if t.get("pair") == pair]
        sl_dists = []
        tp_dists = []
        for t in pair_trades:
            op = float(t.get("open_rate") or 0)
            if op <= 0:
                continue
            sl_p = float(t.get("sl_price") or 0)
            tp_p = float(t.get("tp_price") or 0)
            if sl_p > 0:
                sl_dists.append(abs(sl_p - op) / op * 100)
            if tp_p > 0:
                tp_dists.append(abs(tp_p - op) / op * 100)
        if sl_dists:
            data_diagnostics[pair]["effective_sl_pct_avg"] = round(sum(sl_dists)/len(sl_dists), 3)
            data_diagnostics[pair]["effective_sl_pct_min"] = round(min(sl_dists), 3)
            data_diagnostics[pair]["effective_sl_pct_max"] = round(max(sl_dists), 3)
        if tp_dists:
            data_diagnostics[pair]["effective_tp_pct_avg"] = round(sum(tp_dists)/len(tp_dists), 3)
            data_diagnostics[pair]["effective_tp_pct_min"] = round(min(tp_dists), 3)
            data_diagnostics[pair]["effective_tp_pct_max"] = round(max(tp_dists), 3)

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

    # Aggregate cost transparency rows. ALL THREE are deducted from the
    # simulated balance (real-cost modelling — backtests are only useful
    # if they include what production would actually charge).
    total_funding   = sum(t.get("funding_paid",   0) for t in all_trades)
    total_slippage  = sum(t.get("slippage_paid",  0) for t in all_trades)
    total_fees      = sum(t.get("fees_paid",      0) for t in all_trades)

    # ── Math-check rails: break-even WR + expected-value per trade ────────
    # Breakeven WR = SL / (SL + TP). If actual WR is below this, the
    # strategy is MATHEMATICALLY guaranteed to lose money (before fees)
    # — no amount of code tweaking fixes that, it's arithmetic.
    #
    # For strategies whose engine uses *structural* SL/TP per trade
    # (SMCStrategyTV — pivot-based stops + 2R targets), the slider values
    # are NOT what trades actually used, so deriving breakeven from sliders
    # is misleading. We compute the realised average SL% / TP% from the
    # ACTUAL trade outcomes and use those for the verdict — that's the
    # arithmetic the user is really running.
    realised_sl_pct: float | None = None
    realised_tp_pct: float | None = None
    sl_trades = [t for t in all_trades if t.get("exit_reason") == "stop_loss"]
    tp_trades = [t for t in all_trades if t.get("exit_reason") == "take_profit"]
    if sl_trades:
        realised_sl_pct = abs(sum(
            abs(t["close_rate"] - t["open_rate"]) / t["open_rate"] * 100
            for t in sl_trades
        ) / len(sl_trades))
    if tp_trades:
        realised_tp_pct = abs(sum(
            abs(t["close_rate"] - t["open_rate"]) / t["open_rate"] * 100
            for t in tp_trades
        ) / len(tp_trades))

    # Use realised values when both are present (strategy-defined SL/TP path),
    # otherwise fall back to the slider values (which match what the engine used).
    using_realised = realised_sl_pct is not None and realised_tp_pct is not None
    sl_pct = realised_sl_pct if using_realised else abs(stoploss_pct)
    tp_pct = realised_tp_pct if using_realised else abs(take_profit_pct)
    if sl_pct + tp_pct > 0:
        breakeven_wr = sl_pct / (sl_pct + tp_pct)
        rr_ratio     = tp_pct / sl_pct if sl_pct > 0 else 0
    else:
        breakeven_wr = 0
        rr_ratio     = 0
    # Expected-value per trade in % of margin (positive = profitable
    # expectation; negative = guaranteed loss given infinite trades).
    ev_per_trade_pct = (win_rate * tp_pct - (1 - win_rate) * sl_pct) * leverage
    is_negative_ev   = win_rate < breakeven_wr

    # Cost-drag per trade — the headline number for "why is my profitable
    # strategy losing money?". Includes ALL real costs now (funding +
    # slippage + fees), so this is the actual production-grade drag.
    cost_drag_usdt   = total_funding + total_slippage + total_fees
    cost_drag_per_trade_usdt = (cost_drag_usdt / len(all_trades)) if all_trades else 0
    # Estimate avg margin used per trade (declines as balance changes).
    avg_margin = (sum(t.get("margin", 0) for t in all_trades) / len(all_trades)) if all_trades else 0
    cost_drag_per_trade_pct = (
        (cost_drag_per_trade_usdt / avg_margin) * 100 if avg_margin > 0 else 0
    )
    net_ev_per_trade_pct = ev_per_trade_pct - cost_drag_per_trade_pct
    is_negative_ev_after_costs = net_ev_per_trade_pct < 0

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
            # Production-grade cost-transparency rows. ALL THREE are
            # DEDUCTED from the simulated balance — the final P&L you see
            # is net of every cost you'd pay on KuCoin in production.
            "total_funding_paid":   round(total_funding, 4),
            "total_slippage_paid":  round(total_slippage, 4),
            "total_fees_paid":      round(total_fees, 4),     # real KuCoin fees, deducted
            "kucoin_taker_fee_pct": KUCOIN_TAKER_FEE * 100,
            "kucoin_maker_fee_pct": KUCOIN_MAKER_FEE * 100,
            # Math-check rails (see comment above the computation)
            "breakeven_win_rate":   round(breakeven_wr, 4),
            "risk_reward_ratio":    round(rr_ratio, 3),
            "expected_value_pct":   round(ev_per_trade_pct, 3),
            "is_negative_ev":       bool(is_negative_ev),
            # Cost-drag analysis — explains the gap between "WR above
            # breakeven, EV positive" and "balance ended negative". The
            # UI shows this when net EV flips sign vs gross EV.
            "cost_drag_per_trade_usdt": round(cost_drag_per_trade_usdt, 4),
            "cost_drag_per_trade_pct":  round(cost_drag_per_trade_pct, 3),
            "net_expected_value_pct":   round(net_ev_per_trade_pct, 3),
            "is_negative_ev_after_costs": bool(is_negative_ev_after_costs),
            # Source of the SL/TP used in the breakeven math. "realised"
            # means we measured actual trade outcomes (correct for strategies
            # that override SL/TP); "slider" means we used the UI values.
            "sltp_source_for_ev":   "realised" if using_realised else "slider",
            "realised_avg_sl_pct":  round(realised_sl_pct, 3) if realised_sl_pct is not None else None,
            "realised_avg_tp_pct":  round(realised_tp_pct, 3) if realised_tp_pct is not None else None,
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


# ── Auto-tune SL/TP grid ──────────────────────────────────────────────────
#
# Runs the SAME backtest with a small grid of SL/TP combinations and reports
# which combo gave the best result. Helps the user find the configuration
# that fits the strategy's actual signal quality on the chosen market —
# tight stops get whipsawed by noise, wide TPs are never reached, etc.
#
# The grid is small (3 × 4 = 12 runs) because each run is a real backtest
# (~5-30s); larger grids would time out the request. Data is cached at the
# load_futures_ohlcv / load_funding_history layer so all 12 runs share ONE
# KuCoin download.

# Default grid is intentionally tiny (2 SL × 3 TP = 6 cells) so the whole
# auto-tune fits inside Vercel's ~60s edge-proxy window — the previous 3×3
# grid still took 70-120s on 6M of 15m candles (≈35k bars × ≈10s/cell) and
# 502'd via "Application failed to respond". Six cells covers tight/medium
# SL with low/balanced/wide TP — enough to spot the breakeven cliff for
# most strategies, and the full grid completes in ~40-50s.
#
# Users who want a denser grid can pass sl_grid / tp_grid explicitly in the
# request body; the budget still applies so they always get partial results
# rather than a 502.
AUTO_TUNE_SL_GRID = [2.0, 4.0]                  # SL percentages
AUTO_TUNE_TP_GRID = [3.0, 6.0, 10.0]            # TP percentages
AUTO_TUNE_BUDGET_SECS = 50                       # hard deadline; returns partial


def auto_tune_sltp(
    strategy_name:    str,
    pairs:            list[str],
    timeframe:        str,
    timerange:        str,
    leverage:         int = 10,
    starting_balance: float = 1000.0,
    risk_per_trade:   float = 0.05,
    generated_code:   str | None = None,
    sl_grid:          list[float] | None = None,
    tp_grid:          list[float] | None = None,
) -> dict:
    """Run the SL/TP grid and return a ranked list of results.

    Wrapped in a time budget so we always return SOMETHING within
    Railway's HTTP timeout window — if half the grid completes in
    45s, we return those 4-5 results instead of letting the whole
    request 502 with the upstream proxy.
    """
    sl_grid = sl_grid or AUTO_TUNE_SL_GRID
    tp_grid = tp_grid or AUTO_TUNE_TP_GRID

    import time as _t
    deadline = _t.time() + AUTO_TUNE_BUDGET_SECS
    grid: list[dict] = []
    timed_out = False
    for sl in sl_grid:
        for tp in tp_grid:
            if _t.time() > deadline:
                timed_out = True
                break
            res = run_futures_backtest(
                strategy_name    = strategy_name,
                pairs            = pairs,
                timeframe        = timeframe,
                timerange        = timerange,
                leverage         = leverage,
                starting_balance = starting_balance,
                stoploss_pct     = sl,
                take_profit_pct  = tp,
                risk_per_trade   = risk_per_trade,
                generated_code   = generated_code,
                # Force slider SL/TP so each grid cell ACTUALLY tests its
                # configured combo — otherwise SMCStrategyTV (and any other
                # strategy with structural SL/TP) would produce identical
                # results in every cell and the grid would be useless.
                force_slider_sltp = True,
            )
            m = res.get("metrics", {})
            grid.append({
                "sl_pct":            sl,
                "tp_pct":            tp,
                "rr_ratio":          m.get("risk_reward_ratio", 0),
                "total_trades":      m.get("total_trades", 0),
                "win_rate":          m.get("win_rate", 0),
                "breakeven_wr":      m.get("breakeven_win_rate", 0),
                "expected_value":    m.get("expected_value_pct", 0),
                "is_negative_ev":    m.get("is_negative_ev", True),
                "total_profit_pct":  m.get("total_profit_pct", 0),
                "max_drawdown":      m.get("max_drawdown", 0),
                "liquidations":      m.get("liquidations", 0),
            })
        if timed_out:
            break

    if not grid:
        # Even ONE cell didn't fit in the budget. Return diagnostic error
        # the UI can show as a clean message instead of a 502.
        return {
            "error": (
                f"Auto-tune timed out before completing any backtest "
                f"({AUTO_TUNE_BUDGET_SECS}s budget). Try a shorter "
                f"timerange (1W or 1M) or a higher timeframe (1h/4h)."
            ),
            "verdict": "timeout",
            "grid":    [],
            "runs":    0,
        }

    # Best = highest profit among positive-EV rows; if no row has positive
    # EV, the best one is the LEAST bad (highest profit overall) with a
    # clear "no positive-EV combination found" warning attached.
    positive_ev = [r for r in grid if not r["is_negative_ev"]]
    if positive_ev:
        best = max(positive_ev, key=lambda r: r["total_profit_pct"])
        verdict = "found_positive_ev"
    else:
        best = max(grid, key=lambda r: r["total_profit_pct"])
        verdict = "no_positive_ev_in_grid"

    return {
        "verdict":      verdict,
        "best":         best,
        "grid":         grid,
        "sl_grid":      sl_grid,
        "tp_grid":      tp_grid,
        "strategy":     strategy_name,
        "pair":         pairs[0] if pairs else "",
        "timeframe":    timeframe,
        "timerange":    timerange,
        "leverage":     leverage,
        "runs":         len(grid),
        "expected_runs": len(sl_grid) * len(tp_grid),
        "timed_out":    timed_out,
        "budget_secs":  AUTO_TUNE_BUDGET_SECS,
    }
