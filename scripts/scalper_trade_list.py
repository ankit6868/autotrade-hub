"""List every individual trade SMC Strategy (1min) produced on 30d/90d
real BTC/USDT KuCoin futures data.
"""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.main as m
from backend.services.futures_backtester import run_futures_backtest


def fmt_date(secs): return dt.datetime.utcfromtimestamp(secs).strftime("%Y%m%d")


def run_and_list(days: int):
    end_ts = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    trange = f"{fmt_date(start_ts)}-{fmt_date(end_ts)}"

    result = run_futures_backtest(
        strategy_name="SMCScalper1m", pairs=["BTC/USDT"], timeframe="1m",
        timerange=trange, generated_code=m._SMC_SCALPER_1M_CODE,
        leverage=10, starting_balance=1000.0,
        risk_per_trade=0.05, max_concurrent_positions=1,
        deduct_real_costs=False,
    )
    trades = result.get("trades", [])
    print(f"\n{'='*100}")
    print(f"  SMC Strategy (1min) — {days}d backtest — {len(trades)} trades")
    print(f"{'='*100}")
    print(f"  {'#':>3}  {'open_time':<19}  {'dir':<5}  {'entry':>9}  "
          f"{'exit':>9}  {'pct%':>7}  {'reason':<14}  {'held':>4}")
    print(f"  {'-'*3}  {'-'*19}  {'-'*5}  {'-'*9}  {'-'*9}  {'-'*7}  {'-'*14}  {'-'*4}")
    for i, t in enumerate(trades, 1):
        ot = t.get("open_date", "")[:19] if isinstance(t.get("open_date"), str) else str(t.get("open_date", ""))[:19]
        print(f"  {i:>3}  {ot:<19}  {t.get('direction','?'):<5}  "
              f"{t.get('entry_price', 0):>9.2f}  {t.get('exit_price', 0):>9.2f}  "
              f"{t.get('profit_pct', 0):>+6.2f}%  {t.get('exit_reason','?'):<14}  "
              f"{t.get('candles_held', 0):>4}")


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass
    run_and_list(30)


if __name__ == "__main__":
    main()
