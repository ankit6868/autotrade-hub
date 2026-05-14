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
        from backend.services._kucoin_proxy import httpx_client_kwargs as _kc_kwargs
        async with httpx.AsyncClient(**_kc_kwargs()) as client:
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
        from backend.services._kucoin_proxy import httpx_client_kwargs as _kc_kwargs
        async with httpx.AsyncClient(**_kc_kwargs()) as client:
            resp = await client.get(f"{KUCOIN_BASE}/api/v2/symbols", timeout=15)
            resp.raise_for_status()
            data = resp.json()
        return [s["symbol"].replace("-", "/") for s in data.get("data", []) if s.get("enableTrading")]

    async def get_ticker(self, pair: str) -> dict:
        symbol = pair.replace("/", "-")
        from backend.services._kucoin_proxy import httpx_client_kwargs as _kc_kwargs
        async with httpx.AsyncClient(**_kc_kwargs()) as client:
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
        from backend.services._kucoin_proxy import httpx_client_kwargs as _kc_kwargs
        async with httpx.AsyncClient(**_kc_kwargs()) as client:
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

    async def _get_futures_balance(self) -> dict | None:
        """Fetch the user's Lead Trading futures USDT balance.

        Hits `/api/v1/account-overview` on api-futures.kucoin.com — the same
        endpoint the Futures Terminal "Lead Trading Account" badge uses.
        Returns None on any failure so the caller can fall back gracefully.
        """
        from backend.services._kucoin_proxy import httpx_client_kwargs as _kc_kwargs
        FUTURES_BASE = "https://api-futures.kucoin.com"
        endpoint = "/api/v1/account-overview"
        # Re-sign for the futures host. Same HMAC scheme, just different base.
        timestamp = str(int(time.time() * 1000))
        # GET requests include the query string in the signature base.
        qs = "?currency=USDT"
        headers = self._sign(timestamp, "GET", f"{endpoint}{qs}")
        try:
            async with httpx.AsyncClient(**_kc_kwargs()) as client:
                resp = await client.get(
                    f"{FUTURES_BASE}{endpoint}",
                    params={"currency": "USDT"},
                    headers=headers, timeout=15,
                )
            payload = resp.json()
            if str(payload.get("code")) != "200000":
                return None
            data = payload.get("data") or {}
            return {
                "account_equity":    float(data.get("accountEquity", 0)),
                "available_balance": float(data.get("availableBalance", 0)),
                "margin_balance":    float(data.get("marginBalance", 0)),
                "unrealised_pnl":    float(data.get("unrealisedPNL", 0)),
                "position_margin":   float(data.get("positionMargin", 0)),
                "order_margin":      float(data.get("orderMargin", 0)),
            }
        except Exception:
            return None

    async def test_connection(self) -> dict:
        """Verify the key works and return a useful balance summary.

        Tries futures first (most users on this app are Lead Trading users
        whose money lives in futures, NOT spot — a $0 spot reading scares
        them into thinking the key is broken when it's actually fine).
        Falls back to spot if futures call doesn't auth.
        """
        # Try futures first — that's where the money is for Lead Trading keys.
        fut = await self._get_futures_balance()
        if fut is not None:
            return {
                "connected": True,
                "account_type": "futures",
                "usdt_balance":     round(fut["account_equity"], 4),
                "available_balance": round(fut["available_balance"], 4),
                "unrealised_pnl":   round(fut["unrealised_pnl"], 4),
                "position_margin":  round(fut["position_margin"], 4),
                "order_margin":     round(fut["order_margin"], 4),
            }

        # Fall back to spot — useful for users who only have spot keys.
        try:
            data = await self.get_accounts()
            if data.get("code") == "200000":
                balances = await self.get_balances()
                total_usdt = sum(b["balance"] for b in balances if b["currency"] == "USDT")
                return {
                    "connected": True,
                    "account_type": "spot",
                    "usdt_balance": total_usdt,
                    "accounts": len(balances),
                }
            return {"connected": False, "error": data.get("msg", "Unknown error")}
        except Exception as e:
            return {"connected": False, "error": str(e)}
