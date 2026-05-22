"""
Timeframe Adapter — PDF §5.

Solves the "15m strategy ran on 1m execution" problem: when the user selects
an execution timeframe different from the one the strategy was authored
for, we don't blindly run the strategy on the wrong TF. Instead we:

  • Keep the original strategy logic running on its native TF as a
    BIAS / TREND / SETUP filter (closed-candle policy enforced).
  • Auto-generate a lightweight entry trigger on the SELECTED execution
    TF so the bot fires at scalp-speed when the HTF filter is bullish.
  • Compute SL/TP and risk per trade on the EXECUTION TF (matches the
    user's expectation that picking 1m gives scalp-sized stops).

This module is pure logic — no IO, no DB. It produces a "TimeframeBundle"
the engine uses to decide which candle series to fetch and which rules
to evaluate where. The actual fetch / cache is owned by `mtf_candles.py`.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# ── TF mapping table (PDF §5) ────────────────────────────────────────────
# Each row is: selected_execution_tf → (bias_tfs, setup_tfs, entry_tf, risk_tf).
# Multiple bias / setup TFs let the analyzer pick whichever is freshest
# (smallest closed candle whose close time is recent enough to be valid).

@dataclass(frozen=True)
class TFRoles:
    bias_tfs:  tuple[str, ...]
    setup_tfs: tuple[str, ...]
    entry_tf:  str
    risk_tf:   str

TF_ROLES: dict[str, TFRoles] = {
    "1m":  TFRoles(bias_tfs=("1h", "4h", "15m"), setup_tfs=("15m", "5m"),  entry_tf="1m",  risk_tf="1m"),
    "5m":  TFRoles(bias_tfs=("4h", "1h"),        setup_tfs=("15m",),       entry_tf="5m",  risk_tf="5m"),
    "15m": TFRoles(bias_tfs=("4h", "1d"),        setup_tfs=("1h",),        entry_tf="15m", risk_tf="15m"),
    "30m": TFRoles(bias_tfs=("1d", "4h"),        setup_tfs=("4h",),        entry_tf="30m", risk_tf="30m"),
    "1h":  TFRoles(bias_tfs=("1d", "1w"),        setup_tfs=("4h",),        entry_tf="1h",  risk_tf="1h"),
    "4h":  TFRoles(bias_tfs=("1w", "1d"),        setup_tfs=("1d",),        entry_tf="4h",  risk_tf="4h"),
    "1d":  TFRoles(bias_tfs=("1w",),             setup_tfs=("1w",),        entry_tf="1d",  risk_tf="1d"),
}


@dataclass
class TimeframeBundle:
    """The set of TFs the engine needs to keep warm for one bot.

    The engine's signal loop:
      1. Fetch all `required_timeframes` (deduped).
      2. Run the user's strategy on `strategy_tf` (its native).
      3. If `adapter_active` is True: also run the auto-entry trigger on
         `entry_tf`, gated by the HTF bias condition's result.
      4. Compute SL/TP from `risk_tf` ATR (handed off to risk_engine).
    """
    execution_tf:        str
    strategy_tf:         str                          # what the strategy authored for
    entry_tf:            str
    risk_tf:             str
    bias_tfs:            tuple[str, ...] = ()
    setup_tfs:           tuple[str, ...] = ()
    adapter_active:      bool            = False     # True when strategy_tf != execution_tf
    htf_filter_summary:  str             = ""        # human-readable: "long-only while 15m close > 15m EMA200"
    notes:               list[str]       = field(default_factory=list)

    @property
    def required_timeframes(self) -> list[str]:
        """All TFs the engine should fetch + cache for this bot."""
        seen: list[str] = []
        for tf in (self.strategy_tf, self.entry_tf, self.risk_tf, *self.bias_tfs, *self.setup_tfs):
            if tf and tf not in seen:
                seen.append(tf)
        return seen


def adapt(strategy_tf: str, execution_tf: str) -> TimeframeBundle:
    """Return the TimeframeBundle for `execution_tf`, treating `strategy_tf`
    as a higher-TF bias filter when they differ (PDF §5 "Important" callout).

    When they MATCH (e.g. strategy is native-15m and user picked 15m),
    adapter_active=False — no auto-trigger, no extra fetch. The legacy
    code path keeps working unchanged.
    """
    roles = TF_ROLES.get(execution_tf) or TF_ROLES["15m"]
    same_tf = (strategy_tf or execution_tf) == execution_tf
    if same_tf:
        return TimeframeBundle(
            execution_tf   = execution_tf,
            strategy_tf    = strategy_tf or execution_tf,
            entry_tf       = roles.entry_tf,
            risk_tf        = roles.risk_tf,
            bias_tfs       = (),         # no HTF analysis when on native TF
            setup_tfs      = (),
            adapter_active = False,
            notes          = [f"Strategy and execution both on {execution_tf} — no TF adaptation needed."],
        )

    # Mismatched: the strategy's native TF becomes a HTF bias filter.
    # We don't replace strategy rules — we layer the auto-entry on top.
    # Pick the SMALLEST bias TF that's still strictly LARGER than the
    # execution TF (so HTF means HIGHER, not coincident).
    bias_choice = tuple(tf for tf in roles.bias_tfs if _tf_minutes(tf) > _tf_minutes(execution_tf))
    if (strategy_tf and _tf_minutes(strategy_tf) > _tf_minutes(execution_tf)
            and strategy_tf not in bias_choice):
        # Prepend the strategy's native TF — it IS the right HTF bias.
        bias_choice = (strategy_tf,) + bias_choice

    summary_dir = ("LONG-only while strategy gives a long bias on "
                   + strategy_tf + "; SHORT-only on opposite") if strategy_tf else ""
    return TimeframeBundle(
        execution_tf        = execution_tf,
        strategy_tf         = strategy_tf or execution_tf,
        entry_tf            = roles.entry_tf,
        risk_tf             = roles.risk_tf,
        bias_tfs            = bias_choice,
        setup_tfs           = roles.setup_tfs,
        adapter_active      = True,
        htf_filter_summary  = summary_dir,
        notes               = [
            f"Strategy was authored on {strategy_tf}, but execution TF is {execution_tf}.",
            f"Strategy logic runs on {strategy_tf} as HTF bias filter (closed-candle policy).",
            f"Auto entry-trigger fires on {execution_tf}; SL/TP scaled to {roles.risk_tf} ATR.",
        ],
    )


def _tf_minutes(tf: str) -> int:
    """Convert KuCoin TF strings to minutes for ordering."""
    units = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    if not tf:
        return 0
    suffix = tf[-1].lower()
    try:
        n = int(tf[:-1])
    except (ValueError, TypeError):
        return 0
    return n * units.get(suffix, 1)


def tf_mismatch_warning(strategy_tf: Optional[str], execution_tf: str) -> Optional[str]:
    """Return a UI warning string when there's a TF mismatch the user
    might not have realised. None when TFs align."""
    if not strategy_tf or strategy_tf == execution_tf:
        return None
    s_min = _tf_minutes(strategy_tf)
    e_min = _tf_minutes(execution_tf)
    if s_min == e_min:
        return None
    if s_min > e_min:
        return (
            f"⚠ Strategy was authored for {strategy_tf} but you picked {execution_tf} for execution. "
            f"The adapter will run {strategy_tf} rules as a higher-timeframe bias filter and fire "
            f"entries on {execution_tf}. Backtest the combination before going live."
        )
    return (
        f"⚠ Strategy was authored for {strategy_tf} but you picked {execution_tf} (a HIGHER TF). "
        f"The strategy's rules may fire too rarely on {execution_tf}. Consider switching to a "
        f"timeframe ≤ {strategy_tf}."
    )
