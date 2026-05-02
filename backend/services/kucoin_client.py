import time
import hmac
import base64
import hashlib
import httpx
from typing import Any

KUCOIN_BASE = "https://api.kucoin.com"


class KuCoinClient:
    def __init__(self, api_key: str, api_secret: str, passphrase: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase

    def _sign(self, timestamp: str, method: str, endpoint: str, body: str = "") -> dict:
        str_to_sign = f"{timestamp}{method}{endpoint}{body}"
        signature = base64.b64encode(
            hmac.new(
                self.api_secret.encode(), str_to_sign.encode(), hashlib.sha256
            ).digest()
        ).decode()
        passphrase_sign = base64.b64encode(
            hmac.new(
                self.api_secret.encode(), self.passphrase.encode(), hashlib.sha256
            ).digest()
        ).decode()
        return {
            "KC-API-KEY": self.api_key,
            "KC-API-SIGN": signature,
            "KC-API-TIMESTAMP": timestamp,
            "KC-API-PASSPHRASE": passphrase_sign,
            "KC-API-KEY-VERSION": "2",
            "Content-Type": "application/json",
        }

    async def _request(self, method: str, endpoint: str, params: dict | None = None) -> Any:
        timestamp = str(int(time.time() * 1000))
        url = f"{KUCOIN_BASE}{endpoint}"
        headers = self._sign(timestamp, method.upper(), endpoint)
        async with httpx.AsyncClient() as client:
            if method.upper() == "GET":
                resp = await client.get(url, headers=headers, params=params, timeout=15)
            else:
                resp = await client.post(url, headers=headers, json=params, timeout=15)
            resp.raise_for_status()
            return resp.json()

    async def get_accounts(self) -> dict:
        return await self._request("GET", "/api/v1/accounts")

    async def get_balances(self) -> list:
        data = await self.get_accounts()
        return [
            {"currency": a["currency"], "balance": float(a["balance"]), "available": float(a["available"])}
            for a in data.get("data", [])
            if float(a["balance"]) > 0
        ]

    async def get_symbols(self) -> list[str]:
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"{KUCOIN_BASE}/api/v2/symbols", timeout=15)
            resp.raise_for_status()
            data = resp.json()
        return [s["symbol"].replace("-", "/") for s in data.get("data", []) if s.get("enableTrading")]

    async def get_ticker(self, pair: str) -> dict:
        symbol = pair.replace("/", "-")
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{KUCOIN_BASE}/api/v1/market/orderbook/level1",
                params={"symbol": symbol},
                timeout=15,
            )
            resp.raise_for_status()
            return resp.json().get("data", {})

    async def get_candles(self, pair: str, kline_type: str = "15min", start: int | None = None, end: int | None = None) -> list:
        symbol = pair.replace("/", "-")
        params: dict = {"symbol": symbol, "type": kline_type}
        if start:
            params["startAt"] = start
        if end:
            params["endAt"] = end
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"{KUCOIN_BASE}/api/v1/market/candles",
                params=params,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json().get("data", [])
        # KuCoin returns [time, open, close, high, low, volume, turnover]
        return [
            {
                "timestamp": int(c[0]),
                "open": float(c[1]),
                "close": float(c[2]),
                "high": float(c[3]),
                "low": float(c[4]),
                "volume": float(c[5]),
            }
            for c in data
        ]

    async def test_connection(self) -> dict:
        try:
            data = await self.get_accounts()
            if data.get("code") == "200000":
                balances = await self.get_balances()
                total_usdt = sum(b["balance"] for b in balances if b["currency"] == "USDT")
                return {"connected": True, "usdt_balance": total_usdt, "accounts": len(balances)}
            return {"connected": False, "error": data.get("msg", "Unknown error")}
        except Exception as e:
            return {"connected": False, "error": str(e)}
