"""
Trace the AND-chain bottleneck for SMCStrategy1 + StrategyAsh on REAL
KuCoin futures data. Reports cumulative pass-count after each gate so
we can see exactly which gate kills the signal flow.

Usage:
    python -X utf8 -m scripts.trace_bottleneck
"""
from __future__ import annotations

import sys
import time
import datetime as dt
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
import pandas as pd

import backend.main as m
from backend.services.strategy_runner import evaluate_strategy
from backend.services.native_backtester import load_futures_ohlcv


def banner(s: str) -> None:
    print()
    print("=" * 78)
    print(f"  {s}")
    print("=" * 78)


def trace_smc1(df: pd.DataFrame, anchor_ts: int | None = None) -> None:
    banner(f"SMCStrategy1 @ 15m — cumulative AND-chain trace (anchor={anchor_ts})")
    out = evaluate_strategy(m._SMC_STRATEGY_1_CODE, df.copy(), pair="BTC/USDT", execution_tf="15m",
                            historical_anchor_ts=anchor_ts)
    n = len(out)
    arr = lambda k: out[k].to_numpy() if k in out.columns else None

    # Reconstruct entry chain in order (matches code):
    # long_setup = htf_bias=bull & in_discount & at_bull_zone & recent_bull_sweep
    #              & bull_bos_ltf & in_session
    htf = arr("htf_bias")
    in_disc = arr("in_discount")
    in_prem = arr("in_premium")
    bull_bos = arr("recent_bull_bos") if "recent_bull_bos" in out.columns else arr("bull_bos_ltf")
    bear_bos = arr("recent_bear_bos") if "recent_bear_bos" in out.columns else arr("bear_bos_ltf")
    rsweepL = arr("recent_bull_sweep")
    rsweepS = arr("recent_bear_sweep")
    in_sess = arr("in_session")
    bull_ob_mid  = arr("bull_ob_mid")
    bear_ob_mid  = arr("bear_ob_mid")
    bull_fvg_mid = arr("bull_fvg_mid")
    bear_fvg_mid = arr("bear_fvg_mid")
    closes = out["close"].to_numpy()

    # Use strategy-surfaced columns (truth, not a re-derivation)
    at_bull_zone = arr("at_bull_zone")
    at_bear_zone = arr("at_bear_zone")
    enter_long  = out["enter_long"].to_numpy()  if "enter_long"  in out.columns else None
    enter_short = out["enter_short"].to_numpy() if "enter_short" in out.columns else None

    # LONG chain (matches new code: no at_zone hard gate)
    print(f"\n  LONG chain (need ALL):")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bull",       htf == "bull"),
        ("+ in_discount",           in_disc.astype(bool)),
        ("+ recent_bull_sweep",    rsweepL.astype(bool)),
        ("+ bull_bos_ltf",         bull_bos.astype(bool)),
        ("+ in_session",           in_sess.astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"    {label:35s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")

    print(f"\n  SHORT chain:")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bear",        htf == "bear"),
        ("+ in_premium",            in_prem.astype(bool)),
        ("+ recent_bear_sweep",     rsweepS.astype(bool)),
        ("+ bear_bos_ltf",          bear_bos.astype(bool)),
        ("+ in_session",            in_sess.astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"    {label:35s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")

    # Soft confluence (no longer a hard gate but reported for visibility)
    print(f"\n  Zone confluence (soft, not gating):")
    if at_bull_zone is not None:
        print(f"    at_bull_zone:             {int(at_bull_zone.sum()):>6} / {n} ({at_bull_zone.sum()/n*100:5.2f}%)")
    if at_bear_zone is not None:
        print(f"    at_bear_zone:             {int(at_bear_zone.sum()):>6} / {n} ({at_bear_zone.sum()/n*100:5.2f}%)")
    print(f"    bull_ob_mid non-NaN:      {(~np.isnan(bull_ob_mid)).sum():>6} / {n}")
    print(f"    bull_fvg_mid non-NaN:     {(~np.isnan(bull_fvg_mid)).sum():>6} / {n}")
    if enter_long is not None:
        print(f"\n  FINAL signals: enter_long={int(enter_long.sum())}, enter_short={int(enter_short.sum())}")


