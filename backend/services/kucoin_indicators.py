"""
KuCoin-backed indicator fetcher
================================
Replaces tradingview_ta (which is aggressively IP-rate-limited) with direct
calls to KuCoin's *public* kline endpoint. We pull OHLCV bars and compute
RSI / MACD / ADX / Bollinger / EMA / SMA / ATR locally with TA-Lib.

Why KuCoin:
 * It is the exchange the bot actually trades on, so the data matches exactly.
 * The kline endpoint is public (no auth, no CORS), very high rate limits
   (roughly 1800 req/min per IP).
 * One HTTP round-trip gives us everything we need to compute any classical
   indicator, and we do it in-process instead of screen-scraping.

Public API is the same surface as tv_fetcher so callers can swap drop-in:
    fetch(pair, timeframe)     -> dict | None
    fetch_many(pairs, tf, ...) -> dict[pair, dict | None]
    status()                   -> dict
    clear_cache()              -> int
"""

from __future__ import annotations

import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

import httpx
import numpy as np
import pandas as pd


# Pure-numpy/pandas indicator implementations. Avoids the TA-Lib C dependency
# (TA-Lib has no Python 3.14 wheels on Windows).
def _rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    s = pd.Series(close, dtype=float)
    delta = s.diff()
    up = delta.clip(lower=0.0)
    down = -delta.clip(upper=0.0)
    roll_up = up.ewm(alpha=1 / period, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / period, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    return (100 - (100 / (1 + rs))).to_numpy()


def _ema(arr: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(arr, dtype=float).ewm(span=period, adjust=False).mean().to_numpy()


def _sma(arr: np.ndarray, period: int) -> np.ndarray:
    return pd.Series(arr, dtype=float).rolling(period).mean().to_numpy()


def _macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal: int = 9):
    fast_ema = _ema(close, fast)
    slow_ema = _ema(close, slow)
    macd_line = fast_ema - slow_ema
    sig = _ema(macd_line, signal)
    hist = macd_line - sig
    return macd_line, sig, hist


def _bbands(close: np.ndarray, period: int = 20, stds: float = 2.0):
    s = pd.Series(close, dtype=float)
    middle = s.rolling(period).mean()
    std = s.rolling(period).std(ddof=0)
    upper = middle + stds * std
    lower = middle - stds * std
    return upper.to_numpy(), middle.to_numpy(), lower.to_numpy()


def _true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    h, l, c = pd.Series(high), pd.Series(low), pd.Series(close)
    prev_close = c.shift(1)
    tr = pd.concat([(h - l).abs(), (h - prev_close).abs(), (l - prev_close).abs()], axis=1).max(axis=1)
    return tr.to_numpy()


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    tr = _true_range(high, low, close)
    return pd.Series(tr).ewm(alpha=1 / period, adjust=False).mean().to_numpy()


def _adx(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    h = pd.Series(high, dtype=float)
    l = pd.Series(low, dtype=float)
    up_move = h.diff()
    down_move = -l.diff()
    plus_dm = pd.Series(np.where((up_move > down_move) & (up_move > 0), up_move, 0.0))
    minus_dm = pd.Series(np.where((down_move > up_move) & (down_move > 0), down_move, 0.0))
    atr = pd.Series(_atr(high, low, close, period))
    plus_di = 100 * (plus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    minus_di = 100 * (minus_dm.ewm(alpha=1 / period, adjust=False).mean() / atr.replace(0, np.nan))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
    return dx.ewm(alpha=1 / period, adjust=False).mean().to_numpy()

# --- Tunables ---------------------------------------------------------------
CACHE_TTL_SECONDS = 60          # klines refresh quickly; 1 min cache is ample
STALE_GRACE_SECONDS = 30 * 60   # still serve (flagged) for 30 min on failure
REQUEST_TIMEOUT = 8.0           # KuCoin is usually sub-second
MAX_RETRIES = 2
BACKOFF_BASE = 0.3
BACKOFF_CAP = 1.2
KLINE_COUNT = 200               # enough for 50-period SMA + warmup

KUCOIN_BASE = "https://api.kucoin.com"

# Map freqtrade-style timeframes -> KuCoin kline types
_TF_MAP = {
    "1m":  "1min",
    "3m":  "3min",
    "5m":  "5min",
    "15m": "15min",
    "30m": "30min",
    "1h":  "1hour",
    "2h":  "2hour",
    "4h":  "4hour",
    "6h":  "6hour",
    "8h":  "8hour",
    "12h": "12hour",
    "1d":  "1day",
    "1w":  "1week",
}

# --- State ------------------------------------------------------------------
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()
_last_error: str | None = None


# --- HTTP helpers -----------------------------------------------------------

def _pair_to_kucoin(pair: str) -> str:
    """BTC/USDT -> BTC-USDT"""
    return pair.replace("/", "-")


def _fetch_klines(pair: str, timeframe: str) -> pd.DataFrame | None:
    """Pull the most recent KLINE_COUNT candles from KuCoin. Returns a DataFrame
    indexed oldest -> newest with columns: open, close, high, low, volume."""
    global _last_error
    kline_type = _TF_MAP.get(timeframe, "15min")
    symbol = _pair_to_kucoin(pair)

    # KuCoin returns up to 1500 candles; endAt trims to "now"
    url = f"{KUCOIN_BASE}/api/v1/market/candles"
    params = {"symbol": symbol, "type": kline_type}

    for attempt in range(MAX_RETRIES):
        try:
            with httpx.Client(timeout=REQUEST_TIMEOUT) as client:
                r = client.get(url, params=params)
            if r.status_code == 429:
                time.sleep(min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.1)))
                continue
            r.raise_for_status()
            payload = r.json()
        except Exception as e:  # noqa: BLE001
            _last_error = f"{type(e).__name__}: {e}"
            if attempt + 1 < MAX_RETRIES:
                time.sleep(BACKOFF_BASE * (2 ** attempt))
                continue
            return None

        if payload.get("code") != "200000" or not payload.get("data"):
            _last_error = f"kucoin response: {payload.get('code')} {payload.get('msg')}"
            return None

        # KuCoin rows: [time, open, close, high, low, volume, turnover]
        rows = payload["data"]
        if not rows:
            return None
        df = pd.DataFrame(rows, columns=["time", "open", "close", "high", "low", "volume", "turnover"])
        for c in ("open", "close", "high", "low", "volume"):
            df[c] = pd.to_numeric(df[c], errors="coerce")
        df["time"] = pd.to_numeric(df["time"], errors="coerce")
        # KuCoin returns newest-first; sort ascending
        df = df.sort_values("time").reset_index(drop=True)
        # Keep the last KLINE_COUNT
        if len(df) > KLINE_COUNT:
            df = df.tail(KLINE_COUNT).reset_index(drop=True)
        return df

    return None


# --- Indicator math ---------------------------------------------------------

def _compute_indicators(df: pd.DataFrame) -> dict[str, Any]:
    """Given an OHLCV dataframe, compute the same shape that tv_fetcher used to
    return so the rest of the code (opportunity_scanner, signal endpoint)
    doesn't need to change."""
    close = df["close"].to_numpy(dtype=float)
    high = df["high"].to_numpy(dtype=float)
    low = df["low"].to_numpy(dtype=float)
    volume = df["volume"].to_numpy(dtype=float)

    def _last(a: np.ndarray) -> float | None:
        if a is None or len(a) == 0:
            return None
        v = a[-1]
        if v is None or (isinstance(v, float) and (np.isnan(v) or np.isinf(v))):
            return None
        return float(v)

    rsi14 = _rsi(close, period=14)
    macd_line, macd_signal, macd_hist = _macd(close, 12, 26, 9)
    bb_upper, bb_middle, bb_lower = _bbands(close, 20, 2.0)
    ema20 = _ema(close, 20)
    sma50 = _sma(close, 50)
    adx14 = _adx(high, low, close, 14)
    atr14 = _atr(high, low, close, 14)

    # Lightweight summary like TradingView's "Recommendation"
    last_close = _last(close)
    last_ema = _last(ema20)
    last_sma = _last(sma50)
    last_rsi = _last(rsi14)
    last_macd_hist = _last(macd_hist)

    buy, sell, neutral = 0, 0, 0
    # trend signals
    if last_ema is not None and last_close is not None:
        (buy if last_close > last_ema else sell) and None  # no-op to appease linters
        if last_close > last_ema:
            buy += 1
        else:
            sell += 1
    if last_sma is not None and last_close is not None:
        if last_close > last_sma:
            buy += 1
        else:
            sell += 1
    # momentum
    if last_rsi is not None:
        if last_rsi < 30:
            buy += 1
        elif last_rsi > 70:
            sell += 1
        else:
            neutral += 1
    if last_macd_hist is not None:
        if last_macd_hist > 0:
            buy += 1
        elif last_macd_hist < 0:
            sell += 1
        else:
            neutral += 1

    recs = {(2, 0): "STRONG_BUY", (3, 0): "STRONG_BUY", (4, 0): "STRONG_BUY",
            (1, 0): "BUY"}
    if buy >= sell + 2:
        recommendation = "STRONG_BUY"
    elif buy > sell:
        recommendation = "BUY"
    elif sell >= buy + 2:
        recommendation = "STRONG_SELL"
    elif sell > buy:
        recommendation = "SELL"
    else:
        recommendation = "NEUTRAL"

    return {
        # shape mirrors the old tv_fetcher payload so callers don't change
        "summary": {
            "recommendation": recommendation,
            "buy": buy,
            "sell": sell,
            "neutral": neutral,
        },
        "oscillators": {
            "RECOMMENDATION": recommendation,
        },
        "moving_averages": {
            "RECOMMENDATION": "BUY" if (last_close or 0) > (last_ema or last_close or 0) else "SELL",
        },
        "indicators": {
            "RSI": last_rsi,
            "MACD.macd": _last(macd_line),
            "MACD.signal": _last(macd_signal),
            "BB.upper": _last(bb_upper),
            "BB.lower": _last(bb_lower),
            "EMA20": last_ema,
            "SMA50": last_sma,
            "ADX": _last(adx14),
            "ATR": _last(atr14),
            "volume": _last(volume),
            "close": last_close,
        },
        "source": "kucoin_klines",
    }


# --- Public API -------------------------------------------------------------

def _cache_put(key: tuple[str, str], data: dict[str, Any]) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), data)


