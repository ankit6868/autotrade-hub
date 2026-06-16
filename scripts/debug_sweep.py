"""Reproduce the sweep_long detection manually outside the strategy
to see why it fires so rarely."""
from __future__ import annotations
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd
from backend.services.native_backtester import load_futures_ohlcv

def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass
    end_ts   = int(time.time())
    start_ts = end_ts - 30 * 24 * 3600     # 1 month, fast
    df = load_futures_ohlcv("BTC/USDT", "5m", start_ts, end_ts)
    print(f"loaded {len(df)} 5m bars")

    lows  = df["low"].to_numpy()
    highs = df["high"].to_numpy()
    closes = df["close"].to_numpy()

    SWEEP_LOOKBACK = 20
    recent_low  = pd.Series(lows ).shift(1).rolling(SWEEP_LOOKBACK, min_periods=5).min().to_numpy()
    recent_high = pd.Series(highs).shift(1).rolling(SWEEP_LOOKBACK, min_periods=5).max().to_numpy()

    cond1 = (lows < recent_low)
    cond2 = (closes > recent_low)
    sweep_long = cond1 & cond2

    print(f"\n(lows < recent_low)            fires: {int(cond1.sum())}/{len(df)} ({cond1.sum()/len(df)*100:.2f}%)")
    print(f"(closes > recent_low)           fires: {int(cond2.sum())}/{len(df)} ({cond2.sum()/len(df)*100:.2f}%)")
    print(f"sweep_long (both)               fires: {int(sweep_long.sum())}/{len(df)} ({sweep_long.sum()/len(df)*100:.2f}%)")

    # Sanity check: are there bars where low went below recent_low?
    # First 30 bars where cond1 is true
    print("\nFirst 10 cond1 bars:")
    cond1_idxs = np.where(cond1)[0][:10]
    for i in cond1_idxs:
        print(f"  bar {i:>5} low={lows[i]:>10.2f}  recent_low={recent_low[i]:>10.2f}  "
              f"close={closes[i]:>10.2f}  cond2={cond2[i]}  sweep_long={sweep_long[i]}")

if __name__ == "__main__":
    main()
