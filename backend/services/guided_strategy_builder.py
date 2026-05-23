"""
PDF §4.1 — Guided Strategy Builder.

Synthesizes a complete IStrategy Python class from a structured form
payload, without going through the LLM. Lets a non-technical user fill
out:

  • Entry trigger     — indicator + condition (RSI < 30, MACD cross up,
                        EMA9 above EMA21, Bollinger lower touch, etc.)
  • Direction         — long / short / both
  • Stop-loss type    — fixed % | ATR-multiplier | structural pivot
  • Take-profit type  — fixed % | RR multiplier
  • Timeframe         — 1m / 5m / 15m / 30m / 1h / 4h / 1d
  • Risk per trade    — % of wallet
  • Bias filter (opt) — HTF EMA200 direction
  • Session filter (opt) — NY / London / 24h
  • Volatility filter (opt) — ATR min/max band

The resulting code is a real Freqtrade IStrategy subclass that runs
through the same strategy_runner + risk_engine + ARM pipeline as any
LLM-uploaded strategy. The validator marks it `live_eligible` whenever
the form is complete + the trader passes the standard guardrail
(confidence ≥ 85, backtest within 30 days).

This is intentionally template-based (string templates with safe
substitution) rather than AST-built, because:
  • A code template is reviewable by the user (we return it in the
    response so the strategy editor can show + edit it).
  • Template substitution gives us safe defaults for free.
  • LLM-grade flexibility isn't needed — the form covers the 90%
    most-common case.

Custom logic beyond what the form expresses goes through the existing
natural-language upload path (LLM).
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional


# ── Sanitisation helpers ────────────────────────────────────────────────

_VALID_CLASS_NAME = re.compile(r"[^A-Za-z0-9]+")
_VALID_TFS        = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "1d"}


def _safe_class_name(name: str) -> str:
    """Convert a user-provided strategy name into a valid Python identifier
    starting with a capital letter."""
    cleaned = _VALID_CLASS_NAME.sub("", (name or "MyStrategy").strip())
    if not cleaned:
        cleaned = "MyStrategy"
    if not cleaned[0].isalpha():
        cleaned = "S" + cleaned
    cleaned = cleaned[0].upper() + cleaned[1:]
    if not cleaned.endswith("Strategy"):
        cleaned += "Strategy"
    return cleaned


def _bias_filter_block(form: "GuidedForm") -> tuple[str, str]:
    """Return (indicators_code, filter_predicate). filter_predicate is a
    pandas expression string evaluated AFTER populate_indicators."""
    if not form.bias_filter or form.bias_filter == "none":
        return "", "True"
    if form.bias_filter == "htf_ema200_up":
        return (
            "        df[\"bias_ema200\"] = df[\"close\"].ewm(span=200, adjust=False).mean()\n",
            "(df[\"close\"] > df[\"bias_ema200\"])"
        )
    return "", "True"


def _entry_trigger_block(form: "GuidedForm") -> tuple[str, str, str]:
    """Return (indicator_code, long_predicate, short_predicate) for the
    chosen entry indicator. Predicates are pandas vector expressions."""
    e = form.entry_indicator
    p = form.entry_period or 14
    if e == "rsi_threshold":
        # rsi_threshold uses entry_value as the long-threshold (<) and
        # 100-entry_value as the short-threshold (>) by symmetry.
        thr = max(5.0, min(95.0, float(form.entry_value or 30.0)))
        return (
            f"        df[\"rsi\"] = ta.RSI(df, timeperiod={p})\n",
            f"(df[\"rsi\"] < {thr})",
            f"(df[\"rsi\"] > {100.0 - thr})",
        )
    if e == "macd_cross":
        return (
            "        macd = ta.MACD(df)\n"
            "        df[\"macd\"] = macd[\"macd\"]; df[\"macd_signal\"] = macd[\"macdsignal\"]\n",
            "((df[\"macd\"] > df[\"macd_signal\"]) & (df[\"macd\"].shift(1) <= df[\"macd_signal\"].shift(1)))",
            "((df[\"macd\"] < df[\"macd_signal\"]) & (df[\"macd\"].shift(1) >= df[\"macd_signal\"].shift(1)))",
        )
    if e == "ema_cross":
        fast = max(2, min(50, int(form.entry_period or 9)))
        slow = max(fast + 1, int(form.entry_value or 21))
        return (
            f"        df[\"ema_fast\"] = ta.EMA(df, timeperiod={fast})\n"
            f"        df[\"ema_slow\"] = ta.EMA(df, timeperiod={slow})\n",
            "((df[\"ema_fast\"] > df[\"ema_slow\"]) & (df[\"ema_fast\"].shift(1) <= df[\"ema_slow\"].shift(1)))",
            "((df[\"ema_fast\"] < df[\"ema_slow\"]) & (df[\"ema_fast\"].shift(1) >= df[\"ema_slow\"].shift(1)))",
        )
    if e == "bollinger_touch":
        std = max(1.0, min(4.0, float(form.entry_value or 2.0)))
        return (
            f"        bb = ta.BBANDS(df, timeperiod={p}, nbdevup={std}, nbdevdn={std})\n"
            f"        df[\"bb_upper\"] = bb[\"upperband\"]\n"
            f"        df[\"bb_lower\"] = bb[\"lowerband\"]\n",
            "(df[\"close\"] <= df[\"bb_lower\"])",
            "(df[\"close\"] >= df[\"bb_upper\"])",
        )
    # Default safe fallback — RSI 30/70
    return (
        f"        df[\"rsi\"] = ta.RSI(df, timeperiod={p})\n",
        "(df[\"rsi\"] < 30)",
        "(df[\"rsi\"] > 70)",
    )


def _session_filter_block(form: "GuidedForm") -> tuple[str, str]:
    """Return (indicator_code, predicate) for the session filter."""
    if not form.session_filter or form.session_filter == "24h":
        return "", "True"
    if form.session_filter == "ny":
        return (
            "        df[\"_hour_utc\"] = df[\"date\"].dt.hour\n",
            "((df[\"_hour_utc\"] >= 12) & (df[\"_hour_utc\"] <= 21))"
        )
    if form.session_filter == "london":
        return (
            "        df[\"_hour_utc\"] = df[\"date\"].dt.hour\n",
            "((df[\"_hour_utc\"] >= 7) & (df[\"_hour_utc\"] <= 16))"
        )
    return "", "True"


def _volatility_filter_block(form: "GuidedForm") -> tuple[str, str]:
    """Return (indicator_code, predicate) for the ATR-band volatility
    regime filter from PDF §4. When ON, the strategy only fires when
    current ATR sits in the middle band of the trailing 200-bar
    distribution. Skips dead-chop AND crash-vol regimes."""
    if not form.volatility_filter or form.volatility_filter == "none":
        return "", "True"
    if form.volatility_filter == "middle_band":
        lo  = max(0,  min(100, int(form.volatility_low_pct  or 25)))
        hi  = max(lo, min(100, int(form.volatility_high_pct or 75)))
        return (
            "        df[\"_atr14\"] = ta.ATR(df, timeperiod=14)\n"
            f"        df[\"_atr_lo\"]  = df[\"_atr14\"].rolling(200).quantile({lo/100})\n"
            f"        df[\"_atr_hi\"]  = df[\"_atr14\"].rolling(200).quantile({hi/100})\n",
            "((df[\"_atr14\"] >= df[\"_atr_lo\"]) & (df[\"_atr14\"] <= df[\"_atr_hi\"]))"
        )
    return "", "True"


def _exit_signal_block(form: "GuidedForm") -> tuple[str, str, str]:
    """Return (indicator_code, exit_long_predicate, exit_short_predicate)
    for the optional explicit exit signal. The engine still always
    honours SL/TP, but when an exit signal fires the position closes
    early at market."""
    if not form.exit_signal or form.exit_signal == "none":
        return "", "False", "False"
    p = int(form.exit_period or 14)
    if form.exit_signal == "rsi_neutral":
        # Close longs when RSI crosses back ABOVE 50; shorts when below 50.
        return (
            f"        if \"rsi\" not in df.columns:\n"
            f"            df[\"rsi\"] = ta.RSI(df, timeperiod={p})\n",
            "((df[\"rsi\"] > 50) & (df[\"rsi\"].shift(1) <= 50))",
            "((df[\"rsi\"] < 50) & (df[\"rsi\"].shift(1) >= 50))",
        )
    if form.exit_signal == "ema_cross_exit":
        # Close longs on bearish EMA cross; shorts on bullish.
        return (
            f"        if \"_exit_ema\" not in df.columns:\n"
            f"            df[\"_exit_ema_f\"] = ta.EMA(df, timeperiod={p})\n"
            f"            df[\"_exit_ema_s\"] = ta.EMA(df, timeperiod={p*2})\n",
            "((df[\"_exit_ema_f\"] < df[\"_exit_ema_s\"]) & (df[\"_exit_ema_f\"].shift(1) >= df[\"_exit_ema_s\"].shift(1)))",
            "((df[\"_exit_ema_f\"] > df[\"_exit_ema_s\"]) & (df[\"_exit_ema_f\"].shift(1) <= df[\"_exit_ema_s\"].shift(1)))",
        )
    return "", "False", "False"


# ── Public API ──────────────────────────────────────────────────────────

@dataclass
class GuidedForm:
    """Structured form payload from the wizard UI."""
    name:                    str
    timeframe:               str            = "15m"
    direction:               str            = "both"        # long | short | both
    # Entry trigger
    entry_indicator:         str            = "rsi_threshold"  # rsi_threshold | macd_cross | ema_cross | bollinger_touch
    entry_period:            Optional[int]  = 14
    entry_value:             Optional[float] = 30.0
    # Risk
    stoploss_type:           str            = "fixed_pct"   # fixed_pct | atr_multiplier (engine uses risk_engine for the latter)
    stoploss_pct:            float          = 2.0           # used when stoploss_type == fixed_pct
    take_profit_type:        str            = "risk_reward" # risk_reward | fixed_pct
    risk_reward:             float          = 2.0
    take_profit_pct:         float          = 4.0           # used when take_profit_type == fixed_pct
    risk_per_trade_pct:      float          = 0.5
    # Optional filters
    bias_filter:             Optional[str]  = "none"        # none | htf_ema200_up
    bias_timeframes:         Optional[list[str]] = None     # MTF analyzer opt-in
    session_filter:          Optional[str]  = "24h"         # 24h | ny | london
    # Volatility regime filter (PDF §4 — "trade only when ATR is in middle band")
    volatility_filter:       Optional[str]  = "none"        # none | middle_band
    volatility_low_pct:      Optional[int]  = 25            # percentile of trailing 200-bar ATR
    volatility_high_pct:     Optional[int]  = 75
    # Explicit exit signal (PDF §4 — beyond SL/TP)
    exit_signal:             Optional[str]  = "none"        # none | rsi_neutral | ema_cross_exit
    exit_period:             Optional[int]  = 14
    # ARM defaults (carried into the bot create form, NOT into the class itself)
    arm_enabled:             bool           = False
    arm_tp1_close_pct:       float          = 50.0

    @classmethod
    def from_dict(cls, payload: dict) -> "GuidedForm":
        # Coerce + validate against the field whitelist.
        def _f(k, d=None): return payload.get(k, d)
        return cls(
            name                = str(_f("name", "My Strategy")),
            timeframe           = (str(_f("timeframe", "15m")) if _f("timeframe", "15m") in _VALID_TFS else "15m"),
            direction           = str(_f("direction", "both")),
            entry_indicator     = str(_f("entry_indicator", "rsi_threshold")),
            entry_period        = (int(_f("entry_period")) if _f("entry_period") is not None else 14),
            entry_value         = (float(_f("entry_value")) if _f("entry_value") is not None else 30.0),
            stoploss_type       = str(_f("stoploss_type", "fixed_pct")),
            stoploss_pct        = max(0.1, min(20.0, float(_f("stoploss_pct", 2.0)))),
            take_profit_type    = str(_f("take_profit_type", "risk_reward")),
            risk_reward         = max(1.0, min(10.0, float(_f("risk_reward", 2.0)))),
            take_profit_pct     = max(0.1, min(50.0, float(_f("take_profit_pct", 4.0)))),
            risk_per_trade_pct  = max(0.05, min(10.0, float(_f("risk_per_trade_pct", 0.5)))),
            bias_filter         = str(_f("bias_filter", "none")),
            bias_timeframes     = list(_f("bias_timeframes") or []) or None,
            session_filter      = str(_f("session_filter", "24h")),
            volatility_filter   = str(_f("volatility_filter", "none")),
            volatility_low_pct  = (int(_f("volatility_low_pct"))  if _f("volatility_low_pct")  is not None else 25),
            volatility_high_pct = (int(_f("volatility_high_pct")) if _f("volatility_high_pct") is not None else 75),
            exit_signal         = str(_f("exit_signal", "none")),
            exit_period         = (int(_f("exit_period")) if _f("exit_period") is not None else 14),
            arm_enabled         = bool(_f("arm_enabled", False)),
            arm_tp1_close_pct   = max(1.0, min(99.0, float(_f("arm_tp1_close_pct", 50.0)))),
        )


def build_strategy_code(form: GuidedForm) -> str:
    """Render a complete IStrategy Python source for the given form."""
    cls_name = _safe_class_name(form.name)
    can_short = "True" if form.direction in ("short", "both") else "False"

    # Entry trigger
    ind_code, long_pred, short_pred = _entry_trigger_block(form)
    # Bias filter
    bias_code, bias_pred = _bias_filter_block(form)
    # Session filter
    sess_code, sess_pred = _session_filter_block(form)
    # Volatility regime filter (PDF §4)
    vol_code,  vol_pred  = _volatility_filter_block(form)
    # Optional explicit exit signal (PDF §4 — beyond SL/TP)
    exit_code, exit_long_pred, exit_short_pred = _exit_signal_block(form)

    # Combine predicates with the bias + session + volatility gates.
    bias_long  = bias_pred
    bias_short = bias_pred if bias_pred == "True" else f"(~{bias_pred})"
    full_long  = f"{long_pred} & {bias_long} & {sess_pred} & {vol_pred}"
    full_short = f"{short_pred} & {bias_short} & {sess_pred} & {vol_pred}"
    if form.direction == "long":
        full_short = "False"   # disable short rules
    if form.direction == "short":
        full_long = "False"    # disable long rules

    # Take-profit: when "risk_reward", express as a multiple of stoploss
    # (engine uses minimal_roi → stoploss × RR). When "fixed_pct" use the
    # explicit TP. ARM in the bot create form treats this as TP2.
    if form.take_profit_type == "risk_reward":
        tp_decimal = round(form.stoploss_pct * form.risk_reward / 100.0, 6)
    else:
        tp_decimal = round(form.take_profit_pct / 100.0, 6)
    sl_decimal = round(-abs(form.stoploss_pct) / 100.0, 6)

    bias_tfs_line = ""
    if form.bias_timeframes:
        tfs_list = [t for t in form.bias_timeframes if t in _VALID_TFS]
        if tfs_list:
            bias_tfs_line = (
                f"    bias_timeframes = {tfs_list!r}    "
                "# MTF analyzer opt-in (PDF §5)\n"
            )

    return f'''"""
{form.name} — built via the guided strategy wizard (PDF §4.1).

