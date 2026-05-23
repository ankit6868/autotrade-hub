"""
Timeframe-aware Risk Engine for AutoTrade Hub.

Implements the spec from docs/specs/TIMEFRAME-AWARE-TP-SL.md (also recapped in
the architecture conversation): the *strategy* decides whether a trade is
valid and which direction to take, but this module decides the SCALE of the
trade — how far the stop loss and take profit sit from entry — based on the
selected execution timeframe.

A 1m scalp on BTCUSDT and a 1h swing on BTCUSDT can produce the SAME entry
signal at the SAME price, but should NOT have the same SL/TP distance. The
candle size, expected move, and noise floor are completely different.

Priority order when computing SL/TP for a fresh signal:

  1. Strategy-provided structural levels (e.g. SMC's last opposing pivot).
     When the strategy populates `sl_price` / `tp_price` columns on its
     dataframe, those are real institutional levels and we honour them.
     The risk engine still computes RR and rejects if RR < min_rr for the
     selected TF — that protects against e.g. a 1h strategy whose pivot
     sits 0.1% from price (RR ≈ 0.5).

  2. ATR-based defaults with per-TF multipliers, as per the spec. ATR
     naturally expands and contracts with candle size, so the same multiplier
     produces narrow scalp stops on 1m and wide swing stops on 1h.

  3. (Future) Structural snap — adjust ATR-based levels to the nearest
     swing high/low or Bollinger band if it's within a tolerable buffer.
     Phase-4 ships as priority 1+2 only; structural snap is a follow-up.

Every output carries a `why` / `source` field so the UI and audit log can
explain exactly which path produced the numbers. The spec requires this for
trustability — users need to see how SL/TP was computed.

Used by:
  • backend/services/futures_engine.py (live + paper bots)
  • backend/services/futures_backtester.py (Phase 4b — not yet wired)
  • backend/services/paper_scalp_engine.py (Phase 4c — not yet wired)
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)


# ─────────────────────────── Per-TF configuration ─────────────────────────────
# Defaults pulled from the spec's example table (section 6). These are the
# MIDPOINT of each spec range — conservative enough to backtest as defaults,
# aggressive enough to fire reasonable trade counts on BTCUSDT.
#
# Tuning notes from the spec:
#   - SL multiplier RANGE per TF means the developer can later make this
#     user-tunable per bot. For now we ship a single midpoint value per TF
#     so behaviour is predictable.
#   - min_rr=2.0 across all TFs — matches the spec's "Minimum RR" column.
#     A trade rejected at 2.0 R is a low-quality setup regardless of TF.
#   - atr_period=14 — TF-independent (industry default, ATR is self-scaling).
#
# NOTE: KuCoin Futures supports 1m / 5m / 15m / 30m / 1h / 4h / 1d kline
# granularities. We map all of them. Anything else falls back to 15m config.
TIMEFRAME_CONFIG: dict[str, dict] = {
    "1m":  {"atr_period": 14, "sl_mult": 0.85, "tp_mult": 2.00, "min_rr": 2.0, "style": "scalp"},
    "5m":  {"atr_period": 14, "sl_mult": 1.00, "tp_mult": 2.30, "min_rr": 2.0, "style": "fast_intraday"},
    "15m": {"atr_period": 14, "sl_mult": 1.30, "tp_mult": 2.70, "min_rr": 2.0, "style": "intraday"},
    "30m": {"atr_period": 14, "sl_mult": 1.60, "tp_mult": 3.20, "min_rr": 2.0, "style": "large_intraday"},
    "1h":  {"atr_period": 14, "sl_mult": 2.00, "tp_mult": 4.00, "min_rr": 2.0, "style": "swing"},
    "4h":  {"atr_period": 14, "sl_mult": 2.50, "tp_mult": 5.00, "min_rr": 2.0, "style": "position"},
    "1d":  {"atr_period": 14, "sl_mult": 3.00, "tp_mult": 6.00, "min_rr": 2.0, "style": "swing_daily"},
}
# Hard ceiling — even when ATR is huge (post-crash spike), don't risk more
# than this fraction of entry price on a single trade. Matches the spec's
# Edge Cases section ("If volatility is extremely high, enforce a maximum
# risk limit or reject trades where SL is too far from entry").
MAX_SL_PCT_OF_ENTRY = 0.08   # 8% — hard cap
# Floor — when ATR collapses (Asia chop), don't put SL inside spread noise.
MIN_SL_PCT_OF_ENTRY = 0.0008  # 0.08% — exchange spread + fee floor


def get_tf_config(tf: str, overrides: Optional[dict] = None) -> dict:
    """Return config for `tf`, defaulting to 15m if the TF is unknown.

    `overrides` is the {tf: {sl_mult, tp_mult, atr_period, min_rr}} map
    persisted on Config.risk_config_json. Per-user, per-TF — lets power
    users tune scalp / swing multipliers per pair without redeploying.
    Unknown keys fall back to the hardcoded TIMEFRAME_CONFIG values.
    """
    base = TIMEFRAME_CONFIG.get(tf, TIMEFRAME_CONFIG["15m"])
    if not overrides:
        return base
    user_tf = overrides.get(tf)
    if not user_tf:
        return base
    merged = dict(base)
    for k in ("atr_period", "sl_mult", "tp_mult", "min_rr"):
        if k in user_tf and user_tf[k] is not None:
            try:
                merged[k] = float(user_tf[k]) if k != "atr_period" else int(user_tf[k])
            except (TypeError, ValueError):
                pass
    return merged


def load_user_risk_overrides(user_id: str) -> Optional[dict]:
    """Read Config.risk_config_json for `user_id` and return the parsed
    overrides map. None when no row exists or the JSON is malformed.

    Cached for 30s on the module to avoid a DB hit on every signal scan.
    The engine fetches this once per signal scan via the helper; per-bot
    caching avoids a per-tick lookup."""
    import json, time
    global _OVERRIDES_CACHE
    now = time.time()
    entry = _OVERRIDES_CACHE.get(user_id)
    if entry and (now - entry[0] < 30):
        return entry[1]
    try:
        from backend.models import SessionLocal, Config
        from sqlalchemy import select
        with SessionLocal() as db:
            cfg = db.execute(
                select(Config).where(Config.user_id == user_id).limit(1)
            ).scalar_one_or_none()
        if not cfg or not getattr(cfg, "risk_config_json", None):
            _OVERRIDES_CACHE[user_id] = (now, None)
            return None
        parsed = json.loads(cfg.risk_config_json)
        if not isinstance(parsed, dict):
            _OVERRIDES_CACHE[user_id] = (now, None)
            return None
        _OVERRIDES_CACHE[user_id] = (now, parsed)
        return parsed
    except Exception:
        _OVERRIDES_CACHE[user_id] = (now, None)
        return None


_OVERRIDES_CACHE: dict[str, tuple[float, Optional[dict]]] = {}


def invalidate_overrides_cache(user_id: Optional[str] = None) -> None:
    """Drop the cached overrides — called when the user PUTs new values."""
    if user_id is None:
        _OVERRIDES_CACHE.clear()
    else:
        _OVERRIDES_CACHE.pop(user_id, None)


# ─────────────────────────── ATR computation ──────────────────────────────────

def compute_atr(df: pd.DataFrame, period: int = 14) -> float:
    """Wilder's ATR — returns the LAST bar's ATR value as a float.

    Why Wilder (EWM with alpha=1/period) instead of simple rolling mean:
    Wilder smoothing is the TradingView default and what most strategies
    actually expect. Simple rolling-mean ATR drifts from Pine's ta.atr by
    ~10% at typical periods, which compounds into noticeable SL/TP drift.

    Returns 0.0 if df is too short or contains NaNs at the tail.
    """
    if df is None or len(df) < period + 1:
        return 0.0
    high  = df["high"].astype(float)
    low   = df["low"].astype(float)
    close = df["close"].astype(float)
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / period, adjust=False).mean()
    last = atr.iloc[-1] if len(atr) else None
    if last is None or pd.isna(last):
        return 0.0
    return float(last)


# ─────────────────────────── Risk plan dataclass ──────────────────────────────

@dataclass
class RiskPlan:
    """Structured risk plan emitted for every fresh signal.

    Matches section 15 of the spec ("Output Format Required from System") so
    the UI can render a per-trade explanation and the audit log has the full
    derivation. `valid=False` means the engine should SKIP this signal —
    `rejected_reason` carries the human-readable explanation.
    """
    valid:           bool
    direction:       str        # 'long' | 'short'
    entry:           float
    sl:              float
    tp:              float
    atr:             float
    sl_mult:         float
    tp_mult:         float
    rr:              float
    timeframe:       str
    source:          str        # 'strategy' | 'atr' | 'atr_capped' | 'atr_floored'
    why:             str
    rejected_reason: Optional[str] = None
    # Optional second target — only populated when ARM is on AND the
    # strategy provided a meaningful tp2 (further from entry than tp).
    tp2:             Optional[float] = None

    def to_dict(self) -> dict:
        """Serialisable form for logging + UI display."""
        return {
            "status":          "VALID" if self.valid else "REJECTED",
            "direction":       self.direction,
            "timeframe":       self.timeframe,
            "entry":           round(self.entry, 6),
            "stopLoss":        round(self.sl, 6),
            "takeProfit":      round(self.tp, 6),
            "takeProfit2":     round(self.tp2, 6) if self.tp2 is not None else None,
            "atr":             round(self.atr, 6),
            "slMultiplier":    self.sl_mult,
            "tpMultiplier":    self.tp_mult,
            "rewardRisk":      round(self.rr, 3),
            "source":          self.source,
            "why":             self.why,
            "rejectedReason":  self.rejected_reason,
        }


# ─────────────────────────── Structural snap (FR-07) ─────────────────────────
# When the risk engine falls back to ATR-based SL/TP (Path 2), we optionally
# nudge those levels onto the nearest swing high/low so the stop sits BEYOND
# real market structure rather than inside random candle noise.
#
# The buffer is the maximum % adjustment we'll allow — if the nearest swing
# is further than this, we leave the ATR level alone (the swing isn't
# relevant, e.g. price is in a fresh breakout area with no recent swings).
SNAP_BUFFER_PCT = 0.005   # 0.5% — matches the architecture-audit spec

def _find_recent_swing_low(df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    """Lowest low in the last `lookback` bars. Used for long SL anchoring."""
    if df is None or len(df) < lookback:
        return None
    tail = df.iloc[-lookback:]
    val = float(tail["low"].min())
    return val if val > 0 else None

def _find_recent_swing_high(df: pd.DataFrame, lookback: int = 20) -> Optional[float]:
    """Highest high in the last `lookback` bars. Used for short SL anchoring."""
    if df is None or len(df) < lookback:
        return None
    tail = df.iloc[-lookback:]
    val = float(tail["high"].max())
    return val if val > 0 else None

def _snap_to_structure(
    *,
    sl: float,
    tp: float,
    entry: float,
    direction: str,
    df: pd.DataFrame,
    buffer_pct: float = SNAP_BUFFER_PCT,
) -> tuple[float, float, str]:
    """Snap SL to the nearest swing extreme IF it's within `buffer_pct` of
    the ATR-derived SL, with a small protective extension past the swing.
    Returns (new_sl, new_tp, reason). Reason is empty when no snap happened.

    Long trade rules:
      • SL was below entry. Find recent swing low.
      • If swing_low is between SL and entry AND within buffer_pct of SL,
        move SL to swing_low × (1 - 0.001) — just below the swing.
      • TP can shift proportionally to preserve the original RR (keeps
        the trade from becoming a worse setup).
    Short trade is the mirror.

    Conservative: when the snap would WIDEN risk by > 30%, we abort the
    snap (better to keep the ATR SL than blow up the risk budget).
    """
    if df is None or len(df) < 21:
        return sl, tp, ""

    orig_risk = abs(entry - sl)
    if orig_risk <= 0:
        return sl, tp, ""

    if direction == "long":
        swing = _find_recent_swing_low(df, lookback=20)
        if swing is None or swing >= entry:
            return sl, tp, ""
        # Distance from swing to ATR SL.
        delta = abs(swing - sl)
        if delta > entry * buffer_pct:
            return sl, tp, ""
        new_sl = swing * (1.0 - 0.001)   # 10bps below the swing
        # Risk inflation check — reject if too wide.
        new_risk = entry - new_sl
        if new_risk <= 0 or new_risk > orig_risk * 1.3:
            return sl, tp, ""
        # Keep RR roughly intact by extending TP by the SAME absolute amount.
        new_tp = entry + (tp - entry) * (new_risk / orig_risk)
        return new_sl, new_tp, f"snapped SL to 20-bar swing low {swing:.4f}"
    else:
        swing = _find_recent_swing_high(df, lookback=20)
        if swing is None or swing <= entry:
            return sl, tp, ""
        delta = abs(swing - sl)
        if delta > entry * buffer_pct:
            return sl, tp, ""
        new_sl = swing * (1.0 + 0.001)
        new_risk = new_sl - entry
        if new_risk <= 0 or new_risk > orig_risk * 1.3:
            return sl, tp, ""
        new_tp = entry - (entry - tp) * (new_risk / orig_risk)
        return new_sl, new_tp, f"snapped SL to 20-bar swing high {swing:.4f}"


# ─────────────────────────── Main entrypoint ──────────────────────────────────

def compute_tp_sl(
    *,
    entry:        float,
    direction:    str,                # 'long' | 'short'
    df:           pd.DataFrame,       # OHLCV at the SELECTED execution TF
    timeframe:    str,
    strategy_sl:  Optional[float] = None,
    strategy_tp:  Optional[float] = None,
    strategy_tp2: Optional[float] = None,
    min_rr_override: Optional[float] = None,
    user_overrides: Optional[dict] = None,   # NICE-4: per-user TF overrides
) -> RiskPlan:
    """
    Compute a RiskPlan for a fresh signal on the selected execution TF.

    Args:
        entry:        Live entry price (usually mark/last from the orderbook).
        direction:    'long' or 'short'.
        df:           OHLCV dataframe at `timeframe` granularity (used for ATR).
        timeframe:    KuCoin TF string: '1m', '5m', '15m', '30m', '1h', '4h', '1d'.
        strategy_sl:  Optional structural SL provided by the strategy (e.g.
                      SMC's last opposing pivot). When present and validates,
                      takes priority over the ATR-based default.
        strategy_tp:  Optional structural TP (e.g. 2R from strategy's risk).
        strategy_tp2: Optional second TP for multi-TP / ARM strategies.
        min_rr_override: Override the per-TF min RR. Used by stress tests.

    Returns:
        RiskPlan. Check `.valid` — False means SKIP this signal and log
        `.rejected_reason`.
    """
    cfg     = get_tf_config(timeframe, overrides=user_overrides)
    atr     = compute_atr(df, cfg["atr_period"])
    sl_mult = cfg["sl_mult"]
    tp_mult = cfg["tp_mult"]
    min_rr  = min_rr_override if min_rr_override is not None else cfg["min_rr"]

    direction = direction.lower()
    if direction not in ("long", "short"):
        return RiskPlan(
            valid=False, direction=direction, entry=entry, sl=0, tp=0,
            atr=atr, sl_mult=sl_mult, tp_mult=tp_mult, rr=0,
            timeframe=timeframe, source="atr", why="",
            rejected_reason=f"Unknown direction: {direction!r}",
        )

    # ── Path 1: Strategy provided structural levels ───────────────────────
    if (strategy_sl is not None and strategy_tp is not None
            and not pd.isna(strategy_sl) and not pd.isna(strategy_tp)
            and strategy_sl > 0 and strategy_tp > 0):
        # Validate direction
        ok = (direction == "long"  and strategy_sl < entry < strategy_tp) or \
             (direction == "short" and strategy_tp < entry < strategy_sl)
        if ok:
            risk = abs(entry - strategy_sl)
            reward = abs(strategy_tp - entry)
            rr = reward / max(risk, 1e-9)
            # Float-precision epsilon: strategies that compute exactly 2R
            # (e.g. SMCStrategyTV does `tp = entry + 2 * risk`) frequently
            # land at RR=1.99999... when re-divided. Allow a 0.01 R tolerance
            # so an "exactly 2R" target isn't rejected by the 2.0 floor.
            if rr < min_rr - 0.01:
                return RiskPlan(
                    valid=False, direction=direction, entry=entry,
                    sl=strategy_sl, tp=strategy_tp, atr=atr,
                    sl_mult=sl_mult, tp_mult=tp_mult, rr=rr,
                    timeframe=timeframe, source="strategy",
                    why=f"strategy-provided SL/TP on {timeframe}",
                    rejected_reason=(
                        f"RR {rr:.2f} below min {min_rr} for {timeframe} "
                        f"(strategy SL too close to entry or TP too tight)"
                    ),
                )
            tp2 = strategy_tp2 if (
                strategy_tp2 is not None
                and not pd.isna(strategy_tp2)
                and strategy_tp2 > 0
                and ((direction == "long"  and strategy_tp2 > strategy_tp) or
                     (direction == "short" and strategy_tp2 < strategy_tp))
            ) else None
            return RiskPlan(
                valid=True, direction=direction, entry=entry,
                sl=strategy_sl, tp=strategy_tp, atr=atr,
                sl_mult=sl_mult, tp_mult=tp_mult, rr=rr,
                timeframe=timeframe, source="strategy",
                why=f"strategy-provided SL/TP on {timeframe} (RR={rr:.2f})",
                tp2=tp2,
            )
        # Strategy SL/TP didn't validate (e.g. both above entry on a long).
        # Fall through to ATR path — log the reason for audit.
        log.info(
            "risk_engine: strategy SL/TP invalid for %s — SL=%s TP=%s entry=%s; falling back to ATR",
            direction, strategy_sl, strategy_tp, entry,
        )

    # ── Path 2: ATR-based with per-TF multipliers ─────────────────────────
    if atr <= 0:
        return RiskPlan(
            valid=False, direction=direction, entry=entry, sl=0, tp=0,
            atr=0, sl_mult=sl_mult, tp_mult=tp_mult, rr=0,
            timeframe=timeframe, source="atr", why="",
            rejected_reason=(
                f"ATR is zero on {timeframe} — need at least "
                f"{cfg['atr_period']+1} candles of history"
            ),
        )

    stop_distance = atr * sl_mult
    target_distance = atr * tp_mult

    # ── Floor: don't put SL inside spread noise (very low vol regime) ─────
    min_dist = entry * MIN_SL_PCT_OF_ENTRY
    source = "atr"
    if stop_distance < min_dist:
        stop_distance = min_dist
        # Keep RR roughly intact by lifting target too
        target_distance = max(target_distance, stop_distance * min_rr)
        source = "atr_floored"

    # ── Ceiling: hard cap on SL distance (high vol regime) ────────────────
    max_dist = entry * MAX_SL_PCT_OF_ENTRY
    if stop_distance > max_dist:
        return RiskPlan(
            valid=False, direction=direction, entry=entry, sl=0, tp=0,
            atr=atr, sl_mult=sl_mult, tp_mult=tp_mult, rr=0,
            timeframe=timeframe, source="atr_capped",
            why=f"ATR × {sl_mult} = {stop_distance:.2f} on {timeframe}",
            rejected_reason=(
                f"Stop distance {stop_distance:.2f} ({stop_distance/entry*100:.2f}%) "
                f"exceeds max {MAX_SL_PCT_OF_ENTRY*100:.1f}% of entry on {timeframe} — "
                f"market is too volatile for a clean setup"
            ),
        )

    if direction == "long":
        sl = entry - stop_distance
        tp = entry + target_distance
    else:
        sl = entry + stop_distance
        tp = entry - target_distance

    # ── FR-07: Structural snap to nearest swing if close enough ───────────
    # Conservative — only snaps if the swing is within 0.5% of the ATR SL.
    # Otherwise leaves the ATR levels alone (clean breakout with no nearby
    # swing). Risk inflation capped at +30%.
    snap_reason = ""
    snapped_sl, snapped_tp, snap_reason = _snap_to_structure(
        sl=sl, tp=tp, entry=entry, direction=direction, df=df,
    )
    if snap_reason:
        sl, tp = snapped_sl, snapped_tp
        source = "atr_snapped"
        # Recompute distances for the RR check below.
        stop_distance = abs(entry - sl)
        target_distance = abs(tp - entry)

    rr = target_distance / max(stop_distance, 1e-9)
    if rr < min_rr:
        # This branch is mostly defensive — the multipliers in TIMEFRAME_CONFIG
        # are chosen so tp_mult/sl_mult >= 2.0 always. But the floor logic
        # above can shift the ratio if ATR was extremely small.
        return RiskPlan(
            valid=False, direction=direction, entry=entry, sl=sl, tp=tp,
            atr=atr, sl_mult=sl_mult, tp_mult=tp_mult, rr=rr,
            timeframe=timeframe, source=source,
            why=f"ATR × ({sl_mult}/{tp_mult}) on {timeframe}",
            rejected_reason=f"RR {rr:.2f} below min {min_rr} for {timeframe}",
        )

    why = (
        f"ATR({cfg['atr_period']})={atr:.2f} × SL_mult={sl_mult} "
        f"= stop {stop_distance:.2f}; × TP_mult={tp_mult} "
        f"= target {target_distance:.2f} on {timeframe} ({cfg['style']})"
    )
    if snap_reason:
        why += f" — {snap_reason}"
    return RiskPlan(
        valid=True, direction=direction, entry=entry, sl=sl, tp=tp,
        atr=atr, sl_mult=sl_mult, tp_mult=tp_mult, rr=rr,
        timeframe=timeframe, source=source,
        why=why,
    )


# ─────────────────────────── Convenience helpers ──────────────────────────────

def format_plan_for_log(plan: RiskPlan, pair: str = "") -> str:
    """One-line summary suitable for the engine's last_action / log output."""
    if not plan.valid:
        return f"REJECTED {pair} {plan.direction}: {plan.rejected_reason}"
    return (
        f"PLAN {pair} {plan.direction} @ {plan.entry:.4f} "
        f"SL={plan.sl:.4f} TP={plan.tp:.4f} RR={plan.rr:.2f} "
        f"[{plan.source}, {plan.timeframe}]"
    )
