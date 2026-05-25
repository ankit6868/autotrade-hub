"""Trace the AND-chain bottleneck for SMC Strategy (5min) on real data."""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    end_ts = int(time.time())
    start_ts = end_ts - 90 * 24 * 3600

    df = load_futures_ohlcv("BTC/USDT", "5m", start_ts, end_ts)
    out = evaluate_strategy(m._SMC_SCALPER_1M_CODE, df.copy(),
                            pair="BTC/USDT", execution_tf="5m",
                            historical_anchor_ts=end_ts)
    n = len(out)

    def col(k): return out[k].to_numpy() if k in out.columns else np.zeros(n, dtype=bool)

    print(f"SMC Strategy (5min) — 90 days × 5m on real BTC/USDT ({n} bars)\n")
    print("LONG chain (need ALL):")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bull",       col("htf_bias") == "bull"),
        ("+ in_discount",           col("in_discount").astype(bool)),
        ("+ recent_sweep_long",     col("recent_sweep_long").astype(bool)),
        ("+ recent_choch_up",       col("recent_choch_up").astype(bool)),
        ("+ recent_displ_up",       col("recent_displ_up").astype(bool)),
        ("+ in_long_zone",          col("in_long_zone").astype(bool)),
        ("+ ema21 >= ema50",        col("ema_fast") >= col("ema_slow") * 0.998),
        ("+ rsi < 72",              col("rsi_ok_long").astype(bool)),
        ("+ adx >= 20",             col("adx_ok").astype(bool)),
        ("+ vol_ok",                col("vol_ok").astype(bool)),
        ("+ in_session",            col("in_session").astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"  {label:25s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")

    print(f"\nSHORT chain:")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bear",        col("htf_bias") == "bear"),
        ("+ in_premium",            col("in_premium").astype(bool)),
        ("+ recent_sweep_short",    col("recent_sweep_short").astype(bool)),
        ("+ recent_choch_dn",       col("recent_choch_dn").astype(bool)),
        ("+ recent_displ_dn",       col("recent_displ_dn").astype(bool)),
        ("+ in_short_zone",         col("in_short_zone").astype(bool)),
        ("+ ema21 <= ema50",        col("ema_fast") <= col("ema_slow") * 1.002),
        ("+ rsi > 28",              col("rsi_ok_short").astype(bool)),
        ("+ adx >= 20",             col("adx_ok").astype(bool)),
        ("+ vol_ok",                col("vol_ok").astype(bool)),
        ("+ in_session",            col("in_session").astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"  {label:25s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")

    print(f"\nStandalone gate coverage:")
    for k in ["in_discount", "in_premium", "recent_sweep_long", "recent_sweep_short",
              "recent_choch_up", "recent_choch_dn", "recent_displ_up", "recent_displ_dn",
              "in_long_zone", "in_short_zone",
              "rsi_ok_long", "rsi_ok_short", "adx_ok", "vol_ok", "in_session"]:
        v = col(k).astype(bool)
        print(f"  {k:25s}  {v.sum():>7} / {n} ({v.sum()/n*100:5.2f}%)")
    htf = col("htf_bias")
    print(f"  htf_bias counts:           bull={int((htf=='bull').sum())} bear={int((htf=='bear').sum())} range={int((htf=='range').sum())}")


if __name__ == "__main__":
    main()
