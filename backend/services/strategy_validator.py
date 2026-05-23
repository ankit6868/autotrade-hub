"""
Strategy Validator — Missing-Rule Resolver + Confidence Scorer.

Implements PDF §3 ("Mandatory Safety Decision Logic") and §3.1 ("Confidence
Score Rules"). Given a compiled IStrategy class + its generated_code +
optional natural-language description, produces a fully-populated
StrategyTemplate with:

  • conditions[]       — decoded rules with role + source tags
  • risk + trade_limits — filled from safe defaults where missing
  • confidence_score   — 0-100
  • live_permission    — 'live_eligible' | 'demo_only' | 'backtest_only' | 'blocked'
  • missing_fields     — critical fields still empty
  • inferred_fields    — filled by safe defaults
  • conflicts          — internal logic contradictions detected
  • resolver_notes     — human-readable trail for the UI

Design choices:
  • NO LLM call inside this module. The decoder layer (strategy_parser.py)
    already paid the LLM cost when the user uploaded; we just inspect the
    resulting Python class + dataframe attrs and apply deterministic rules.
  • Rule extraction reads the strategy_runner output's `.attrs` (which
    populate_indicators sets) for sl_price/tp_price columns + class
    attributes (`timeframe`, `stoploss`, `minimal_roi`, `can_short`).
  • Confidence math is intentionally simple and inspectable — no ML model,
    just a transparent rubric the user can reason about.

Called from:
  • backend/services/strategy_parser.py — after the LLM generates code
  • backend/routers/strategy.py — to refresh confidence on demand
  • backend/routers/futures.py — live-guardrail check before opening a bot
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .strategy_template import (
    StrategyTemplate, StrategyCondition, StrategyRisk, StrategyTradeLimits,
    CRITICAL_FIELDS, classify_missing_field,
)

log = logging.getLogger(__name__)


# ── Safe defaults per trade mode (PDF §7) ────────────────────────────────
# These are the values the resolver fills in when the user didn't specify
# them and the field is non-critical or inferable. Every filled value is
# tagged `source="default_safe"` so the UI can highlight "this came from
# the bot, not your strategy."
#
# Numbers chosen to match PDF §7 mid-points where ranges are given.

_DEFAULTS_BY_MODE: dict[str, dict] = {
    "scalp": {
        "risk_per_trade_pct":  0.35,   # spec: 0.25-0.5%
        "risk_reward":         1.5,    # spec: 1R-1.5R
        "max_trades_per_day":  8,      # spec: 5-10
        "cooldown_candles":    4,      # spec: 3-5
    },
    "intraday": {
        "risk_per_trade_pct":  0.75,   # spec: 0.5-1.0%
        "risk_reward":         2.0,    # spec: 1.5R-2R
        "max_trades_per_day":  4,      # spec: 2-5
        "cooldown_candles":    3,
    },
    "swing": {
        "risk_per_trade_pct":  1.0,    # spec: 0.5-1%
        "risk_reward":         2.5,    # spec: 2R-3R
        "max_trades_per_day":  2,      # spec: 1-3
        "cooldown_candles":    2,
    },
    "position": {
        "risk_per_trade_pct":  1.0,
        "risk_reward":         3.0,
        "max_trades_per_day":  1,
        "cooldown_candles":    1,
    },
}


def _mode_from_timeframe(tf: str) -> str:
    """Infer the trading mode from the execution timeframe."""
    if tf in ("1m", "3m"):
        return "scalp"
    if tf in ("5m", "15m"):
        return "intraday"
    if tf in ("30m", "1h"):
        return "swing"
    if tf in ("4h", "1d", "1w"):
        return "position"
    return "intraday"


# ── Direction inference ──────────────────────────────────────────────────

def _infer_direction(strategy_cls, signal_cols: list[str]) -> str:
    """Read `can_short` + populated signal columns to decide direction."""
    can_short = bool(getattr(strategy_cls, "can_short", False))
    has_long  = "enter_long"  in signal_cols
    has_short = "enter_short" in signal_cols
    if has_long and has_short and can_short:
        return "both"
    if has_short and not has_long:
        return "short"
    if has_long and not has_short:
        return "long"
    if can_short:
        return "both"
    return "long"   # safe default — most user strategies are long-only


# ── Rule decoding from generated_code (regex inspection) ─────────────────
# We use lightweight regexes to spot the most common Freqtrade patterns
# (RSI, EMA, MACD, BB, ATR) instead of full AST parsing. Misses are OK —
# they show up as `unknown_role` conditions and only nudge the confidence
# score down a little. This intentionally trades precision for simplicity.

_INDICATOR_PATTERNS = [
    # ta.RSI(df, timeperiod=14) | dataframe["rsi"]
    (r"\bRSI\b",                  "RSI"),
    (r"['\"](rsi(?:_\d+)?)['\"]", "RSI"),
    # ta.EMA / SMA / WMA
    (r"\b(EMA|SMA|WMA)\b",        "MA"),
    (r"['\"](ema_?\d+|sma_?\d+)['\"]", "MA"),
    # MACD
    (r"\bMACD\b",                 "MACD"),
    (r"['\"](macd(?:signal|hist)?)['\"]", "MACD"),
    # Bollinger
    (r"\bBBANDS\b",               "BBANDS"),
    (r"['\"](upperband|lowerband|middleband)['\"]", "BBANDS"),
    # ATR
    (r"\bATR\b",                  "ATR"),
    # Pivot / swing — common in SMC strategies
    (r"pivot_(high|low)",         "PIVOT"),
    (r"swing_(high|low)",         "PIVOT"),
    (r"BOS\b|bos\b",              "BOS"),
    (r"FVG\b|fvg\b",              "FVG"),
    # Volume
    (r"\b(volume|vol)\b\s*[<>=]", "VOLUME"),
]


def _decode_indicators(generated_code: str) -> list[str]:
    """Return a deduped list of indicator names mentioned in the code."""
    found = set()
    for pattern, name in _INDICATOR_PATTERNS:
        if re.search(pattern, generated_code):
            found.add(name)
    return sorted(found)


def _has_entry_trigger(df: pd.DataFrame) -> bool:
    """True iff at least one entry-signal bar fired during the warmup
    compile against a dummy dataframe. Confirms the strategy's
    populate_entry_trend can actually produce signals (defensive — catches
    strategies that compile but never fire under any conditions)."""
    for col in ("enter_long", "enter_short"):
        if col in df.columns:
            try:
                if (df[col].fillna(0).astype(int) != 0).any():
                    return True
            except Exception:
                pass
    return False


def _strategy_has_structural_sl_tp(df: pd.DataFrame) -> bool:
    """True iff the strategy populated sl_price / tp_price columns
    (structural levels) rather than relying on the engine's slider %s."""
    return ("sl_price" in df.columns) and ("tp_price" in df.columns)


