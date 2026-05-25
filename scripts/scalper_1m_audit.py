"""SMC Strategy (5min) backtest audit across 7d / 30d / 90d.

Validates trade frequency, WR, and monthly P&L target (15%+).
"""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv
from backend.services.futures_backtester import run_futures_backtest


def fmt_date(secs): return dt.datetime.utcfromtimestamp(secs).strftime("%Y%m%d")


def run_one(days: int) -> dict:
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    trange = f"{fmt_date(start_ts)}-{fmt_date(end_ts)}"

    df = load_futures_ohlcv("BTC/USDT", "5m", start_ts, end_ts)
    out = evaluate_strategy(m._SMC_SCALPER_1M_CODE, df.copy(),
                            pair="BTC/USDT", execution_tf="5m",
                            historical_anchor_ts=end_ts)
    el = out["enter_long"].fillna(0).astype(int).to_numpy()
    es = out["enter_short"].fillna(0).astype(int).to_numpy()
    edges_long  = int(((el == 1) & (np.roll(el, 1) == 0)).sum())
    edges_short = int(((es == 1) & (np.roll(es, 1) == 0)).sum())

    result = run_futures_backtest(
        strategy_name="SMCScalper5m", pairs=["BTC/USDT"], timeframe="5m",
        timerange=trange, generated_code=m._SMC_SCALPER_1M_CODE,
        leverage=10, starting_balance=1000.0,
        risk_per_trade=0.05, max_concurrent_positions=1,
        deduct_real_costs=False,
    )
    mt = result.get("metrics", {})
    return {
        "days":         days,
        "bars":         len(df),
        "edges":        edges_long + edges_short,
        "edges_long":   edges_long,
        "edges_short":  edges_short,
        "trades":       mt.get("total_trades", 0),
        "long_trades":  mt.get("long_trades", 0),
        "short_trades": mt.get("short_trades", 0),
        "wr":           mt.get("win_rate", 0) * 100,
        "pl":           mt.get("total_profit_pct", 0),
        "max_dd":       mt.get("max_drawdown_pct", 0),
    }


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    print("=" * 96)
    print("  SMC Strategy (5min) — backtest audit on real KuCoin BTC/USDT futures")
    print("=" * 96)
    print()
    print(f"  {'WIN':>4}  {'bars':>6}  {'edges':>8}  {'L':>4}  {'S':>4}  "
          f"{'trades':>7}  {'L':>3}  {'S':>3}  {'WR%':>6}  {'P&L%':>8}  {'MDD%':>6}  "
          f"{'monthly':>9}")
    print("  " + "-" * 92)
    for d in [7, 30, 90, 180]:
        r = run_one(d)
        monthly = r["pl"] / max(r["days"], 1) * 30
        print(f"  {d:>3}d  {r['bars']:>6}  {r['edges']:>8}  "
              f"{r['edges_long']:>4}  {r['edges_short']:>4}  "
              f"{r['trades']:>7}  {r['long_trades']:>3}  {r['short_trades']:>3}  "
              f"{r['wr']:>5.1f}%  {r['pl']:>+7.2f}%  {r['max_dd']:>5.2f}%  "
              f"{monthly:>+8.2f}%")


if __name__ == "__main__":
    main()
