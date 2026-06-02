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

import io
import logging
import os
import re
import threading
import urllib.error
import urllib.request
from typing import Any

import requests
import requests.adapters

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
    # Drop the cached Session for this proxy — its connection pool may be
    # stale (DNS failure / proxy down / rotated creds). Next call rebuilds
    # a fresh Session + pool.
    _drop_session(proxy)


# ── pooled requests.Session layer (used by native_trading_engine + ──────
# ──  futures router + futures_engine via urlopen) ───────────────────────
#
# WHY requests instead of urllib:
#   urllib.request opens a brand-new HTTPSConnection on EVERY urlopen()
#   call (AbstractHTTPHandler.do_open does `http_class(host, ...)` each
#   time), so every KuCoin call paid a full TCP + TLS handshake — through
#   a proxy that's ~150-300ms of pure round-trips before a single byte of
#   the actual request is sent. On the live trading path (buy/long,
#   sell/short, close, partial-close) that handshake dominated latency.
#
#   requests.Session (urllib3 underneath) keeps a keep-alive connection
#   pool keyed by (proxy, destination host). For HTTPS-through-an-HTTP
#   proxy it reuses the CONNECT-tunnelled, TLS-handshaked socket across
#   calls, so only the FIRST call to KuCoin pays the handshake; every
#   subsequent call reuses the warm socket (~typical 80-200ms total
#   instead of 250-500ms). This is the single biggest backend latency win
#   for live order execution.
#
# We keep the public `urlopen(req, timeout=...)` signature and a urllib-
# compatible response/error surface so none of the ~15 call sites change.

# Per-proxy Session cache. Sessions are thread-safe for our usage pattern
# (each call grabs the cached Session and issues an independent request;
# urllib3's connection pool is internally locked).
_session_cache: dict[str, requests.Session] = {}
_session_lock = threading.Lock()

# Tune-able pool sizes. The live path makes at most 2-3 concurrent KuCoin
# calls per user action; keep a small but reusable pool.
_POOL_CONNECTIONS = int(os.getenv("KUCOIN_POOL_CONNECTIONS", "4"))
_POOL_MAXSIZE     = int(os.getenv("KUCOIN_POOL_MAXSIZE", "10"))


def _get_session(proxy: str | None) -> requests.Session:
    key = proxy or "__direct__"
    with _session_lock:
        sess = _session_cache.get(key)
        if sess is not None:
            return sess
        sess = requests.Session()
        # Don't let OS / shell env proxies (HTTP_PROXY etc.) leak in — we
        # set the proxy explicitly per-Session. trust_env=False also skips
        # .netrc lookups, shaving a touch of per-request work.
        sess.trust_env = False
        if proxy:
            sess.proxies = {"http": proxy, "https": proxy}
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=_POOL_CONNECTIONS,
            pool_maxsize=_POOL_MAXSIZE,
            max_retries=0,          # we do our own proxy-level failover
        )
        sess.mount("https://", adapter)
        sess.mount("http://", adapter)
        _session_cache[key] = sess
        return sess


def _drop_session(proxy: str | None) -> None:
    key = proxy or "__direct__"
    with _session_lock:
        sess = _session_cache.pop(key, None)
    if sess is not None:
        try:
            sess.close()
        except Exception:
            pass


def reset_session_cache() -> None:
    """Drop all cached Sessions — call when the proxy list changes."""
    with _session_lock:
        sessions = list(_session_cache.values())
        _session_cache.clear()
    for sess in sessions:
        try:
            sess.close()
        except Exception:
            pass


# Backward-compat alias for the old urllib-era name (kept in case any
# external caller still imports it; internally unused now).
reset_opener_cache = reset_session_cache


class _Resp:
    """Minimal urllib-compatible wrapper over a requests.Response so existing
    call sites (`with urlopen(req) as resp: resp.read().decode()`) keep working.

    requests has already fully read the body into `r.content` by the time we
    wrap it, so `.read()` just hands back those bytes and the underlying
    socket has been returned to the keep-alive pool for reuse.
    """

    __slots__ = ("_r", "_content", "status", "code", "headers")

    def __init__(self, r: requests.Response):
        self._r = r
        self._content = r.content
        self.status = r.status_code
        self.code = r.status_code
        self.headers = r.headers

    def read(self, *_a, **_k) -> bytes:
        return self._content

    def getcode(self) -> int:
        return self._r.status_code

    def geturl(self) -> str:
        return self._r.url

    def info(self):
        return self._r.headers

    def close(self) -> None:
        try:
            self._r.close()
        except Exception:
            pass

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *_exc) -> bool:
        self.close()
        return False