# ── Conflict detection ───────────────────────────────────────────────────

def _detect_conflicts(generated_code: str) -> list[str]:
    """Flag obvious contradictions like 'enter_long when RSI > 70' (which
    is normally a SHORT condition). Line-scoped so a strategy with BOTH
    'enter_long when RSI<30' AND 'enter_short when RSI>70' doesn't
    false-flag — those are healthy bidirectional rules on DIFFERENT lines.

    Only flags when 'enter_long' AND 'rsi > 70+' appear on the SAME line
    (or within a single .loc[...] expression unbroken by a newline).
    """
    conflicts: list[str] = []
    for line in generated_code.split("\n"):
        l = line.lower()
        # Skip lines that touch BOTH enter_long and enter_short — those are
        # multi-direction assignments where the threshold tied to each side
        # can't be disambiguated by simple regex (rare in practice).
        if "enter_long" in l and "enter_short" in l:
            continue
        if "enter_long" in l and re.search(r"rsi[^<\n]{0,40}>\s*(7[0-9]|8\d|9\d)", l):
            conflicts.append(
                "Possible logic conflict: enter_long triggers when RSI > 70+ "
                "(usually a short condition). Review the entry rule."
            )
        if "enter_short" in l and re.search(r"rsi[^>\n]{0,40}<\s*([12]\d|30)\b", l):
            conflicts.append(
                "Possible logic conflict: enter_short triggers when RSI < 30 "
                "(usually a long condition). Review the entry rule."
            )
    # Dedup repeated identical flags from a multi-occurrence file.
    seen: set[str] = set()
    out: list[str] = []
    for c in conflicts:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


