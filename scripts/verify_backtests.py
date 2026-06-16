"""Run real backtests for SMCStrategy1 + StrategyAsh on 1M and 6M.
Report trade counts, win rate, P&L."""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import backend.main as m
from backend.services.futures_backtester import run_futures_backtest


def fmt_pct(x): return f"{x*100:+.2f}%"

def run(name, code, tf, days):
    end_ts   = int(time.time())
    start_ts = end_ts - days * 24 * 3600
    s = dt.datetime.utcfromtimestamp(start_ts).strftime("%Y%m%d")
    e = dt.datetime.utcfromtimestamp(end_ts).strftime("%Y%m%d")
    trange = f"{s}-{e}"
    print(f"\n── {name} @ {tf} | {days}d backtest ({trange}) ──")
    result = run_futures_backtest(
        strategy_name=name, pairs=["BTC/USDT"], timeframe=tf,
        timerange=trange, generated_code=code,
        leverage=10, starting_balance=1000.0,
        risk_per_trade=0.05, max_concurrent_positions=1,
        deduct_real_costs=False,
    )
    if "error" in result:
        print(f"   ERROR: {result['error']}")
        return
    mt = result.get("metrics", {})
    print(f"  total trades   : {mt.get('total_trades', 0)}")
    print(f"  long / short   : {mt.get('long_trades', 0)} / {mt.get('short_trades', 0)}")
    print(f"  win rate       : {mt.get('win_rate', 0)*100:.1f}%")
    print(f"  total P&L      : {fmt_pct(mt.get('total_profit_pct', 0)/100)}")
    print(f"  max drawdown   : {fmt_pct(mt.get('max_drawdown_pct', 0)/100)}")
    print(f"  final balance  : ${mt.get('final_balance', 0):.2f}")
    print(f"  signals fired  : {result.get('signals_fired', '?')}")
    dq = result.get("data_quality") or {}
    for p, d in dq.items():
        print(f"  data           : {p} loaded={d.get('candles_loaded')} expected={d.get('candles_expected')} coverage={d.get('coverage_pct')}% src={d.get('signal_source')}")
    # First few trade samples
    trades = result.get("trades", []) or []
    if trades:
        print(f"  trade dates (first 3 + last 3):")
        for t in trades[:3]:
            print(f"    {t.get('direction','?'):5s} open={t.get('open_date','?')} close={t.get('close_date','?')} pct={t.get('profit_pct',0):+.2f}% ({t.get('exit_reason','?')})")
        if len(trades) > 6:
            print(f"    ...")
        for t in trades[-3:]:
            print(f"    {t.get('direction','?'):5s} open={t.get('open_date','?')} close={t.get('close_date','?')} pct={t.get('profit_pct',0):+.2f}% ({t.get('exit_reason','?')})")

def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass
    run("SMCStrategy1", m._SMC_STRATEGY_1_CODE, "15m", 30)
    run("SMCStrategy1", m._SMC_STRATEGY_1_CODE, "15m", 180)
    run("StrategyAsh",  m._STRATEGY_ASH_CODE,    "5m",  30)
    run("StrategyAsh",  m._STRATEGY_ASH_CODE,    "5m",  180)

if __name__ == "__main__":
    main()
