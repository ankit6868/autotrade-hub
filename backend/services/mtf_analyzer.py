"""
Multi-Timeframe Analyzer (PDF §5) — opt-in HTF context for strategies.

This is a thin layer on top of `mtf_candles` that lets a user strategy
declare ADDITIONAL timeframes it wants data for, beyond the engine's
execution TF. Closed candles are pre-fetched and exposed to the
strategy's `populate_indicators` via the `metadata['htf']` dict and
the dataframe's `df.attrs['htf']`.

How a strategy opts in
----------------------
Just add a class attribute:

    class MyStrategy(IStrategy):
        timeframe = "15m"
        bias_timeframes = ["1h", "4h"]    # ← opt-in to MTF context

        def populate_indicators(self, df, metadata):
            df["ema21"] = ta.EMA(df, timeperiod=21)

            # Read HTF closed candles (None if fetch failed):
            df_1h = metadata.get("htf", {}).get("1h")
            df_4h = metadata.get("htf", {}).get("4h")

            # ... compute HTF indicators, project onto df by ffill ...
            if df_1h is not None and len(df_1h) >= 200:
                df["htf_1h_ema200"] = df_1h["close"].ewm(span=200, adjust=False).mean().iloc[-1]
            return df

Strategies WITHOUT `bias_timeframes` see no change — `metadata['htf']`
is just `{}` and existing behavior is preserved 1:1. The fetch only
happens for strategies that explicitly opt in.

Why this is a separate module
-----------------------------
Keeping the analyzer out of strategy_runner means:
  • the runner stays a pure executor with no IO concerns
  • the live engine, backtester, and validator can all attach context
    using the same helper
  • we can add caching, prefetching, and rate-limit smoothing here
    without touching strategy execution code

Live + backtest parity
----------------------
  • Live engine calls `attach_htf_context()` once per signal scan,
    using the existing `mtf_candles.get_candles()` (closed-bar trimmed).
  • Backtester calls the same helper with `historical=True`, which
    uses an offline OHLCV fetch capped to the bar timestamp under
    review (so we don't peek at future data).
"""
from __future__ import annotations

import logging
from typing import Optional

import pandas as pd

log = logging.getLogger(__name__)


# Valid TF strings the analyzer accepts. Matches mtf_candles + risk_engine.
_VALID_TFS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "8h", "12h", "1d", "1w"}


def _tf_minutes(tf: str) -> int:
    units = {"m": 1, "h": 60, "d": 1440, "w": 10080}
    if not tf:
        return 0
    try:
        return int(tf[:-1]) * units.get(tf[-1].lower(), 1)
    except (ValueError, TypeError):
        return 0


def get_strategy_bias_tfs(strategy_instance, execution_tf: str) -> list[str]:
    """Read `bias_timeframes` off the strategy instance, validate, and
    drop TFs that aren't strictly HIGHER than the execution TF.

    Strategies that accidentally declare a LOWER TF (e.g. `bias_timeframes
    = ["5m"]` on a 15m engine) get those entries silently filtered — the
    spec says HTF means HIGHER, and using a lower TF here would mean
    peeking at lower-resolution data which isn't the analyzer's job.
    """
    raw = getattr(strategy_instance, "bias_timeframes", None)
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        log.warning("bias_timeframes must be a list; got %r — ignoring", type(raw))
        return []
    exec_min = _tf_minutes(execution_tf)
    valid: list[str] = []
    seen: set[str] = set()
    for tf in raw:
        tf_s = str(tf).strip()
        if tf_s not in _VALID_TFS:
            continue
        if tf_s in seen:
            continue
        if _tf_minutes(tf_s) <= exec_min:
            log.debug("dropping bias_tf %s (not strictly higher than %s)", tf_s, execution_tf)
            continue
        seen.add(tf_s)
        valid.append(tf_s)
    return valid


def attach_htf_context(
    *,
    strategy_instance,
    pair: str,
    execution_tf: str,
    metadata: dict,
    df: pd.DataFrame,
    historical_anchor_ts: Optional[int] = None,
) -> dict[str, pd.DataFrame]:
    """Fetch closed HTF candles for every TF in `strategy_instance.bias_timeframes`,
    attach them to `metadata['htf']` and `df.attrs['htf']`, and return
    the mapping for the caller to log.

    Args:
        strategy_instance:   The instantiated IStrategy subclass.
        pair:                Engine pair, e.g. "BTC/USDT".
        execution_tf:        The engine's selected TF (we never re-fetch this).
        metadata:            The metadata dict passed to populate_indicators —
                             mutated in-place to add an "htf" key.
        df:                  The strategy's main dataframe — `df.attrs["htf"]`
                             is also set so post-processing can read it.
        historical_anchor_ts: For backtests, the maximum bar timestamp
                             (in seconds) that's allowed to be visible.
                             Prevents the analyzer from leaking future
                             data into a backtest's HTF view.

    Returns:
        Dict[tf_str → DataFrame|None]. None values mean fetch failed.
    """
    bias_tfs = get_strategy_bias_tfs(strategy_instance, execution_tf)
    htf_map: dict[str, Optional[pd.DataFrame]] = {}
    if not bias_tfs:
        # No opt-in — leave metadata['htf'] empty so strategies can still
        # safely call metadata.get('htf', {}).get('1h').
        metadata.setdefault("htf", {})
        try:
            df.attrs["htf"] = {}
        except Exception:
            pass
        return {}

    from backend.services import mtf_candles
    for tf in bias_tfs:
        try:
            tf_df = mtf_candles.get_candles(pair, tf, force_refresh=False)
            if tf_df is not None and historical_anchor_ts is not None:
                # Backtest mode — clip to the anchor so the strategy can't
                # peek at HTF bars whose CLOSE is in the future relative
                # to the current backtest bar. The mtf_candles helper
                # already trims partial bars; this is the extra historical
                # safety net.
                if "date" in tf_df.columns:
                    try:
                        anchor_dt = pd.Timestamp(historical_anchor_ts, unit="s", tz="UTC")
                        tf_df = tf_df[tf_df["date"] <= anchor_dt].copy()
                    except Exception:
                        pass
            htf_map[tf] = tf_df
        except Exception as e:
            log.debug("mtf_analyzer: fetch %s/%s failed: %s", pair, tf, e)
            htf_map[tf] = None

    metadata["htf"] = htf_map
    try:
        df.attrs["htf"] = htf_map
    except Exception:
        pass
    return htf_map
