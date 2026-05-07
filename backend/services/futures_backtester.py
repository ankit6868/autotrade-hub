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

from backend.services.native_backtester import (
    load_ohlcv, add_indicators,
    _signal_miss_candle_short, _signal_miss_candle_long,
    _signal_macd_crossover, _signal_rsi_bollinger, _signal_ema_scalping,
    _guess_strategy,
)

# Funding fee per 8-hour period (KuCoin standard)
FUNDING_RATE = 0.0003   # 0.03%
CANDLES_PER_8H = {"1m": 480, "5m": 96, "15m": 32, "30m": 16, "1h": 8, "4h": 2}


def _calc_liquidation(entry: float, direction: str, leverage: int) -> float:
    """Return the liquidation price (simplified isolated margin)."""
    mm = 0.005   # 0.5% maintenance margin
    if direction == "long":
        return round(entry * (1 - 1.0 / leverage + mm), 6)
    else:
        return round(entry * (1 + 1.0 / leverage - mm), 6)


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
) -> dict:
    """
    Run a leveraged futures backtest.

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

    signal_fn = _guess_strategy(strategy_name)
    all_trades: list[dict] = []
    balance = starting_balance
    candles_per_8h = CANDLES_PER_8H.get(timeframe, 32)

    for pair in pairs:
        # ── Load OHLCV ────────────────────────────────────────────────────
        try:
            df = load_ohlcv(pair, timeframe, start_ts, end_ts)
        except Exception as e:
            return {"error": f"Data download failed for {pair}: {e}"}
        df = add_indicators(df)

        in_trade     = False
        entry_price  = sl = tp = liq_price = None
        direction    = None
        entry_date   = None
        candles_held = 0
        margin       = 0.0

        for i in range(3, len(df)):
            row = df.iloc[i]

            if in_trade:
                lo, hi    = row["low"], row["high"]
                candles_held += 1

                # ── Funding fee every 8h ──────────────────────────────────
                funding_cost = 0.0
                if candles_held % candles_per_8h == 0:
                    funding_cost = margin * FUNDING_RATE * leverage

                # ── Check liquidation (highest priority) ──────────────────
                liquidated = False
                if direction == "long" and lo <= liq_price:
                    exit_p   = liq_price
                    pnl_abs  = -margin          # full margin loss
                    liquidated = True
                elif direction == "short" and hi >= liq_price:
                    exit_p   = liq_price
                    pnl_abs  = -margin
                    liquidated = True

                if not liquidated:
                    # ── Check SL / TP ─────────────────────────────────────
                    exited = False
                    if direction == "long":
                        if lo <= sl:
                            exit_p = sl; exited = True; exit_rsn = "stop_loss"
                        elif hi >= tp:
                            exit_p = tp; exited = True; exit_rsn = "take_profit"
                    else:  # short
                        if hi >= sl:
                            exit_p = sl; exited = True; exit_rsn = "stop_loss"
                        elif lo <= tp:
                            exit_p = tp; exited = True; exit_rsn = "take_profit"

                    if not exited:
                        balance -= funding_cost
                        continue

                    # ── Compute leveraged P&L ─────────────────────────────
                    if direction == "long":
                        price_move_pct = (exit_p - entry_price) / entry_price
                    else:
                        price_move_pct = (entry_price - exit_p) / entry_price

                    leveraged_pnl_pct = price_move_pct * leverage
                    pnl_abs = margin * leveraged_pnl_pct - funding_cost
                    # Cap loss at full margin
                    pnl_abs = max(pnl_abs, -margin)

                balance += pnl_abs
                balance = max(balance, 0)

                profit_pct = (pnl_abs / margin * 100) if margin > 0 else 0

                all_trades.append({
                    "pair":           pair,
                    "direction":      direction,
                    "leverage":       leverage,
                    "open_date":      str(entry_date),
                    "close_date":     str(row["date"]),
                    "entry":          round(float(entry_price), 4),
                    "open_rate":      round(float(entry_price), 4),
                    "close_rate":     round(float(exit_p), 4),
                    "sl_price":       round(float(sl), 4),
                    "tp_price":       round(float(tp), 4),
                    "liq_price":      round(float(liq_price), 4),
                    "margin":         round(float(margin), 4),
                    "profit_pct":     round(float(profit_pct), 3),
                    "profit_abs":     round(float(pnl_abs), 4),
                    "exit_reason":    "liquidated" if liquidated else exit_rsn,
                    "balance":        round(float(balance), 2),
                    "candles_held":   candles_held,
                })
                in_trade = False

            else:
                if balance <= 0:
                    break

                sig = signal_fn(df, i)
                if sig:
                    entry_price, sl_raw, tp_raw, direction = sig

                    # Override SL/TP with user-defined percentages
                    if direction == "long":
                        sl = entry_price * (1 - stoploss_pct / 100)
                        tp = entry_price * (1 + take_profit_pct / 100)
                    else:
                        sl = entry_price * (1 + stoploss_pct / 100)
                        tp = entry_price * (1 - take_profit_pct / 100)

                    liq_price    = _calc_liquidation(entry_price, direction, leverage)
                    margin       = balance * risk_per_trade
                    entry_date   = row["date"]
                    candles_held = 0
                    in_trade     = True

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
        "trades":       all_trades,
        "equity_curve": equity_curve,
    }
