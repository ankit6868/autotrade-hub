"""
Structured Strategy Template — the strict schema every strategy is normalised
into before it can run on the AutoTrade Hub execution engine.

This module is the heart of "Phase 5 — Strategy Understanding Layer" from the
hybrid-engine architecture handoff. The pipeline is:

    Raw strategy upload
      → Strategy Decoder/Compiler (strategy_parser.py — emits Python IStrategy
        code + the StrategyTemplate JSON object defined HERE)
      → Missing-Rule Resolver (strategy_validator.py — classifies missing
        fields into critical / inferable / non-critical, fills safe defaults,
        flags source as user_strategy / inferred / default)
      → Confidence Validator (strategy_validator.py — 0-100 score)
      → Live Guardrail (futures.py /bots POST — blocks live trading unless
        confidence ≥ 85, no critical missing fields, backtest passed)

No part of this module talks to the network, the DB, or the LLM — it is a
pure dataclass + validation layer so it can be unit-tested cheaply and
reused by both the live engine and the backtester.

The schema matches PDF §8 ("Structured Strategy Object for Developer")
field-for-field; deviations are documented inline.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from typing import Optional, Literal


# ── Vocabulary types ─────────────────────────────────────────────────────
# Every condition carries a `role` (what the rule's job is in the pipeline)
# and a `source` (who put it there — user, decoder inference, or safe default).
# Keeping these as Literal[...] gives editors autocomplete and lets us reject
# nonsense values cheaply at parse time.

RuleRole   = Literal[
    "bias_filter",        # higher-TF directional gate (e.g. 4H RSI > 50 for longs)
    "trend_filter",       # secondary trend confirmation (e.g. price > EMA200)
    "setup_filter",       # zone / range condition (e.g. inside premium/discount)
    "entry_trigger",      # the actual fire — crossover, breakout, candle pattern
    "exit_signal",        # optional rule to close before SL/TP
    "session_filter",     # NY / London / 24h time window
    "volatility_filter",  # ATR-band or volume gate
]
RuleSource = Literal[
    "user_strategy",          # author wrote it explicitly
    "inferred_from_strategy", # decoder pulled it from natural language / class fields
    "inferred_for_execution", # adapter added it because the user's selected execution TF needs one
    "default_safe",           # safe-default filled by the Missing-Rule Resolver
]
TradeMode  = Literal["scalp", "intraday", "swing", "position", "auto"]
Direction  = Literal["long", "short", "both"]
SLType     = Literal["recent_swing_or_atr", "fixed_pct", "structural_pivot", "atr_multiplier"]
TPType     = Literal["risk_reward", "fixed_pct", "structural_level", "atr_multiplier", "multi_tp"]
BEMode     = Literal["leverage", "manual_pct", "entry", "none"]


# ── Condition: one decoded rule (indicator/level/signal) ─────────────────

@dataclass
class StrategyCondition:
    """One atomic rule from the decoded strategy.

    Fields mirror PDF §8's example payload. Optional fields stay None when
    the source strategy didn't specify them (e.g. a candle-pattern entry
    rule has no `period` or `value`).
    """
    role:       str                                 # RuleRole — kept as str for JSON round-trip
    source:     str                                 # RuleSource
    timeframe:  str                                 # "1m" | "5m" | "15m" | "30m" | "1h" | "4h" | "1d"
    indicator:  Optional[str]   = None              # "RSI" | "EMA" | "MACD" | "BBANDS" | "ATR" | "PIVOT" | ...
    period:     Optional[int]   = None              # lookback period for the indicator
    rule:       Optional[str]   = None              # "above" | "below" | "cross_above" | "cross_below" | "price_above" | "in_range" | ...
    value:      Optional[float] = None              # threshold for the comparison (None for relative rules)
    description: Optional[str]  = None              # human-readable summary for the UI


# ── Risk plan + trade limits ──────────────────────────────────────────────

@dataclass
class StrategyRisk:
    """Risk-management section of the template. Maps PDF §8's `risk`."""
    stop_loss_type:        str    = "atr_multiplier"   # SLType
    stop_loss_timeframe:   str    = "15m"
    take_profit_type:      str    = "risk_reward"      # TPType
    risk_reward:           float  = 2.0
    risk_per_trade_pct:    float  = 0.5                # % of wallet per trade
    source:                str    = "default_safe"     # RuleSource
    # ARM (Advanced Risk Management) section — when arm_enabled is True the
    # engine treats the strategy's TP as TP2 and books partial at midpoint.
    arm_enabled:           bool   = False
    arm_tp1_close_pct:     float  = 50.0
    arm_be_mode:           str    = "leverage"         # BEMode
    arm_be_buffer_pct:     float  = 1.0
    arm_trail_to_tp1:      bool   = True


@dataclass
class StrategyTradeLimits:
    """Operational limits: cooldown, max trades/day, re-entry. PDF §8."""
    max_trades_per_day:    int   = 5                   # safe default — refined by mode below
    cooldown_candles:      int   = 3                   # bars to wait after a close before re-entering same direction
    require_fresh_trigger: bool  = True                # block stale-signal re-entry
    max_concurrent:        int   = 1                   # 1 = TradingView pyramiding=0
    source:                str   = "default_safe"


# ── The full template ────────────────────────────────────────────────────