# ── Confidence scoring rubric ────────────────────────────────────────────
# Transparent + auditable. Each bullet adds or subtracts from a base of 50.
# Maps to PDF §3.1 score bands:
#   85-100 → live eligible
#   70-84  → demo only
#   50-69  → backtest only
#   <50    → blocked

def _compute_confidence(template: StrategyTemplate, df: Optional[pd.DataFrame]) -> int:
    score = 50

    # Decoder reached at least one rule of each major role.
    if template.has_role("entry_trigger"):    score += 15
    if template.has_role("bias_filter") or template.has_role("trend_filter"):
        score += 10
    if template.has_role("setup_filter"):     score += 5
    # Structural SL/TP (strategy authored real levels) is much higher quality
    # than slider fallback.
    if template.risk.stop_loss_type in ("structural_pivot", "recent_swing_or_atr"):
        score += 8
    if template.risk.take_profit_type in ("structural_level", "risk_reward"):
        score += 5
    # Author explicitly set risk/RR.
    if template.risk.source == "user_strategy":
        score += 5

    # Penalise inferred-or-default critical fields.
    for f in template.inferred_fields:
        if classify_missing_field(f) == "critical":
            score -= 20
        elif classify_missing_field(f) == "inferable":
            score -= 5

    # Hard penalty for any unresolved critical missing field.
    for f in template.missing_fields:
        if classify_missing_field(f) == "critical":
            score -= 25

    # Conflicts → hard penalty.
    score -= 15 * len(template.conflicts)

    # Did the compile against dummy data actually produce a signal?
    if df is not None and _has_entry_trigger(df):
        score += 5
    elif df is not None:
        # Strategy compiled OK but populate_entry_trend produced no signal
        # under any conditions on the dummy series — usually means the rule
        # is so restrictive it's effectively dead, or there's a bug.
        score -= 10

    return max(0, min(100, score))


def _live_permission_for_score(score: int, missing_critical: bool, conflicts: bool) -> str:
    """Map confidence score → PDF §3.1 permission band."""
    if missing_critical or conflicts:
        return "blocked"
    if score >= 85:
        return "live_eligible"
    if score >= 70:
        return "demo_only"
    if score >= 50:
        return "backtest_only"
    return "blocked"


# ── Main entrypoint ──────────────────────────────────────────────────────

