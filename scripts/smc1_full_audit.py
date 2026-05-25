"""SMCStrategy1 @ 15m — full audit across 1W / 1M / 3M / 6M.

Reports trade count, WR, P&L, AND the bottleneck breakdown so we know
if we're losing trades to the strategy OR to the engine (in-trade
skips, max_concurrent_positions, daily limits, etc.).
"""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv
from backend.services.futures_backtester import run_futures_backtest


def fmt_secs_to_date(secs: int) -> str:
    return dt.datetime.utcfromtimestamp(secs).strftime("%Y%m%d")


def run_with_diagnostics(days: int) -> dict:
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    trange = f"{fmt_secs_to_date(start_ts)}-{fmt_secs_to_date(end_ts)}"

    # ── Raw signal trace ──────────────────────────────────────────────
    df = load_futures_ohlcv("BTC/USDT", "15m", start_ts, end_ts)
    out = evaluate_strategy(m._SMC_STRATEGY_1_CODE, df.copy(),
                            pair="BTC/USDT", execution_tf="15m",
                            historical_anchor_ts=end_ts)
    enter_long  = out["enter_long"].to_numpy().astype(int)
    enter_short = out["enter_short"].to_numpy().astype(int)
    sig_long_bars  = int(enter_long.sum())
    sig_short_bars = int(enter_short.sum())
    # Edges (0→1 transitions) = TradingView-style entry events
    edges_long  = int(((enter_long  == 1) & (np.roll(enter_long , 1) == 0)).sum())
    edges_short = int(((enter_short == 1) & (np.roll(enter_short, 1) == 0)).sum())

    # ── Full engine backtest ──────────────────────────────────────────
    result = run_futures_backtest(
        strategy_name="SMCStrategy1", pairs=["BTC/USDT"], timeframe="15m",
        timerange=trange, generated_code=m._SMC_STRATEGY_1_CODE,
        leverage=10, starting_balance=1000.0,
        risk_per_trade=0.05, max_concurrent_positions=1,
        deduct_real_costs=False,
    )
    mt = result.get("metrics", {})

    return {
        "days":            days,
        "bars":            len(df),
        "sig_long_bars":   sig_long_bars,
        "sig_short_bars":  sig_short_bars,
        "edges_long":      edges_long,
        "edges_short":     edges_short,
        "trades":          mt.get("total_trades", 0),
        "long_trades":     mt.get("long_trades", 0),
        "short_trades":    mt.get("short_trades", 0),
        "wr":              mt.get("win_rate", 0) * 100,
        "pl_pct":          mt.get("total_profit_pct", 0),
        "max_dd":          mt.get("max_drawdown_pct", 0),
    }


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    print("=" * 96)
    print("  SMCStrategy1 @ 15m — backtest audit across 1W / 1M / 3M / 6M")
    print("=" * 96)
    print()
    fmt = "  {:<5}  {:>5}  {:>9}  {:>9}  {:>8}  {:>5}  {:>5}  {:>7}  {:>7}"
    print(fmt.format("WIN", "bars", "sig-L bars", "sig-S bars",
                     "edges", "L", "S", "WR%", "P&L%"))
    print("  " + "-" * 92)

    rows = []
    for d in [7, 30, 90, 180]:
        r = run_with_diagnostics(d)
        rows.append(r)
        print(fmt.format(
            f"{d}d", r["bars"],
            f"{r['sig_long_bars']}", f"{r['sig_short_bars']}",
            f"{r['edges_long']}+{r['edges_short']}",
            r["long_trades"], r["short_trades"],
            f"{r['wr']:.1f}", f"{r['pl_pct']:+.2f}",
        ))

    # Interpretation
    print()
    print("INTERPRETATION:")
    for r in rows:
        total_edges = r["edges_long"] + r["edges_short"]
        opened = r["trades"]
        skipped = total_edges - opened
        print(f"  {r['days']:>3}d: {total_edges} entry signals → {opened} opened, "
              f"{skipped} skipped (already in-trade)")


if __name__ == "__main__":
    main()