# requests transport errors that mean "this proxy is bad, try the next one"
# (the urllib3 equivalents of urllib's URLError-for-proxy-down).
_PROXY_TRANSPORT_ERRORS = (
    requests.exceptions.ProxyError,
    requests.exceptions.SSLError,
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
)

# HTTP status codes that indicate the PROXY (not KuCoin) failed → try next.
_PROXY_FAIL_STATUS = (407, 502, 503, 504)


def _send(req, proxy: str | None, timeout: float) -> requests.Response:
    """Issue `req` (a urllib.request.Request) via the pooled Session for `proxy`."""
    sess = _get_session(proxy)
    method = req.get_method()
    url = req.full_url
    # urllib capitalises header keys (KC-API-KEY -> Kc-api-key); HTTP headers
    # are case-insensitive and KuCoin accepts them (this is exactly what the
    # urllib path already put on the wire in production).
    headers = dict(req.header_items())
    data = req.data
    # Short connect timeout for fast proxy failover; full read timeout as
    # requested by the caller. (connect, read)
    connect_to = min(float(timeout), 4.0)
    return sess.request(
        method, url,
        headers=headers,
        data=data,
        timeout=(connect_to, float(timeout)),
        allow_redirects=True,
    )


def _finalize(r: requests.Response):
    """Return a urllib-compatible _Resp for 2xx; raise HTTPError otherwise
    (matches urllib's urlopen, which raises HTTPError on any non-2xx)."""
    if 200 <= r.status_code < 300:
        return _Resp(r)
    raise urllib.error.HTTPError(
        r.url, r.status_code, r.reason or "", r.headers, io.BytesIO(r.content)
    )


def urlopen(req, *, timeout: float = 8):
    """Open `req` through the proxy list with automatic failover.

    Drop-in replacement for the old urllib-based helper, but now backed by a
    pooled requests.Session so HTTPS keep-alive connections are reused across
    calls (no per-call TLS handshake after the first). Returns a
    urllib-compatible response (supports `with ... as resp: resp.read()`).

    Behaviour preserved from the urllib version:
      * Iterates the current proxy list; first success is promoted to head.
      * 407/502/503/504 OR a transport error (proxy down / DNS / TLS / timeout)
        → demote that proxy and try the next one.
      * Any OTHER non-2xx (e.g. KuCoin 400 "balance insufficient") → raise
        urllib.error.HTTPError immediately so the caller's existing
        `except urllib.error.HTTPError` handlers and `.read()` on the error
        body keep working (rotating proxies wouldn't help and would hide the
        real error).
      * No proxy configured → direct connection.

    Worst case across N proxies is bounded by ~(connect+read) per attempt;
    the typical healthy case completes fast on a warm pooled socket.
    """
    proxies = list(_get_proxies())   # snapshot — _promote/_demote mutates the cache
    if not proxies:
        # No proxy configured — direct connection (local dev).
        try:
            r = _send(req, None, timeout)
        except _PROXY_TRANSPORT_ERRORS as exc:
            # Surface as urllib.error.URLError so callers expecting the urllib
            # exception type still match.
            raise urllib.error.URLError(exc) from exc
        return _finalize(r)

    last_exc: Exception | None = None
    for proxy in proxies:
        try:
            r = _send(req, proxy, timeout)
        except _PROXY_TRANSPORT_ERRORS as exc:
            last_exc = urllib.error.URLError(exc)
            log.warning("Proxy %s failed (%s); trying next.",
                        _redact(proxy), type(exc).__name__)
            _demote(proxy)
            continue

        if r.status_code in _PROXY_FAIL_STATUS:
            last_exc = urllib.error.HTTPError(
                r.url, r.status_code, r.reason or "", r.headers, io.BytesIO(r.content)
            )
            log.warning("Proxy %s returned HTTP %s; trying next.",
                        _redact(proxy), r.status_code)
            _demote(proxy)
            continue

        # Reached KuCoin (2xx or a real upstream 4xx/5xx). This proxy works.
        _promote(proxy)
        return _finalize(r)   # raises HTTPError for non-proxy non-2xx

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
