"""
KuCoin proxy helper with multi-proxy failover.

KuCoin geo-blocks US IPs. Railway (and most US PaaS providers) hand out US
egress IPs, so direct calls to `api.kucoin.com` / `api-futures.kucoin.com`
fail with HTTP 451 / "current area: US".

This module funnels every KuCoin REST call through a user-supplied proxy
when the env var `KUCOIN_HTTPS_PROXY` is set.

Single proxy:
    KUCOIN_HTTPS_PROXY=http://user:pass@host:port

Multiple proxies (comma OR newline separated):
    KUCOIN_HTTPS_PROXY=http://u:p@host1:port1,http://u:p@host2:port2,...

When the env var has multiple entries, the helpers try each in order on
every call. If a proxy returns 407 (Webshare rotated/expired credentials),
ProxyError (proxy down), or any 5xx/connection error, we move to the next.
The first one to succeed gets "promoted" to head-of-list and used for
subsequent calls until it also fails. This makes the app resilient to
Webshare's free-tier rotating individual proxy IPs without redeploying.

Two helpers:

* `urlopen(req, timeout=...)` — for the stdlib urllib calls in
                                 `native_trading_engine.py`. Tries each
                                 proxy until one succeeds or all 407.
* `httpx_client_kwargs()`     — drop-in kwargs for httpx clients. Returns
                                 the CURRENT best proxy. On 407 the caller
                                 should ideally re-fetch and retry, but
                                 since httpx-using paths are non-critical
                                 (Setup test, top-volume scanner), a
                                 single try is fine — user can retry.
"""
from __future__ import annotations

import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any

log = logging.getLogger(__name__)


# ── Proxy list management ──────────────────────────────────────────────

_lock = threading.Lock()
_cached_proxies: list[str] | None = None


def _parse_proxies() -> list[str]:
    """Parse KUCOIN_HTTPS_PROXY into a list. Supports comma / newline / whitespace
    as separators so users can paste the whole Webshare list at once."""
    raw = (
        os.getenv("KUCOIN_HTTPS_PROXY")
        or os.getenv("KUCOIN_PROXY_URL")
        or ""
    ).strip()
    if not raw:
        return []
    # Split on comma, newline, semicolon, or whitespace.
    parts = [p.strip() for p in re.split(r"[,\n;\s]+", raw) if p.strip()]
    # Filter out anything that doesn't look like a proxy URL.
    return [p for p in parts if "://" in p]


def _get_proxies() -> list[str]:
    """Return the cached proxy list, parsing env on first call.

    We cache so the list order (which is updated on success/failure) survives
    across calls within a process. Order matters: the head of the list is the
    proxy that worked most recently, so subsequent calls hit it first.
    """
    global _cached_proxies
    if _cached_proxies is None:
        _cached_proxies = _parse_proxies()
    return _cached_proxies


def kucoin_proxy_url() -> str | None:
    """Return the CURRENT best proxy URL, or None for direct connection."""
    lst = _get_proxies()
    return lst[0] if lst else None


def _promote(proxy: str) -> None:
    """Move `proxy` to the head of the list (it just succeeded)."""
    with _lock:
        if _cached_proxies and proxy in _cached_proxies and _cached_proxies[0] != proxy:
            _cached_proxies.remove(proxy)
            _cached_proxies.insert(0, proxy)


def _demote(proxy: str) -> None:
    """Move `proxy` to the tail (it failed — try others first next time)."""
    with _lock:
        if _cached_proxies and proxy in _cached_proxies and len(_cached_proxies) > 1:
            _cached_proxies.remove(proxy)
            _cached_proxies.append(proxy)
    # Drop the cached opener for this proxy — its TLS pool may be
    # stale (DNS failure / proxy down). Next call rebuilds fresh.
    with _opener_lock:
        _opener_cache.pop(proxy, None)


def _proxy_failed_terminally(exc: Exception) -> bool:
    """Return True if this exception means the proxy itself is bad, not the
    upstream — i.e. we should try the next proxy."""
    if isinstance(exc, urllib.error.HTTPError):
        # 407 Proxy Auth Required, 502/503/504 from the proxy itself
        return exc.code in (407, 502, 503, 504)
    if isinstance(exc, urllib.error.URLError):
        # DNS resolution failure, connection refused, TLS error — proxy down
        return True
    return False


