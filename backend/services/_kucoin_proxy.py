"""
KuCoin proxy helper.

KuCoin geo-blocks US IPs. Railway (and most US PaaS providers) hand out US
egress IPs, so direct calls to `api.kucoin.com` / `api-futures.kucoin.com`
fail with HTTP 451 / "current area: US".

This module funnels every KuCoin REST call through a user-supplied proxy
when the env var `KUCOIN_HTTPS_PROXY` is set. Format:

    KUCOIN_HTTPS_PROXY=http://user:pass@proxyhost:port
    KUCOIN_HTTPS_PROXY=socks5://user:pass@proxyhost:port   # needs httpx[socks]

Two helpers:

* `urllib_opener()`       — for the stdlib urllib calls in
                             `native_trading_engine.py`.
* `httpx_client_kwargs()` — drop-in kwargs for `httpx.AsyncClient(...)` in
                             `kucoin_client.py` / `kucoin_futures_client.py`.

If the env var is empty/unset, both helpers degrade to a plain direct
client — local dev keeps working without a proxy.
"""
from __future__ import annotations

import os
import urllib.request
from typing import Any


def kucoin_proxy_url() -> str | None:
    """Return the configured KuCoin proxy URL, or None for direct connection."""
    url = (
        os.getenv("KUCOIN_HTTPS_PROXY")
        or os.getenv("KUCOIN_PROXY_URL")
        or ""
    ).strip()
    return url or None


# ── urllib (used by native_trading_engine) ──────────────────────────────

_opener_cache: tuple[str | None, urllib.request.OpenerDirector] | None = None


def urllib_opener() -> urllib.request.OpenerDirector:
    """Return a urllib opener that honors KUCOIN_HTTPS_PROXY, cached per URL."""
    global _opener_cache
    proxy = kucoin_proxy_url()
    if _opener_cache and _opener_cache[0] == proxy:
        return _opener_cache[1]

    if proxy:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy})
        opener = urllib.request.build_opener(handler)
    else:
        # No proxy configured — direct connection.
        opener = urllib.request.build_opener()

    _opener_cache = (proxy, opener)
    return opener


def urlopen(req, *, timeout: float = 20):
    """Wrapper that uses the proxy-aware opener."""
    return urllib_opener().open(req, timeout=timeout)


# ── httpx (used by kucoin_client / kucoin_futures_client) ───────────────


def httpx_client_kwargs() -> dict[str, Any]:
    """Return kwargs to splat into `httpx.AsyncClient(**kwargs)` / `httpx.Client(**kwargs)`.

    httpx >=0.28 removed `proxies=` and uses `proxy=` (single URL) instead.
    We pin httpx==0.28.1 in requirements.txt so always emit the singular form.
    """
    proxy = kucoin_proxy_url()
    if not proxy:
        return {}
    return {"proxy": proxy}
