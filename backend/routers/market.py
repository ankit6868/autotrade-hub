import httpx
from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import get_db, Config
from backend.utils.encryption import decrypt, DecryptError
from backend.utils.clerk_auth import get_user_id
from backend.services.kucoin_client import KuCoinClient
from backend.services import kucoin_indicators

router = APIRouter(prefix="/api/market", tags=["market"])


def _get_kucoin_client(db: Session, user_id: str) -> KuCoinClient:
    config = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not config or not config.kucoin_key_enc:
        raise ValueError("KuCoin not configured")
    try:
        return KuCoinClient(
            api_key=decrypt(config.kucoin_key_enc, user_id),
            api_secret=decrypt(config.kucoin_secret_enc, user_id),
            passphrase=decrypt(config.kucoin_passphrase_enc, user_id),
        )
    except DecryptError:
        raise ValueError(
            "Your API credentials could not be decrypted. "
            "Please go to Setup and re-enter your KuCoin API keys."
        )


@router.get("/pairs")
async def get_pairs(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Return all tradeable KuCoin USDT pairs using the public API (no credentials needed)."""
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get("https://api.kucoin.com/api/v2/symbols")
            data = resp.json()
        if str(data.get("code")) == "200000":
            usdt_pairs = sorted([
                f"{s['baseCurrency']}/{s['quoteCurrency']}"
                for s in data.get("data", [])
                if s.get("quoteCurrency") == "USDT" and s.get("enableTrading")
            ])
            return {"pairs": usdt_pairs}
    except Exception:
        pass

    # Fallback: use authenticated KuCoin client if public API fails
    try:
        kucoin = _get_kucoin_client(db, user_id)
        symbols = await kucoin.get_symbols()
        usdt_pairs = [s for s in symbols if s.endswith("/USDT")]
        return {"pairs": sorted(usdt_pairs)}
    except ValueError as e:
        return {"pairs": [], "error": str(e)}


@router.get("/price/{pair:path}")
async def get_price(
    pair: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        client = _get_kucoin_client(db, user_id)
        ticker = await client.get_ticker(pair)
        return {
            "pair": pair,
            "price": float(ticker.get("price", 0)),
            "bestBid": float(ticker.get("bestBid", 0)),
            "bestAsk": float(ticker.get("bestAsk", 0)),
        }
    except Exception as e:
        return {"pair": pair, "error": str(e)}


@router.get("/candles/{pair:path}")
async def get_candles(
    pair: str,
    kline_type: str = "15min",
    start: int = None,
    end: int = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    try:
        client = _get_kucoin_client(db, user_id)
        candles = await client.get_candles(pair, kline_type, start, end)
        return {"pair": pair, "candles": candles}
    except Exception as e:
        return {"pair": pair, "candles": [], "error": str(e)}


@router.get("/signals/{pair:path}")
async def get_pair_signals(pair: str, interval: str = "15m"):
    """KuCoin klines + local TA. No external TA service."""
    payload = kucoin_indicators.fetch(pair, interval)
    if not payload:
        return {"symbol": pair, "interval": interval, "error": "no_data"}
    return {
        "symbol": pair,
        "interval": interval,
        "summary": payload.get("summary"),
        "indicators": payload.get("indicators"),
        "oscillators": payload.get("oscillators"),
        "moving_averages": payload.get("moving_averages"),
        "source": payload.get("source", "kucoin_klines"),
    }
