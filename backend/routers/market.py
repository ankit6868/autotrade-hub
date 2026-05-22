"""
/api/market/* — public market-data endpoints (no KuCoin credentials needed).

Slimmed down after the spot purge — provides ONLY what the futures terminal
and futures backtest UI need:

    GET /api/market/pairs                — list of tradable USDT-margined pairs
    GET /api/market/price/{pair}         — current mark/last price
    GET /api/market/ohlcv/{pair}         — candlestick array for charts

The legacy spot endpoints (/api/market/candles, /api/market/signals) and
all opportunity-scanner / TradingView signal-monitor endpoints were
removed alongside the spot trading stack.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
import urllib.error
import logging
from datetime import datetime, timezone

from fastapi import APIRouter
from backend.services._kucoin_proxy import urlopen as _proxy_urlopen

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/market", tags=["market"])

KUCOIN_FUTURES_BASE = "https://api-futures.kucoin.com"
KUCOIN_SPOT_BASE    = "https://api.kucoin.com"   # used ONLY for the pair-list discovery endpoint

# KuCoin Futures kline granularity is in MINUTES (not seconds — fixed in commit f6003ff).
TF_MINUTES = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720,
    "1d": 1440, "1w": 10080,
}


def _kc_get(path: str, base: str = KUCOIN_FUTURES_BASE, timeout: int = 8) -> dict:
    """Authless GET wrapped through the rotating webshare proxy."""
    req = urllib.request.Request(f"{base}{path}", method="GET")
    with _proxy_urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


@router.get("/pairs")
async def list_pairs():
    """Return the list of USDT-margined perp contracts the user can trade.

    Display format is BTC/USDT (slash) — the UI converts to BTCUSDTM at
    order-placement time via normalize_futures_symbol().
    """
    try:
        data = _kc_get("/api/v1/contracts/active")
        if str(data.get("code")) != "200000":
            return {"pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "source": "fallback"}
        contracts = data.get("data") or []
        pairs: list[str] = []
        for c in contracts:
            symbol = c.get("symbol", "")
            # KuCoin Futures uses {BASE}USDTM, except for BTC which is XBTUSDTM.
            if symbol.endswith("USDTM"):
                base = symbol[:-5].replace("XBT", "BTC")
                pairs.append(f"{base}/USDT")
        # Stable ordering — BTC/ETH first, alphabetical after.
        priority = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT"]
        out = [p for p in priority if p in pairs]
        out += sorted(p for p in pairs if p not in priority)
        return {"pairs": out, "count": len(out), "source": "kucoin_futures"}
    except Exception as e:
        log.warning("market.pairs fallback: %s", e)
        return {"pairs": ["BTC/USDT", "ETH/USDT", "SOL/USDT"], "source": "fallback", "error": str(e)}


@router.get("/price/{pair:path}")
async def get_price(pair: str):
    """Current mark/last price for `pair` (e.g. BTC/USDT)."""
    sym = pair.replace("/", "")
    if not sym.endswith("USDTM"):
        sym = sym.replace("USDT", "USDTM").replace("BTCUSDTM", "XBTUSDTM")
    try:
        data = _kc_get(f"/api/v1/ticker?symbol={sym}")
        if str(data.get("code")) != "200000":
            return {"pair": pair, "error": data.get("msg", "no_data")}
        d = data.get("data") or {}
        # KuCoin returns price as string; convert and round.
        return {
            "pair":      pair,
            "price":     str(float(d.get("price", 0) or 0)),
            "best_bid":  float(d.get("bestBidPrice", 0) or 0),
            "best_ask":  float(d.get("bestAskPrice", 0) or 0),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    except Exception as e:
        return {"pair": pair, "error": str(e)}


@router.get("/ohlcv/{pair:path}")
async def get_ohlcv(pair: str, timeframe: str = "15m", limit: int = 120):
    """Candlestick array suitable for lightweight-charts. Pulls from KuCoin
    Futures' /api/v1/kline/query endpoint. Returns [{time, open, high, low,
    close, volume}, ...] sorted oldest → newest."""
    sym = pair.replace("/", "")
    if not sym.endswith("USDTM"):
        sym = sym.replace("USDT", "USDTM").replace("BTCUSDTM", "XBTUSDTM")
    gran = TF_MINUTES.get(timeframe, 15)
    try:
        # Request approx `limit` bars worth of history.
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        from_ms = now_ms - (limit + 5) * gran * 60_000
        path = f"/api/v1/kline/query?symbol={sym}&granularity={gran}&from={from_ms}&to={now_ms}"
        data = _kc_get(path)
        if str(data.get("code")) != "200000":
            return {"pair": pair, "candles": [], "error": data.get("msg", "no_data")}
        rows = data.get("data") or []
        candles: list[dict] = []
        for r in rows[-limit:]:
            # KuCoin Futures kline shape: [timestamp_ms, open, high, low, close, volume]
            if len(r) < 6:
                continue
            candles.append({
                "time":   int(r[0]) // 1000,
                "open":   round(float(r[1]), 4),
                "high":   round(float(r[2]), 4),
                "low":    round(float(r[3]), 4),
                "close":  round(float(r[4]), 4),
                "volume": round(float(r[5]), 4),
            })
        candles.sort(key=lambda c: c["time"])
        return {"pair": pair, "timeframe": timeframe, "candles": candles, "count": len(candles)}
    except Exception as e:
        return {"pair": pair, "candles": [], "error": str(e)}