def trace_ash(df: pd.DataFrame, anchor_ts: int | None = None) -> None:
    banner(f"StrategyAsh @ 5m — cumulative AND-chain trace (anchor={anchor_ts})")
    out = evaluate_strategy(m._STRATEGY_ASH_CODE, df.copy(), pair="BTC/USDT", execution_tf="5m",
                            historical_anchor_ts=anchor_ts)
    n = len(out)
    arr = lambda k: out[k].to_numpy() if k in out.columns else None

    htf = arr("htf_bias")
    in_disc = arr("in_discount")
    in_prem = arr("in_premium")
    rsweepL = arr("recent_sweep_long")
    rsweepS = arr("recent_sweep_short")
    chochU = arr("recent_choch_up") if "recent_choch_up" in out.columns else arr("choch_up")
    chochD = arr("recent_choch_dn") if "recent_choch_dn" in out.columns else arr("choch_dn")
    displU = arr("recent_displ_up") if "recent_displ_up" in out.columns else arr("displacement_up")
    displD = arr("recent_displ_dn") if "recent_displ_dn" in out.columns else arr("displacement_dn")
    fvgBull = arr("fresh_bull_fvg")
    fvgBear = arr("fresh_bear_fvg")
    adx_ok = arr("adx_ok")
    rsi_okL = arr("rsi_ok_long")
    rsi_okS = arr("rsi_ok_short")

    enter_long  = out["enter_long"].to_numpy()  if "enter_long"  in out.columns else None
    enter_short = out["enter_short"].to_numpy() if "enter_short" in out.columns else None

    print(f"\n  LONG chain (need ALL — current code w/ ema_align + vwap_ok DROPPED):")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bull",   htf == "bull"),
        ("+ in_discount",       in_disc.astype(bool)),
        ("+ recent_sweep_long", rsweepL.astype(bool)),
        ("+ choch_up",          chochU.astype(bool)),
        ("+ displacement_up",   displU.astype(bool)),
        ("+ fresh_bull_fvg",    fvgBull.astype(bool)),
        ("+ adx_ok",            adx_ok.astype(bool)),
        ("+ rsi_ok_long",       rsi_okL.astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"    {label:32s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")

    print(f"\n  SHORT chain:")
    cum = np.ones(n, dtype=bool)
    for label, gate in [
        ("htf_bias == bear",        htf == "bear"),
        ("+ in_premium",            in_prem.astype(bool)),
        ("+ recent_sweep_short",    rsweepS.astype(bool)),
        ("+ choch_dn",              chochD.astype(bool)),
        ("+ displacement_dn",       displD.astype(bool)),
        ("+ fresh_bear_fvg",        fvgBear.astype(bool)),
        ("+ adx_ok",                adx_ok.astype(bool)),
        ("+ rsi_ok_short",          rsi_okS.astype(bool)),
    ]:
        cum = cum & gate
        pct = cum.sum() / max(n, 1) * 100
        print(f"    {label:32s}  {cum.sum():>6} / {n} ({pct:5.2f}%)")
    if enter_long is not None:
        print(f"\n  FINAL signals: enter_long={int(enter_long.sum())}, enter_short={int(enter_short.sum())}")


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    end_ts   = int(time.time())
    start_ts = end_ts - 180 * 24 * 3600     # 6 months

    print(f"Window: {dt.datetime.utcfromtimestamp(start_ts)} → {dt.datetime.utcfromtimestamp(end_ts)} UTC")

    df_15m = load_futures_ohlcv("BTC/USDT", "15m", start_ts, end_ts)
    print(f"15m bars loaded: {len(df_15m)}")
    trace_smc1(df_15m, anchor_ts=end_ts)

    df_5m = load_futures_ohlcv("BTC/USDT", "5m", start_ts, end_ts)
    print(f"\n5m bars loaded: {len(df_5m)}")
    trace_ash(df_5m, anchor_ts=end_ts)


if __name__ == "__main__":
    main()
