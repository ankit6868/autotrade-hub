"""KuCoin public tickers endpoint - used to rank pairs by 24h volume.

We hit /api/v1/market/allTickers (public, no auth, no CORS) and keep a short
in-memory cache so the opportunity scanner and the UI can both ask for
'top N by volume' cheaply.
"""
from __future__ import annotations
import threading
import time
import httpx

KUCOIN_BASE = "https://api.kucoin.com"
_CACHE: dict = {"ts": 0.0, "data": []}
_LOCK = threading.Lock()
TTL_S = 120  # 2 min cache — volumes don't swing that fast


def _fetch_raw() -> list[dict]:
    with httpx.Client(timeout=10.0) as client:
        r = client.get(f"{KUCOIN_BASE}/api/v1/market/allTickers")
        r.raise_for_status()
        payload = r.json()
    return (payload.get("data") or {}).get("ticker", []) or []


def top_usdt_pairs(n: int = 50, min_volume_usd: float = 100_000.0) -> list[str]:
    """Return the top-N KuCoin USDT spot pairs by 24h quote volume.

    Pairs returned in 'BASE/USDT' form (slash). Cached for TTL_S seconds.
    """
    now = time.time()
    with _LOCK:
        if now - _CACHE["ts"] < TTL_S and _CACHE["data"]:
            rows = _CACHE["data"]
        else:
            try:
                rows = _fetch_raw()
                _CACHE["ts"] = now
                _CACHE["data"] = rows
            except Exception:
                rows = _CACHE["data"] or []

    usdt = []
    for t in rows:
        sym = t.get("symbol") or ""
        if not sym.endswith("-USDT"):
            continue
        try:
            quote_vol = float(t.get("volValue") or 0)
        except (TypeError, ValueError):
            quote_vol = 0
        if quote_vol < min_volume_usd:
            continue
        usdt.append((sym.replace("-", "/"), quote_vol))

    usdt.sort(key=lambda x: x[1], reverse=True)
    return [p for p, _ in usdt[:n]]


def top_usdt_pairs_with_volume(n: int = 50) -> list[dict]:
    """Same, but returns [{pair, volume_usd, price_pct_change}] for the UI."""
    now = time.time()
    with _LOCK:
        if now - _CACHE["ts"] < TTL_S and _CACHE["data"]:
            rows = _CACHE["data"]
        else:
            try:
                rows = _fetch_raw()
                _CACHE["ts"] = now
                _CACHE["data"] = rows
            except Exception:
                rows = _CACHE["data"] or []

    out = []
    for t in rows:
        sym = t.get("symbol") or ""
        if not sym.endswith("-USDT"):
            continue
        try:
            vol = float(t.get("volValue") or 0)
            chg = float(t.get("changeRate") or 0) * 100
            last = float(t.get("last") or 0)
        except (TypeError, ValueError):
            continue
        out.append({
            "pair": sym.replace("-", "/"),
            "volume_usd": vol,
            "change_pct": round(chg, 2),
            "price": last,
        })
    out.sort(key=lambda r: r["volume_usd"], reverse=True)
    return out[:n]