Generated from a structured form, not the LLM. Edit the form in the
Strategy → Upload Guided page, or edit this code directly in the
Strategy Editor; both round-trip cleanly.
"""
from freqtrade.strategy import IStrategy
from pandas import DataFrame
import talib.abstract as ta


class {cls_name}(IStrategy):
    timeframe   = "{form.timeframe}"
    stoploss    = {sl_decimal}
    minimal_roi = {{"0": {tp_decimal}}}
    can_short   = {can_short}
    startup_candle_count   = 220
    process_only_new_candles = True
{bias_tfs_line}
    def populate_indicators(self, df: DataFrame, metadata: dict) -> DataFrame:
{ind_code}{bias_code}{sess_code}{vol_code}{exit_code}        return df

    def populate_entry_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["enter_long"]  = 0
        df["enter_short"] = 0
        df.loc[
            {full_long},
            "enter_long",
        ] = 1
        df.loc[
            {full_short},
            "enter_short",
        ] = 1
        return df

    def populate_exit_trend(self, df: DataFrame, metadata: dict) -> DataFrame:
        df["exit_long"]  = 0
        df["exit_short"] = 0
        df.loc[
            {exit_long_pred},
            "exit_long",
        ] = 1
        df.loc[
            {exit_short_pred},
            "exit_short",
        ] = 1
        return df
'''
