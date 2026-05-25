"""Trace the AND-chain bottleneck for SMC Strategy (1min) on real data."""
from __future__ import annotations
import sys, time, datetime as dt
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv


def main():
    try: sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except: pass

    end_ts = int(time.time())
    start_ts = end_ts - 30 * 24 * 3600    # 30 days

    df = load_futures_ohlcv("BTC/USDT", "1m", start_ts, end_ts)
    out = evaluate_strategy(m._SMC_SCALPER_1M_CODE, df.copy(),
                            pair="BTC/USDT", execution_tf="1m",
                            historical_anchor_ts=end_ts)
    n = len(out)

    def col(k): return out[k].to_numpy() if k in out.columns else np.zeros(n, dtype=bool)

    print(f"SMC Strategy (1min) — 30 days × 1m on real BTC/USDT futures ({n} bars)\n")
    print("LONG chain (need ALL):")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bull",       col("htf_bias") == "bull"),
        ("+ in_discount",           col("in_discount").astype(bool)),
        ("+ recent_sweep_long",     col("recent_sweep_long").astype(bool)),
        ("+ choch_up",              col("choch_up").astype(bool)),
        ("+ displacement_up",       col("displacement_up").astype(bool)),
        ("+ ema_fast > ema_slow",   col("ema_fast") > col("ema_slow")),
        ("+ rsi < 72",              col("rsi_ok_long").astype(bool)),
        ("+ adx_ok",                col("adx_ok").astype(bool)),
        ("+ in_session",            col("in_session").astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"  {label:30s}  {cum.sum():>7} / {n} ({pct:5.2f}%)")

    print(f"\nSHORT chain:")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bear",       col("htf_bias") == "bear"),
        ("+ in_premium",            col("in_premium").astype(bool)),
        ("+ recent_sweep_short",    col("recent_sweep_short").astype(bool)),
        ("+ choch_dn",              col("choch_dn").astype(bool)),
        ("+ displacement_dn",       col("displacement_dn").astype(bool)),
        ("+ ema_fast < ema_slow",   col("ema_fast") < col("ema_slow")),
        ("+ rsi > 28",              col("rsi_ok_short").astype(bool)),
        ("+ adx_ok",                col("adx_ok").astype(bool)),
        ("+ in_session",            col("in_session").astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"  {label:30s}  {cum.sum():>7} / {n} ({pct:5.2f}%)")

    # Gate-coverage standalone (how often each fires)
    print(f"\nStandalone gate coverage:")
    for k in ["in_discount", "in_premium", "recent_sweep_long", "recent_sweep_short",
              "choch_up", "choch_dn", "displacement_up", "displacement_dn",
              "rsi_ok_long", "rsi_ok_short", "adx_ok", "in_session"]:
        v = col(k).astype(bool)
        print(f"  {k:25s}  {v.sum():>7} / {n} ({v.sum()/n*100:5.2f}%)")
    # HTF bias distribution
    htf = col("htf_bias")
    print(f"  htf_bias counts:           bull={int((htf=='bull').sum())} bear={int((htf=='bear').sum())} range={int((htf=='range').sum())}")


if __name__ == "__main__":
    main()