# ── urllib (used by native_trading_engine + futures router) ─────────────


# Per-proxy opener cache — urllib doesn't pool HTTPS connections by
# default, so a fresh opener on every call means a new TCP + TLS
# handshake every call (~200-400ms each). Caching one opener per
# proxy URL lets HTTPSConnection reuse the underlying socket between
# calls to the same host (KuCoin), cutting subsequent-call latency
# significantly. Critical for the live manual-entry flow which makes
# 2-3 sequential KuCoin calls per click.
_opener_cache: dict[str, urllib.request.OpenerDirector] = {}
_opener_lock = threading.Lock()


def _build_opener(proxy: str | None) -> urllib.request.OpenerDirector:
    key = proxy or "__direct__"
    with _opener_lock:
        cached = _opener_cache.get(key)
        if cached is not None:
            return cached
        if proxy:
            handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
            opener = urllib.request.build_opener(handler)
        else:
            opener = urllib.request.build_opener()
        # urllib's HTTPSConnection keeps the underlying socket alive
        # between requests when keep-alive headers are present, which
        # is the default for HTTP/1.1.
        _opener_cache[key] = opener
        return opener


def reset_opener_cache() -> None:
    """Drop all cached openers — call when proxy list changes."""
    with _opener_lock:
        _opener_cache.clear()


def urlopen(req, *, timeout: float = 8):
    """Open `req` through the proxy list with automatic failover.

    Default timeout: 8 seconds (was 20). Order placement / close on
    live trading needs to fail fast — a 20s wait on a stuck proxy was
    the source of user-reported 6-7s close latency. With proxy
    failover + 8s per attempt, the worst case across N proxies is
    bounded at ~8N seconds, but the typical case (healthy proxy)
    completes in ~300-500ms because the cached HTTPS connection is
    reused (TLS handshake avoided after the first call).

    Iterates the current proxy list in order. The first proxy that returns
    a non-407 response wins and is promoted to the head. If every proxy
    fails with 407/proxy-down, raises the last error so the caller surfaces
    a real message instead of silently using no proxy.
    """
    proxies = list(_get_proxies())   # snapshot — _promote/_demote mutates the cache
    if not proxies:
        # No proxy configured — direct connection.
        return _build_opener(None).open(req, timeout=timeout)

    last_exc: Exception | None = None
    for proxy in proxies:
        opener = _build_opener(proxy)
        try:
            resp = opener.open(req, timeout=timeout)
            _promote(proxy)
            return resp
        except Exception as exc:    # noqa: BLE001
            last_exc = exc
            if _proxy_failed_terminally(exc):
                log.warning("Proxy %s failed (%s); trying next.",
                            _redact(proxy), type(exc).__name__)
                _demote(proxy)
                continue
            # Non-proxy error (e.g. KuCoin returned 4xx) — bubble up immediately;
            # rotating proxies won't help and would hide the real issue.
            raise

    # Every proxy failed terminally.
    if last_exc:
        raise last_exc
    raise RuntimeError("All configured proxies failed.")


def _redact(url: str) -> str:
    """Strip user:pass from a proxy URL for safe logging."""
    return re.sub(r"://[^@]+@", "://***@", url)


# ── httpx (used by kucoin_client / kucoin_futures_client) ───────────────


def httpx_client_kwargs() -> dict[str, Any]:
    """Return kwargs to splat into `httpx.AsyncClient(**kwargs)` / `httpx.Client(**kwargs)`.

    Returns the CURRENT best proxy. Failover happens at the urllib layer
    where most KuCoin calls live; httpx-using endpoints (Setup test,
    top-volume scanner) are low-stakes and benefit less from per-call
    rotation. If they hit a dead proxy, the user retries the click and
    the urllib path has by then demoted the bad proxy.
    """
    proxy = kucoin_proxy_url()
    if not proxy:
        return {}
    return {"proxy": proxy}


def proxy_status() -> dict:
    """Diagnostic helper — useful in a /api/health-style endpoint."""
    proxies = _get_proxies()
    return {
        "count": len(proxies),
        "active": _redact(proxies[0]) if proxies else None,
        "all":    [_redact(p) for p in proxies],
    }
