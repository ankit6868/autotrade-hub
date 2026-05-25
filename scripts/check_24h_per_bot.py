"""For each (strategy, tf) combo currently active in user's paper bots,
show what signals would have fired in the last 24h / 48h.
"""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv


def signals_in_window(const, exec_tf, days):
    end_ts   = int(time.time())
    start_ts = end_ts - int(days * 24 * 3600)
    df = load_futures_ohlcv("BTC/USDT", exec_tf, start_ts, end_ts)
    if len(df) == 0:
        return {"bars": 0, "edges_long": 0, "edges_short": 0, "long_ts": [], "short_ts": []}
    code = getattr(m, const)
    out = evaluate_strategy(code, df.copy(), pair="BTC/USDT",
                            execution_tf=exec_tf,
                            historical_anchor_ts=end_ts)
    el = out["enter_long"].fillna(0).astype(int).to_numpy()
    es = out["enter_short"].fillna(0).astype(int).to_numpy()
    edges_long  = int(((el == 1) & (np.roll(el, 1) == 0)).sum())
    edges_short = int(((es == 1) & (np.roll(es, 1) == 0)).sum())
    long_ts  = out.loc[(el == 1) & (np.roll(el, 1) == 0), "date"].astype(str).tolist()
    short_ts = out.loc[(es == 1) & (np.roll(es, 1) == 0), "date"].astype(str).tolist()
    return {"bars": len(df), "edges_long": edges_long, "edges_short": edges_short,
            "long_ts": long_ts[-5:], "short_ts": short_ts[-5:]}


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    # Bots active per user screenshot
    bots = [
        ("StrategyAsh",            "_STRATEGY_ASH_CODE",            "15m"),  # if on 15m
        ("StrategyAsh",            "_STRATEGY_ASH_CODE",            "5m"),
        ("BestPracticesV1",        "_BESTPRACTICES_V1_CODE",        "15m"),
        ("SMCStrategy1",           "_SMC_STRATEGY_1_CODE",          "15m"),
        ("BestPracticesV1Strict",  "_BESTPRACTICES_V1_STRICT_CODE", "15m"),
    ]
    for label, const, tf in bots:
        print()
        print("=" * 92)
        print(f"  {label} @ {tf}  (last 48h)")
        print("=" * 92)
        r = signals_in_window(const, tf, 2)
        print(f"  bars={r['bars']}  edges: {r['edges_long']} long, {r['edges_short']} short")
        for ts in r["long_ts"]:
            print(f"    LONG  {ts}")
        for ts in r["short_ts"]:
            print(f"    SHORT {ts}")


if __name__ == "__main__":
    main()