def _cache_get(key: tuple[str, str], allow_stale: bool = False) -> tuple[dict[str, Any] | None, bool]:
    with _cache_lock:
        entry = _cache.get(key)
    if not entry:
        return None, False
    ts, data = entry
    age = time.time() - ts
    if age <= CACHE_TTL_SECONDS:
        return data, False
    if allow_stale and age <= STALE_GRACE_SECONDS:
        return data, True
    return None, False


def fetch(pair: str, timeframe: str = "15m") -> dict[str, Any] | None:
    """Return the indicator payload for (pair, timeframe). Serves from cache
    when fresh; on error returns stale cache with {"stale": True}."""
    key = (pair, timeframe)
    cached, stale = _cache_get(key)
    if cached and not stale:
        return cached

    df = _fetch_klines(pair, timeframe)
    if df is None or len(df) < 30:
        stale_data, _ = _cache_get(key, allow_stale=True)
        if stale_data:
            return {**stale_data, "stale": True, "reason": "kline_fetch_failed"}
        return None

    try:
        data = _compute_indicators(df)
    except Exception as e:  # noqa: BLE001
        global _last_error
        _last_error = f"indicator_compute: {type(e).__name__}: {e}"
        stale_data, _ = _cache_get(key, allow_stale=True)
        if stale_data:
            return {**stale_data, "stale": True, "reason": "compute_failed"}
        return None

    _cache_put(key, data)
    return data