def validate_and_score(
    *,
    strategy_name:  str,
    strategy_id:    Optional[int],
    generated_code: str,
    description:    str = "",
    execution_timeframe: str = "15m",
    compiled_df:    Optional[pd.DataFrame] = None,
    strategy_class = None,
) -> StrategyTemplate:
    """Decode + resolve + score a strategy into a StrategyTemplate.

    Args:
        strategy_name:        Display name.
        strategy_id:          DB id (None for unsaved).
        generated_code:       The Python IStrategy class source.
        description:          Optional NL description from upload.
        execution_timeframe:  Engine TF the user picked.
        compiled_df:          Output of strategy_runner.evaluate_strategy
                              against a dummy dataframe — carries `.attrs`
                              with strategy_class, signal_columns, class_*.
        strategy_class:       The instantiated class (for reading
                              .timeframe / .can_short / .stoploss directly).

    Returns:
        StrategyTemplate with confidence_score, live_permission,
        missing_fields, inferred_fields, conflicts populated.
    """
    # ── 1. Read class attributes (defensive) ─────────────────────────────
    class_tf      = getattr(strategy_class, "timeframe", None) if strategy_class else None
    class_can_short = bool(getattr(strategy_class, "can_short", False)) if strategy_class else False
    class_sl_pct  = None
    class_tp_pct  = None
    if compiled_df is not None:
        class_sl_pct = compiled_df.attrs.get("class_stoploss_pct")
        class_tp_pct = compiled_df.attrs.get("class_take_profit_pct")
    signal_cols   = compiled_df.attrs.get("signal_columns", []) if compiled_df is not None else []
    has_structural = _strategy_has_structural_sl_tp(compiled_df) if compiled_df is not None else False

    # ── 2. Build conditions list (decoded indicators with role mapping) ──
    conditions: list[StrategyCondition] = []
    indicators = _decode_indicators(generated_code) if generated_code else []
    # Map indicator → likely role using strategy structure. This is a
    # heuristic; the goal is to give the UI a meaningful breakdown, not to
    # be 100% accurate.
    role_map = {
        "RSI":    "entry_trigger",     # most common: RSI cross triggers entry
        "MA":     "trend_filter",      # EMAs typically act as trend filters
        "MACD":   "entry_trigger",     # MACD crossovers are entry triggers
        "BBANDS": "setup_filter",      # BB acts as a zone/range filter
        "ATR":    "volatility_filter",
        "PIVOT":  "setup_filter",      # SMC pivot levels are setup zones
        "BOS":    "entry_trigger",     # break of structure = entry signal
        "FVG":    "setup_filter",      # FVG zone = setup filter
        "VOLUME": "volatility_filter",
    }
    for ind in indicators:
        conditions.append(StrategyCondition(
            role        = role_map.get(ind, "setup_filter"),
            source      = "inferred_from_strategy",
            timeframe   = class_tf or execution_timeframe,
            indicator   = ind,
            period      = None,
            rule        = None,
            value       = None,
            description = f"{ind} detected in strategy code",
        ))

    # If structural pivot levels are populated, add an explicit
    # structural-SL condition so the UI shows it.
    if has_structural:
        conditions.append(StrategyCondition(
            role        = "exit_signal",
            source      = "user_strategy",
            timeframe   = class_tf or execution_timeframe,
            indicator   = "STRUCTURAL_SL_TP",
            description = "Strategy populates sl_price / tp_price columns — engine honours them",
        ))

    # ── 3. Risk plan ─────────────────────────────────────────────────────
    mode = _mode_from_timeframe(execution_timeframe)
    defaults = _DEFAULTS_BY_MODE[mode]
    inferred: list[str] = []

    if class_sl_pct is not None:
        sl_type = "fixed_pct"
        sl_src  = "user_strategy"
    elif has_structural:
        sl_type = "structural_pivot"
        sl_src  = "user_strategy"
    else:
        sl_type = "atr_multiplier"
        sl_src  = "default_safe"
        inferred.append("stop_loss_type")

    if class_tp_pct is not None:
        tp_type = "fixed_pct"
        tp_src  = "user_strategy"
    elif has_structural:
        tp_type = "structural_level"
        tp_src  = "user_strategy"
    else:
        tp_type = "risk_reward"
        tp_src  = "default_safe"
        inferred.append("take_profit_type")

    # If both come from the user, the overall risk block is "user_strategy";
    # otherwise it's "default_safe" (PDF §7 — every default must be tagged).
    risk_source = "user_strategy" if (sl_src == "user_strategy" and tp_src == "user_strategy") else "default_safe"

    rr = defaults["risk_reward"]
    if class_sl_pct is not None and class_tp_pct is not None and class_sl_pct > 0:
        rr = round(class_tp_pct / class_sl_pct, 2)
    elif risk_source == "default_safe":
        inferred.append("risk_reward")

    risk = StrategyRisk(
        stop_loss_type      = sl_type,
        stop_loss_timeframe = class_tf or execution_timeframe,
        take_profit_type    = tp_type,
        risk_reward         = rr,
        risk_per_trade_pct  = defaults["risk_per_trade_pct"],
        source              = risk_source,
    )
    if risk.risk_per_trade_pct == defaults["risk_per_trade_pct"]:
        inferred.append("risk_per_trade_pct")

    # ── 4. Trade limits (always defaults — these are operational settings) ──
    trade_limits = StrategyTradeLimits(
        max_trades_per_day    = defaults["max_trades_per_day"],
        cooldown_candles      = defaults["cooldown_candles"],
        require_fresh_trigger = True,
        max_concurrent        = 1,
        source                = "default_safe",
    )
    inferred.extend(["max_trades_per_day", "cooldown_candles"])

    # ── 5. Direction + critical-field check ──────────────────────────────
    # Direction: prefer signal columns from compile (works without
    # strategy_class). Falls back to "long" only when neither source has info.
    if compiled_df is not None and ("enter_long" in compiled_df.columns or "enter_short" in compiled_df.columns):
        has_long  = "enter_long"  in compiled_df.columns
        has_short = "enter_short" in compiled_df.columns
        direction = "both" if (has_long and has_short) else ("long" if has_long else "short")
        if strategy_class is not None:
            # Refine using can_short if available.
            direction = _infer_direction(strategy_class, signal_cols)
    else:
        direction = _infer_direction(strategy_class, signal_cols) if strategy_class else "long"

    missing: list[str] = []
    if execution_timeframe is None or execution_timeframe == "":
        missing.append("execution_timeframe")

    # Entry trigger check: the strategy must DEFINE entry signal columns
    # via populate_entry_trend. We don't require them to fire on the dummy
    # series (some strategies need real volatility / specific patterns), but
    # the columns must EXIST after compile. If they don't, populate_entry_trend
    # either wasn't defined or threw — both are critical failures.
    if compiled_df is not None:
        has_entry_cols = ("enter_long" in compiled_df.columns or
                          "enter_short" in compiled_df.columns)
        if not has_entry_cols:
            missing.append("entry_trigger")
    elif strategy_class is None:
        # No compile artefact AND no class to introspect — can't verify entry trigger.
        missing.append("entry_trigger")

    # ── 6. Conflict detection ────────────────────────────────────────────
    conflicts = _detect_conflicts(generated_code) if generated_code else []

    # ── 7. Assemble template, then score + permission ────────────────────
    template = StrategyTemplate(
        strategy_name       = strategy_name,
        strategy_id         = strategy_id,
        description         = description or "",
        mode                = mode,
        direction           = direction,
        original_timeframe  = class_tf or execution_timeframe,
        execution_timeframe = execution_timeframe,
        market              = "BTC/USDT",
        conditions          = conditions,
        risk                = risk,
        trade_limits        = trade_limits,
        missing_fields      = missing,
        inferred_fields     = sorted(set(inferred)),
        conflicts           = conflicts,
        decoder_version     = "1.0",
        compiled_at         = datetime.now(timezone.utc).isoformat(),
    )

    score = _compute_confidence(template, compiled_df)
    has_critical_missing = any(
        classify_missing_field(f) == "critical" for f in template.missing_fields
    )
    permission = _live_permission_for_score(score, has_critical_missing, bool(conflicts))

    template.confidence_score = score
    template.live_permission  = permission

    # ── 8. Resolver notes (human-readable trail) ─────────────────────────
    notes: list[str] = []
    if template.inferred_fields:
        notes.append(
            f"Filled {len(template.inferred_fields)} field(s) with safe defaults: "
            f"{', '.join(template.inferred_fields[:5])}"
            f"{' (+more)' if len(template.inferred_fields) > 5 else ''}"
        )
    if template.missing_fields:
        notes.append(
            f"Critical missing field(s) — live blocked: {', '.join(template.missing_fields)}"
        )
    if conflicts:
        notes.append(f"⚠️ {len(conflicts)} potential logic conflict(s) detected")
    if has_structural:
        notes.append("Strategy provides structural SL/TP — engine will honour them")
    if compiled_df is not None and not _has_entry_trigger(compiled_df):
        notes.append(
            "Strategy compiled but produced no entries on dummy data — "
            "confirm it can fire under real market conditions before live use"
        )
    notes.append(f"Confidence score: {score}/100 → {permission}")
    template.resolver_notes = notes

    return template


