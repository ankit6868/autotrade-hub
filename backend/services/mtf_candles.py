"""
Multi-Timeframe Candle Service — PDF §6 closed-candle policy.

Per-symbol, per-TF dataframe cache the engine uses when the Timeframe
Adapter says it needs HTF bias data. Implements two safety rules from
the spec:

  1. HTF candles must be CLOSED — never evaluate filters on a candle
     that's still forming, otherwise the "bias is bullish" verdict can
     flip mid-bar. We achieve this by slicing off the last partial bar
     whenever the latest candle's timestamp falls within the current
     wall-clock window for that TF.

  2. Refresh per-TF only when the TF's candle window has rolled over —
     no point re-fetching 4h candles every 60 seconds.

Keeps a global LRU-style cache keyed by (symbol_kucoin, tf). Thread-safe
via a single module-level lock.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

import pandas as pd

from .native_trading_engine import _fetch_candles, _build_df

log = logging.getLogger(__name__)


# ── Cache machinery ──────────────────────────────────────────────────────

_CACHE: dict[tuple[str, str], dict] = {}     # (symbol, tf) → {df, last_fetch_ts}
_LOCK = threading.Lock()
_MAX_AGE_SECS = {
    "1m":  60,
    "5m":  60 * 4,
    "15m": 60 * 14,
    "30m": 60 * 25,
    "1h":  60 * 55,
    "4h":  60 * 60 * 3,
    "1d":  60 * 60 * 20,
    "1w":  60 * 60 * 24 * 6,
}


def _tf_seconds(tf: str) -> int:
    """KuCoin TF string → seconds per bar."""
    units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    if not tf:
        return 60
    try:
        return int(tf[:-1]) * units.get(tf[-1].lower(), 60)
    except Exception:
        return 60


def _trim_partial_bar(df: pd.DataFrame, tf: str) -> pd.DataFrame:
    """Drop the most recent row if it represents a candle that hasn't
    closed yet (partial bar). Implements PDF §6 closed-candle policy.

    KuCoin's klines API returns the in-progress bar with the current time
    as its `date`. We check whether the last bar's open-time + tf_seconds
    is still in the future — if yes, it's partial, drop it.
    """
    if df is None or df.empty:
        return df
    last_ts = df.iloc[-1]["date"]
    try:
        # `date` is a pandas Timestamp; convert to epoch seconds.
        last_epoch = last_ts.timestamp() if hasattr(last_ts, "timestamp") else pd.Timestamp(last_ts).timestamp()
    except Exception:
        return df
    tf_sec = _tf_seconds(tf)
    now_epoch = time.time()
    # A bar is "closed" only once `last_epoch + tf_sec` is in the past.
    if last_epoch + tf_sec > now_epoch:
        return df.iloc[:-1].copy()
    return df


def get_candles(symbol_pair: str, tf: str, *, force_refresh: bool = False) -> Optional[pd.DataFrame]:
    """Fetch closed-only candles for (symbol, tf) with caching.

    Args:
        symbol_pair:   "BTC/USDT" — converted to BTC-USDT for the KuCoin call.
        tf:            "1m" / "5m" / "15m" / "30m" / "1h" / "4h" / "1d" / "1w"
        force_refresh: bypass cache (used for one-shot refreshes from /preview).

    Returns:
        A pandas DataFrame with columns [date, open, high, low, close, vol]
        where the LAST row is the most recent CLOSED bar (the partial bar
        is trimmed). Returns None on fetch failure.
    """
    sym_kc = symbol_pair.replace("/", "-")
    key    = (sym_kc, tf)
    now    = time.time()
    max_age = _MAX_AGE_SECS.get(tf, 60)

    with _LOCK:
        cached = _CACHE.get(key)
        if cached and not force_refresh and (now - cached["last_fetch_ts"]) < max_age:
            return cached["df"]

    # Cache miss / stale — fetch fresh.
    try:
        candles = _fetch_candles(sym_kc, tf)
    except Exception as e:
        log.warning("mtf_candles: fetch %s/%s failed: %s", sym_kc, tf, e)
        # Return stale cache if we have one — better than nothing.
        with _LOCK:
            cached = _CACHE.get(key)
            return cached["df"] if cached else None
    if not candles:
        return None

    df = _build_df(candles)
    df = _trim_partial_bar(df, tf)
    with _LOCK:
        _CACHE[key] = {"df": df, "last_fetch_ts": now}
    return df


def get_last_closed_close(symbol_pair: str, tf: str) -> Optional[float]:
    """Convenience: most recent CLOSED bar's close price on this TF."""
    df = get_candles(symbol_pair, tf)
    if df is None or df.empty:
        return None
    return float(df.iloc[-1]["close"])


def htf_bias_long(symbol_pair: str, tf: str, *, ema_period: int = 200) -> Optional[bool]:
    """Return True iff CLOSED price on `tf` is above EMA(ema_period).

    Default 200-EMA is the industry standard for long-term bias. Returns
    None on insufficient data (need ≥ ema_period+1 closed bars).

    This is the primary HTF filter used by the Timeframe Adapter when the
    user picks a smaller execution TF than the strategy's native TF.
    """
    df = get_candles(symbol_pair, tf)
    if df is None or len(df) < ema_period + 1:
        return None
    close = df["close"].astype(float)
    ema = close.ewm(span=ema_period, adjust=False).mean()
    return bool(close.iloc[-1] > ema.iloc[-1])


def htf_bias_summary(symbol_pair: str, tfs: list[str], ema_period: int = 200) -> dict:
    """Summarise HTF bias across multiple TFs. Used by /api/strategy/preview
    and by the engine's signal-trace diagnostic on every fire."""
    out: dict[str, str] = {}
    for tf in tfs:
        verdict = htf_bias_long(symbol_pair, tf, ema_period=ema_period)
        if verdict is None:
            out[tf] = "unknown"
        elif verdict:
            out[tf] = "bullish"
        else:
            out[tf] = "bearish"
    return out


def clear_cache() -> int:
    """Drop everything. For tests and admin-triggered refresh."""
    with _LOCK:
        n = len(_CACHE)
        _CACHE.clear()
    return n