def fetch_many(pairs: list[str], timeframe: str = "15m",
               max_workers: int = 6, per_request_pause: float = 0.0) -> dict[str, dict[str, Any] | None]:
    """Parallel fetch. KuCoin tolerates ~6 concurrent requests comfortably."""
    results: dict[str, dict[str, Any] | None] = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {}
        for i, p in enumerate(pairs):
            if per_request_pause and i > 0:
                time.sleep(per_request_pause)
            futures[pool.submit(fetch, p, timeframe)] = p
        for fut in as_completed(futures):
            pair = futures[fut]
            try:
                results[pair] = fut.result()
            except Exception as e:  # noqa: BLE001
                results[pair] = None
                global _last_error
                _last_error = f"worker_exception {pair}: {e}"
    return results


def status() -> dict[str, Any]:
    now = time.time()
    with _cache_lock:
        entries = list(_cache.values())
    fresh = sum(1 for ts, _ in entries if now - ts <= CACHE_TTL_SECONDS)
    stale = sum(1 for ts, _ in entries if CACHE_TTL_SECONDS < now - ts <= STALE_GRACE_SECONDS)
    return {
        "source": "kucoin_klines",
        "cache_entries": len(entries),
        "fresh_entries": fresh,
        "stale_entries": stale,
        "last_error": _last_error,
        # cooldown is a no-op for KuCoin (no IP ban issue) but kept for UI compat
        "cooldown_remaining_s": 0.0,
    }


def clear_cache() -> int:
    with _cache_lock:
        n = len(_cache)
        _cache.clear()
    return n