# ── Lightweight wrapper that the bot create endpoint can call ────────────

def validate_for_live(
    *,
    strategy_name:  str,
    strategy_id:    Optional[int],
    generated_code: str,
    execution_timeframe: str,
    require_score: int = 85,
) -> tuple[bool, StrategyTemplate, str]:
    """Convenience wrapper used by the live guardrail.

    Compiles the strategy against a dummy dataframe, validates + scores,
    and returns (live_ok, template, reason_if_blocked).

    Even when live_ok=False the template is returned so the UI can render
    its preview block.
    """
    from .strategy_runner import evaluate_strategy

    if not generated_code or not generated_code.strip():
        # No code at all — can't be live-eligible.
        tpl = StrategyTemplate(strategy_name=strategy_name, strategy_id=strategy_id,
                               execution_timeframe=execution_timeframe,
                               missing_fields=["entry_trigger", "direction"],
                               confidence_score=0, live_permission="blocked",
                               resolver_notes=["Strategy has no generated_code — upload + parse first"])
        return False, tpl, "Strategy has no code to validate"

    try:
        dummy = pd.DataFrame({
            "date":  pd.date_range("2024-01-01", periods=400, freq="15min", tz="UTC"),
            "open":  [100.0 + i * 0.01 for i in range(400)],
            "high":  [101.0 + i * 0.01 for i in range(400)],
            "low":   [ 99.0 + i * 0.01 for i in range(400)],
            "close": [100.5 + i * 0.01 for i in range(400)],
            "vol":   [1000.0] * 400,
        })
        compiled_df = evaluate_strategy(
            generated_code, dummy,
            pair="BTC/USDT", execution_tf=execution_timeframe,
        )
    except Exception as e:
        tpl = StrategyTemplate(strategy_name=strategy_name, strategy_id=strategy_id,
                               execution_timeframe=execution_timeframe,
                               missing_fields=["entry_trigger"],
                               confidence_score=0, live_permission="blocked",
                               resolver_notes=[f"Strategy failed to compile: {e}"])
        return False, tpl, f"Strategy compile failed: {e}"

    # Find the class so we can read its attrs
    strategy_class = None
    try:
        # The strategy_runner sandbox exec'd the code; the class isn't in
        # this namespace, but we can find it by inspecting compiled_df.attrs
        # which carries `strategy_class` name.
        # To get the actual class object, re-run a thin variant — for now
        # we just pass None and rely on dataframe attrs for class_sl/tp pct.
        pass
    except Exception:
        pass

    template = validate_and_score(
        strategy_name       = strategy_name,
        strategy_id         = strategy_id,
        generated_code      = generated_code,
        description         = "",
        execution_timeframe = execution_timeframe,
        compiled_df         = compiled_df,
        strategy_class      = strategy_class,
    )

    if template.confidence_score < require_score or template.live_permission != "live_eligible":
        reason = (
            f"Live trading blocked: confidence={template.confidence_score}/100, "
            f"permission={template.live_permission}. " + " | ".join(template.resolver_notes[-3:])
        )
        return False, template, reason

    return True, template, ""
