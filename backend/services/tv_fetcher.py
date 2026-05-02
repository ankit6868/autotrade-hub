"""
Resilient TradingView Technical-Analysis fetcher.
=================================================
Wraps tradingview_ta with three defences against rate-limits / transient
failures so the Opportunity Scanner keeps returning data:

  1. **In-memory TTL cache** — same (pair, timeframe) within the TTL window
     returns the previously-fetched payload instead of hitting TradingView.
  2. **Retry with jittered exponential back-off** — on any exception
     (especially HTTP 429) we sleep 1s → 2s → 4s with a bit of jitter.
  3. **Exchange fallback** — if KuCoin's symbol map is unavailable, we retry
     against Binance, Bybit, OKX before giving up. TradingView exposes the
     same ticker under multiple exchanges, so this usually recovers.

The existing `signal_engine.get_signals()` remains untouched; the scanner
imports from *this* module instead.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any

from tradingview_ta import TA_Handler, Interval

# ---- Config -----------------------------------------------------------------

CACHE_TTL_SECONDS = 180            # fresh for 3 minutes
STALE_GRACE_SECONDS = 30 * 60      # still serve (flagged stale) for up to 30 min
MAX_RETRIES = 2                    # per exchange — fail fast, try next venue
BACKOFF_BASE = 0.4                 # seconds, doubled per attempt (0.4 → 0.8)
BACKOFF_CAP = 1.5                  # never sleep longer than this between retries
EXCHANGE_FALLBACKS = ["KUCOIN", "BINANCE", "BYBIT", "OKX", "MEXC", "GATEIO"]
# Once we know TradingView is globally rate-limiting us, quarantine further
# attempts for this many seconds so we don't hammer them.
GLOBAL_COOLDOWN_SECONDS = 90
# If we see this many 429s across any attempts, assume IP-level ban and arm
# the global cooldown right away so the rest of the batch fast-fails. This
# prevents a 20-pair scan from slogging through 20 × 6 exchanges × 2 retries.
RATE_LIMIT_TRIP_THRESHOLD = 3
# Hard per-pair deadline — once exceeded we give up and let stale cache handle it.
PER_PAIR_DEADLINE_SECONDS = 8.0

INTERVAL_MAP = {
    "1m":  Interval.INTERVAL_1_MINUTE,
    "5m":  Interval.INTERVAL_5_MINUTES,
    "15m": Interval.INTERVAL_15_MINUTES,
    "30m": Interval.INTERVAL_30_MINUTES,
    "1h":  Interval.INTERVAL_1_HOUR,
    "2h":  Interval.INTERVAL_2_HOURS,
    "4h":  Interval.INTERVAL_4_HOURS,
    "1d":  Interval.INTERVAL_1_DAY,
    "1w":  Interval.INTERVAL_1_WEEK,
    "1M":  Interval.INTERVAL_1_MONTH,
}

# ---- Cache (process-local, thread-safe) -------------------------------------

_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_cache_lock = threading.Lock()

# Per-symbol "last-good exchange" so repeat calls skip fallbacks once we know
# which venue has the ticker.
_exchange_hint: dict[str, str] = {}

# When set, we've seen repeated 429s and will skip new requests until this time.
_global_cooldown_until: float = 0.0

# Running count of 429s seen since the last successful fetch. When this crosses
# RATE_LIMIT_TRIP_THRESHOLD, we arm the global cooldown for the whole process.
_recent_429_count: int = 0
_recent_429_lock = threading.Lock()


def _note_429() -> None:
    """Record a 429. If the threshold is crossed, arm the global cooldown so
    the rest of the batch fails fast and serves stale cache."""
    global _global_cooldown_until, _recent_429_count
    with _recent_429_lock:
        _recent_429_count += 1
        if _recent_429_count >= RATE_LIMIT_TRIP_THRESHOLD and time.time() >= _global_cooldown_until:
            _global_cooldown_until = time.time() + GLOBAL_COOLDOWN_SECONDS


def _note_success() -> None:
    """Reset the 429 counter when a fetch succeeds."""
    global _recent_429_count
    with _recent_429_lock:
        _recent_429_count = 0


def _cache_get(key: tuple[str, str], allow_stale: bool = False) -> tuple[dict[str, Any], bool] | None:
    """Returns (data, is_stale) or None."""
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        ts, data = hit
        age = time.time() - ts
        if age <= CACHE_TTL_SECONDS:
            return data, False
        if allow_stale and age <= STALE_GRACE_SECONDS:
            return data, True
        if age > STALE_GRACE_SECONDS:
            del _cache[key]
        return None


def _cache_put(key: tuple[str, str], data: dict[str, Any]) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), data)


def clear_cache() -> int:
    """Drop everything from the cache. Returns the number of entries removed."""
    with _cache_lock:
        n = len(_cache)
        _cache.clear()
    return n


# ---- Core fetch -------------------------------------------------------------

def _call_tradingview(pair: str, exchange: str, timeframe: str) -> dict[str, Any] | None:
    """Single blocking call to TradingView — raises on error."""
    tv_symbol = pair.replace("/", "").replace("-", "").upper()
    tv_interval = INTERVAL_MAP.get(timeframe, Interval.INTERVAL_15_MINUTES)

    handler = TA_Handler(
        symbol=tv_symbol,
        screener="crypto",
        exchange=exchange,
        interval=tv_interval,
    )
    analysis = handler.get_analysis()
    if analysis is None:
        return None

    return {
        "pair": pair,
        "exchange": exchange,
        "timeframe": timeframe,
        "summary": analysis.summary,
        "oscillators": analysis.oscillators,
        "moving_averages": analysis.moving_averages,
        "indicators": analysis.indicators,
        "fetched_at": time.time(),
    }


def fetch(pair: str, timeframe: str = "15m") -> dict[str, Any] | None:
    """
    Fetch a TradingView TA snapshot for (pair, timeframe). Returns None if
    every attempt across every exchange failed AND we had no usable cache.
    If the network call fails but a stale cache entry (<30 min) exists, that
    entry is returned with `"stale": True` so the UI can flag it.
    """
    global _global_cooldown_until
    key = (pair, timeframe)
    fresh = _cache_get(key)
    if fresh is not None:
        data, _ = fresh
        return data

    # If we're globally rate-limited right now, skip the network entirely and
    # try to serve stale cache.
    if time.time() < _global_cooldown_until:
        stale = _cache_get(key, allow_stale=True)
        if stale is not None:
            data, _ = stale
            return {**data, "stale": True, "reason": "global_cooldown"}
        return None

    # Try the last-known-good exchange first (if any), then the rest.
    hint = _exchange_hint.get(pair)
    order = ([hint] if hint else []) + [e for e in EXCHANGE_FALLBACKS if e != hint]

    saw_429 = False
    deadline = time.time() + PER_PAIR_DEADLINE_SECONDS
    for exchange in order:
        if time.time() >= deadline:
            break
        # Check global cooldown armed by another worker mid-batch — stop immediately.
        if time.time() < _global_cooldown_until:
            saw_429 = True
            break
        for attempt in range(MAX_RETRIES):
            if time.time() >= deadline:
                break
            try:
                data = _call_tradingview(pair, exchange, timeframe)
                if data is None:
                    break
                _cache_put(key, data)
                _exchange_hint[pair] = exchange
                _note_success()
                return data
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "429" in msg or "rate" in msg.lower():
                    saw_429 = True
                    _note_429()
                    # If the threshold just tripped, bail out of the entire attempt.
                    if time.time() < _global_cooldown_until:
                        break
                    sleep_s = min(BACKOFF_CAP, BACKOFF_BASE * (2 ** attempt) + random.uniform(0, 0.2))
                    time.sleep(sleep_s)
                    continue
                if "invalid symbol" in msg.lower() or "exchange" in msg.lower():
                    break
                time.sleep(0.2 + random.uniform(0, 0.15))
        # If the threshold tripped during this exchange's retries, stop trying
        # further exchanges for this pair.
        if time.time() < _global_cooldown_until:
            break

    # Complete failure. If we saw 429s from multiple exchanges this is an
    # IP-level ban — arm the global cooldown so the next call fails fast.
    if saw_429 and time.time() >= _global_cooldown_until:
        _global_cooldown_until = time.time() + GLOBAL_COOLDOWN_SECONDS

    # Try stale cache as a graceful fallback.
    stale = _cache_get(key, allow_stale=True)
    if stale is not None:
        data, _ = stale
        return {**data, "stale": True, "reason": "rate_limit" if saw_429 else "fetch_failed"}
    return None


def status() -> dict[str, Any]:
    """Lightweight introspection used by the UI to show scanner health."""
    now = time.time()
    with _cache_lock:
        entries = [(p, tf, now - ts) for (p, tf), (ts, _) in _cache.items()]
    fresh = sum(1 for _, _, age in entries if age <= CACHE_TTL_SECONDS)
    return {
        "cache_entries": len(entries),
        "fresh_entries": fresh,
        "stale_entries": len(entries) - fresh,
        "cooldown_remaining_s": max(0.0, round(_global_cooldown_until - now, 1)),
    }


def fetch_many(
    pairs: list[str],
    timeframe: str = "15m",
    max_workers: int = 2,
    per_request_pause: float = 0.15,
) -> dict[str, dict[str, Any] | None]:
    """
    Fetch many pairs with a small worker pool + a small pause between requests
    so we stay well under TradingView's rate limit.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    results: dict[str, dict[str, Any] | None] = {}

    def _work(p: str) -> tuple[str, dict[str, Any] | None]:
        # Stagger requests inside each worker
        time.sleep(per_request_pause * random.random())
        return p, fetch(p, timeframe)

    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = [ex.submit(_work, p) for p in pairs]
        for fut in as_completed(futs):
            pair, data = fut.result()
            results[pair] = data
    return results
