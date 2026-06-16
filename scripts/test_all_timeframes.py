"""Comprehensive backtest test for both strategies across all TFs.
Verifies trades fire on 1m / 5m / 15m / 30m / 1h / 4h for multiple windows.
"""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.main as m
from backend.services.futures_backtester import run_futures_backtest


def fmt_secs_to_date(secs):
    return dt.datetime.utcfromtimestamp(secs).strftime("%Y%m%d")


def run_one(name, code, tf, days):
    end_ts   = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    trange = f"{fmt_secs_to_date(start_ts)}-{fmt_secs_to_date(end_ts)}"
    try:
        result = run_futures_backtest(
            strategy_name=name, pairs=["BTC/USDT"], timeframe=tf,
            timerange=trange, generated_code=code,
            leverage=10, starting_balance=1000.0,
            risk_per_trade=0.05, max_concurrent_positions=1,
            deduct_real_costs=False,
        )
        if "error" in result:
            return f"ERROR: {result['error'][:60]}"
        mt = result.get("metrics", {})
        n = mt.get("total_trades", 0)
        wr = mt.get("win_rate", 0) * 100
        pl = mt.get("total_profit_pct", 0)
        l_  = mt.get("long_trades", 0)
        s_ = mt.get("short_trades", 0)
        return f"trades={n:>3}  L/S={l_:>2}/{s_:>2}  WR={wr:5.1f}%  P&L={pl:+6.2f}%"
    except Exception as e:
        return f"EXC: {type(e).__name__}: {str(e)[:60]}"


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    strategies = [
        ("SMCStrategy1", m._SMC_STRATEGY_1_CODE),
        ("StrategyAsh",  m._STRATEGY_ASH_CODE),
    ]
    timeframes  = ["5m", "15m", "30m", "1h", "4h"]
    windows     = [30, 90, 180]   # 1M, 3M, 6M

    for (name, code) in strategies:
        print()
        print("=" * 88)
        print(f"  {name}")
        print("=" * 88)
        print(f"  {'TF':>4} {'1M':>40} {'3M':>40} {'6M':>40}")
        print(f"  {'─'*4} {'─'*40} {'─'*40} {'─'*40}")
        for tf in timeframes:
            row = f"  {tf:>4} "
            for d in windows:
                row += f"{run_one(name, code, tf, d):>40} "
            print(row)


if __name__ == "__main__":
    main()