@dataclass
class StrategyTemplate:
    """Strict, machine-readable representation of a strategy.

    Every strategy that wants to run on this platform (paper, live, or
    backtest) MUST be normalisable into a StrategyTemplate. The validator
    refuses to mark a template as live-eligible until critical fields are
    populated AND the confidence score is ≥85.
    """
    # Identity
    strategy_name:         str
    strategy_id:           Optional[int] = None
    description:           str           = ""

    # Trading model
    mode:                  str           = "intraday"      # TradeMode
    direction:             str           = "both"          # Direction
    original_timeframe:    str           = "15m"           # what the user authored on
    execution_timeframe:   str           = "15m"           # what the engine should actually tick on
    market:                str           = "BTC/USDT"      # default pair — multi-pair handled at engine level

    # Decoded rules
    conditions:            list[StrategyCondition] = field(default_factory=list)

    # Risk + trade limits
    risk:                  StrategyRisk            = field(default_factory=StrategyRisk)
    trade_limits:          StrategyTradeLimits     = field(default_factory=StrategyTradeLimits)

    # Resolver output — set by strategy_validator.py
    confidence_score:      int          = 0
    live_permission:       str          = "blocked"        # 'live_eligible' | 'demo_only' | 'backtest_only' | 'blocked'
    missing_fields:        list[str]    = field(default_factory=list)
    inferred_fields:       list[str]    = field(default_factory=list)
    conflicts:             list[str]    = field(default_factory=list)
    resolver_notes:        list[str]    = field(default_factory=list)

    # Metadata
    decoder_version:       str          = "1.0"
    compiled_at:           Optional[str] = None             # ISO timestamp

    # ── Serialisation ─────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        """Round-trip serialisation for DB storage and API responses."""
        return asdict(self)

    def to_json(self) -> str:
        """Compact JSON for DB column storage."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict) -> "StrategyTemplate":
        """Inverse of to_dict — used when loading from DB / API."""
        conds = [StrategyCondition(**c) for c in data.get("conditions", [])]
        risk_data = data.get("risk", {}) or {}
        limits_data = data.get("trade_limits", {}) or {}
        return cls(
            strategy_name       = data.get("strategy_name", ""),
            strategy_id         = data.get("strategy_id"),
            description         = data.get("description", ""),
            mode                = data.get("mode", "intraday"),
            direction           = data.get("direction", "both"),
            original_timeframe  = data.get("original_timeframe", "15m"),
            execution_timeframe = data.get("execution_timeframe", "15m"),
            market              = data.get("market", "BTC/USDT"),
            conditions          = conds,
            risk                = StrategyRisk(**risk_data),
            trade_limits        = StrategyTradeLimits(**limits_data),
            confidence_score    = int(data.get("confidence_score", 0)),
            live_permission     = data.get("live_permission", "blocked"),
            missing_fields      = list(data.get("missing_fields", [])),
            inferred_fields     = list(data.get("inferred_fields", [])),
            conflicts           = list(data.get("conflicts", [])),
            resolver_notes      = list(data.get("resolver_notes", [])),
            decoder_version     = data.get("decoder_version", "1.0"),
            compiled_at         = data.get("compiled_at"),
        )

    # ── Convenience helpers used by the engine ────────────────────────────

    def has_role(self, role: str) -> bool:
        """True iff at least one condition with this role exists."""
        return any(c.role == role for c in self.conditions)

    def conditions_for(self, role: str) -> list[StrategyCondition]:
        """All conditions with the given role, in declaration order."""
        return [c for c in self.conditions if c.role == role]

    def timeframes_used(self) -> set[str]:
        """All distinct timeframes referenced by conditions — for the
        multi-timeframe analyzer (Phase 7) to know which candle series
        to keep warm."""
        return {c.timeframe for c in self.conditions}

    def is_live_eligible(self) -> bool:
        """Convenience: matches the live guardrail's accept condition."""
        return (
            self.live_permission == "live_eligible"
            and self.confidence_score >= 85
            and not self.missing_fields
            and not self.conflicts
        )


# ── Field categorisation (PDF §3) ─────────────────────────────────────────
# Used by the Missing-Rule Resolver to decide what to do when a field is
# absent from the decoded strategy.
#
#   CRITICAL_FIELDS   → block live entirely until user fills them
#   INFERABLE_FIELDS  → fill with safe default + restrict to demo/backtest
#                       until validation passes
#   NON_CRITICAL      → fill with safe default + still live-eligible
#
# Order matches PDF §4 ("Strategy Upload Requirements") row by row.

CRITICAL_FIELDS = {
    # No clear direction → can't trade safely
    "direction",
    # No entry trigger → bot has nothing to fire on
    "entry_trigger",
    # No execution timeframe → no idea how often to scan
    "execution_timeframe",
}

INFERABLE_FIELDS = {
    "stop_loss_type",
    "take_profit_type",
    "risk_per_trade_pct",
    "bias_filter",          # higher-TF directional rule
    "setup_filter",
}

NON_CRITICAL_FIELDS = {
    "cooldown_candles",
    "max_trades_per_day",
    "max_concurrent",
    "arm_tp1_close_pct",
    "arm_be_mode",
    "arm_be_buffer_pct",
    "arm_trail_to_tp1",
    "session_filter",
    "volatility_filter",
}


def classify_missing_field(field_name: str) -> str:
    """Return 'critical' | 'inferable' | 'non_critical' | 'unknown'."""
    if field_name in CRITICAL_FIELDS:
        return "critical"
    if field_name in INFERABLE_FIELDS:
        return "inferable"
    if field_name in NON_CRITICAL_FIELDS:
        return "non_critical"
    return "unknown"
