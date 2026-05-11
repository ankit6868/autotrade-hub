"""
KuCoin Futures API client — async, HMAC-signed.

Mirrors kucoin_client.py but targets https://api-futures.kucoin.com.
Provides: order book, recent trades, account overview, positions,
order placement/cancellation, leverage control, and contract metadata.
"""
from __future__ import annotations

import time
import hmac
import base64
import hashlib
import json
import httpx
from typing import Any, Optional

KUCOIN_FUTURES_BASE = "https://api-futures.kucoin.com"


def _sign_request(
    api_secret: str, passphrase: str, api_key: str,
    timestamp: str, method: str, endpoint: str, body: str = "",
) -> dict[str, str]:
    str_to_sign = f"{timestamp}{method}{endpoint}{body}"
    signature = base64.b64encode(
        hmac.new(api_secret.encode(), str_to_sign.encode(), hashlib.sha256).digest()
    ).decode()
    passphrase_sign = base64.b64encode(
        hmac.new(api_secret.encode(), passphrase.encode(), hashlib.sha256).digest()
    ).decode()
    return {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": signature,
        "KC-API-TIMESTAMP": timestamp,
        "KC-API-PASSPHRASE": passphrase_sign,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }


class KuCoinFuturesClient:
    def __init__(self, api_key: str = "", api_secret: str = "", passphrase: str = ""):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

    def _headers(self, method: str, endpoint: str, body: str = "") -> dict[str, str]:
        ts = str(int(time.time() * 1000))
        return _sign_request(
            self.api_secret, self.passphrase, self.api_key,
            ts, method, endpoint, body,
        )

    async def _request(
        self, method: str, endpoint: str,
        params: dict | None = None, body: dict | None = None,
    ) -> Any:
        url = f"{KUCOIN_FUTURES_BASE}{endpoint}"
        body_str = json.dumps(body) if body else ""
        headers = self._headers(method.upper(), endpoint, body_str)
        async with httpx.AsyncClient() as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params, timeout=15)
            elif method.upper() == "DELETE":
                resp = await client.delete(url, headers=headers, params=params, timeout=15)
            else:
                resp = await client.post(url, headers=headers, content=body_str, timeout=15)
            resp.raise_for_status()
            return resp.json()

    async def _public_get(self, endpoint: str, params: dict | None = None) -> Any:
        url = f"{KUCOIN_FUTURES_BASE}{endpoint}"
        async with httpx.AsyncClient() as client:
            resp = await client.get(url, params=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

    # ── Public endpoints (no auth) ──────────────────────────────────────

    async def get_order_book(self, symbol: str, depth: int = 20) -> dict:
        endpoint = "/api/v1/level2/depth20" if depth <= 20 else "/api/v1/level2/depth100"
        data = await self._public_get(endpoint, {"symbol": symbol})
        return data.get("data", {})

    async def get_recent_trades(self, symbol: str) -> list[dict]:
        data = await self._public_get("/api/v1/trade/history", {"symbol": symbol})
        return data.get("data", [])

    async def get_contracts(self) -> list[dict]:
        data = await self._public_get("/api/v1/contracts/active")
        return data.get("data", [])

    async def get_contract_detail(self, symbol: str) -> dict:
        data = await self._public_get(f"/api/v1/contracts/{symbol}")
        return data.get("data", {})

    async def get_ticker(self, symbol: str) -> dict:
        data = await self._public_get("/api/v1/ticker", {"symbol": symbol})
        return data.get("data", {})

    async def get_klines(self, symbol: str, granularity: int = 15, start: int | None = None, end: int | None = None) -> list:
        params: dict = {"symbol": symbol, "granularity": granularity}
        if start:
            params["from"] = start
        if end:
            params["to"] = end
        data = await self._public_get("/api/v1/kline/query", params)
        return data.get("data", [])

    # ── Authenticated endpoints ─────────────────────────────────────────

    async def get_account_overview(self, currency: str = "USDT") -> dict:
        data = await self._request("GET", "/api/v1/account-overview", {"currency": currency})
        return data.get("data", {})

    async def get_positions(self) -> list[dict]:
        data = await self._request("GET", "/api/v1/positions")
        return data.get("data", [])

    async def get_position(self, symbol: str) -> dict:
        data = await self._request("GET", "/api/v1/position", {"symbol": symbol})
        return data.get("data", {})

    async def place_order(
        self,
        symbol: str,
        side: str,
        size: int,
        order_type: str = "market",
        price: float | None = None,
        leverage: int | None = None,
        stop: float | None = None,
        stop_type: str | None = None,
        client_oid: str | None = None,
        close_order: bool = False,
        reduce_only: bool = False,
        post_only: bool = False,
        hidden: bool = False,
        time_in_force: str | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
    ) -> dict:
        body: dict[str, Any] = {
            "clientOid": client_oid or f"ath-{int(time.time() * 1000)}",
            "side": side,
            "symbol": symbol,
            "type": order_type,
            "size": size,
        }
        if leverage is not None:
            body["leverage"] = str(leverage)
        if price is not None and order_type == "limit":
            body["price"] = str(price)
        if stop is not None:
            body["stop"] = "up" if side == "buy" else "down"
            body["stopPrice"] = str(stop)
            body["stopPriceType"] = stop_type or "TP"
        if close_order:
            body["closeOrder"] = True
        if reduce_only:
            body["reduceOnly"] = True
        if post_only:
            body["postOnly"] = True
        if hidden:
            body["hidden"] = True
        if time_in_force:
            body["timeInForce"] = time_in_force
        data = await self._request("POST", "/api/v1/orders", body=body)
        return data.get("data", {})

    async def cancel_order(self, order_id: str) -> dict:
        data = await self._request("DELETE", f"/api/v1/orders/{order_id}")
        return data.get("data", {})

    async def cancel_all_orders(self, symbol: str | None = None) -> dict:
        params = {"symbol": symbol} if symbol else None
        data = await self._request("DELETE", "/api/v1/orders", params=params)
        return data.get("data", {})

    async def get_open_orders(self, symbol: str | None = None) -> list[dict]:
        params: dict = {"status": "active"}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/api/v1/orders", params)
        items = data.get("data", {})
        if isinstance(items, dict):
            return items.get("items", [])
        return items

    async def get_order_history(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        params: dict = {"status": "done", "pageSize": limit}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/api/v1/orders", params)
        items = data.get("data", {})
        if isinstance(items, dict):
            return items.get("items", [])
        return items

    async def get_fills(self, symbol: str | None = None, limit: int = 50) -> list[dict]:
        params: dict = {"pageSize": limit}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/api/v1/fills", params)
        items = data.get("data", {})
        if isinstance(items, dict):
            return items.get("items", [])
        return items

    async def adjust_risk_limit(self, symbol: str, level: int) -> dict:
        data = await self._request("POST", "/api/v1/position/risk-limit-level/change", body={
            "symbol": symbol, "level": level,
        })
        return data.get("data", {})

    async def set_auto_deposit_margin(self, symbol: str, status: bool) -> dict:
        data = await self._request("POST", "/api/v1/position/margin/auto-deposit-status", body={
            "symbol": symbol, "status": status,
        })
        return data.get("data", {})

    async def add_margin(self, symbol: str, margin: float, biz_no: str | None = None) -> dict:
        body: dict = {"symbol": symbol, "margin": margin}
        if biz_no:
            body["bizNo"] = biz_no
        data = await self._request("POST", "/api/v1/position/margin/deposit-margin", body=body)
        return data.get("data", {})

    async def test_connection(self) -> dict:
        try:
            account = await self.get_account_overview()
            return {
                "connected": True,
                "balance": float(account.get("availableBalance", 0)),
                "equity": float(account.get("accountEquity", 0)),
            }
        except Exception as e:
            return {"connected": False, "error": str(e)}


# ── Synthetic order book for paper mode ─────────────────────────────────

def generate_paper_orderbook(price: float, spread_pct: float = 0.02, levels: int = 20) -> dict:
    """Generate a realistic-looking order book around the current price for paper trading."""
    import random
    half_spread = price * spread_pct / 100
    asks = []
    bids = []
    for i in range(levels):
        ask_price = round(price + half_spread + (i * price * 0.0001), 2)
        bid_price = round(price - half_spread - (i * price * 0.0001), 2)
        ask_size = round(random.uniform(0.01, 5.0), 3)
        bid_size = round(random.uniform(0.01, 5.0), 3)
        asks.append([str(ask_price), str(ask_size)])
        bids.append([str(bid_price), str(bid_size)])
    return {
        "asks": asks,
        "bids": bids,
        "ts": int(time.time() * 1000),
    }
