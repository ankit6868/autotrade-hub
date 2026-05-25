"""Check what signals SMCStrategy1 + StrategyAsh WOULD have fired
in the last 24h (and 7 days) on real KuCoin data. Tells us whether
the bot is broken OR the market just had no SMC setups.
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


def signals_in_window(strategy_const: str, exec_tf: str, days: float) -> dict:
    end_ts   = int(time.time())
    start_ts = end_ts - int(days * 24 * 3600)
    df = load_futures_ohlcv("BTC/USDT", exec_tf, start_ts, end_ts)
    code = getattr(m, strategy_const)
    out = evaluate_strategy(code, df.copy(), pair="BTC/USDT",
                            execution_tf=exec_tf,
                            historical_anchor_ts=end_ts)
    enter_long  = out["enter_long"].fillna(0).astype(int).to_numpy()
    enter_short = out["enter_short"].fillna(0).astype(int).to_numpy()
    edges_long  = int(((enter_long  == 1) & (np.roll(enter_long , 1) == 0)).sum())
    edges_short = int(((enter_short == 1) & (np.roll(enter_short, 1) == 0)).sum())
    # Edge timestamps
    long_ts  = out.loc[(enter_long  == 1) & (np.roll(enter_long , 1) == 0), "date"].tolist()
    short_ts = out.loc[(enter_short == 1) & (np.roll(enter_short, 1) == 0), "date"].tolist()
    return {
        "bars": len(df),
        "first_bar":  df["date"].iloc[0]  if len(df) else None,
        "last_bar":   df["date"].iloc[-1] if len(df) else None,
        "edges_long": edges_long,
        "edges_short": edges_short,
        "long_ts":  long_ts[-10:],   # last 10 entries
        "short_ts": short_ts[-10:],
    }


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    for (label, const, tf) in [
        ("SMCStrategy1 @ 15m", "_SMC_STRATEGY_1_CODE", "15m"),
        ("StrategyAsh @ 5m",   "_STRATEGY_ASH_CODE",   "5m"),
    ]:
        print()
        print("=" * 90)
        print(f"  {label}")
        print("=" * 90)
        for d in [1, 3, 7]:
            r = signals_in_window(const, tf, d)
            print(f"\n  Last {d} day(s):  bars={r['bars']}  range=[{r['first_bar']} → {r['last_bar']}]")
            print(f"    edges:  {r['edges_long']} long,  {r['edges_short']} short")
            if r["long_ts"]:
                print(f"    most recent long entries:")
                for t in r["long_ts"]:  print(f"      {t}")
            if r["short_ts"]:
                print(f"    most recent short entries:")
                for t in r["short_ts"]: print(f"      {t}")


if __name__ == "__main__":
    main()
