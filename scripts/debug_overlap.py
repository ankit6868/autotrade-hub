"""Inspect concrete bars where StrategyAsh bull_bias + in_discount fires
and check why recent_sweep_long doesn't overlap."""
from __future__ import annotations
import sys, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    end_ts   = int(time.time())
    start_ts = end_ts - 180 * 24 * 3600
    df = load_futures_ohlcv("BTC/USDT", "5m", start_ts, end_ts)
    print(f"loaded {len(df)} 5m bars")

    out = evaluate_strategy(m._STRATEGY_ASH_CODE, df.copy(), pair="BTC/USDT", execution_tf="5m")
    htf = out["htf_bias"].to_numpy()
    in_disc = out["in_discount"].to_numpy()
    rec_sweep = out["recent_sweep_long"].to_numpy()
    sweep_raw = out["sweep_long"].to_numpy()
    closes = out["close"].to_numpy()
    range_md = out["htf_range_md"].to_numpy()
    range_hi = out["htf_range_hi"].to_numpy()
    range_lo = out["htf_range_lo"].to_numpy()
    dates = out["date"].to_numpy()

    mask = (htf == "bull") & in_disc.astype(bool)
    idxs = np.where(mask)[0]
    print(f"\nbars with bull_bias + in_discount: {len(idxs)}")
    print(f"of those, with recent_sweep_long: {int(rec_sweep[idxs].sum())}")

    # Print first 5 segments where bull_bias + in_discount is true
    print(f"\nSample bars (first 5 instances):")
    sampled = idxs[:5] if len(idxs) >= 5 else idxs
    for i in sampled:
        # Look for the last sweep_long within 30 bars before i
        win_start = max(0, i - 50)
        sweep_in_win = sweep_raw[win_start: i+1]
        last_sweep_offset = int(np.where(sweep_in_win)[0].max() - len(sweep_in_win) + 1) if sweep_in_win.any() else None
        # Position in range
        pos = (closes[i] - range_lo[i]) / max(range_hi[i] - range_lo[i], 1e-9)
        print(f"  bar {i:>5} {pd.Timestamp(dates[i]).isoformat():25s} "
              f"close={closes[i]:>10.2f}  range=[{range_lo[i]:.2f},{range_hi[i]:.2f}] "
              f"pos_in_range={pos:.3f}  "
              f"last_sweep_offset={last_sweep_offset}  rec={rec_sweep[i]}")

    # Now: how many raw sweep_long bars exist globally?
    print(f"\nraw sweep_long globally: {int(sweep_raw.sum())}")
    print(f"recent_sweep_long globally: {int(rec_sweep.sum())}")

    # Look at overlap of sweep with htf_bias
    print(f"\nsweep distribution by bias:")
    for b in ("bull", "bear", "range"):
        m_bias = (htf == b)
        n_bias = int(m_bias.sum())
        n_sweep_in = int(sweep_raw[m_bias].sum())
        print(f"  bias={b:5s}: {n_bias:>6} bars, {n_sweep_in:>5} sweep_long ({n_sweep_in/max(n_bias,1)*100:.2f}%)")


if __name__ == "__main__":
    main()
