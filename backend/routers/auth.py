from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx

from backend.models import get_db, Config
from backend.utils.encryption import encrypt, decrypt, DecryptError
from backend.utils.clerk_auth import get_user_id, clerk_enabled
from backend.services.kucoin_client import KuCoinClient

router = APIRouter(prefix="/api/config", tags=["config"])


class SetupRequest(BaseModel):
    kucoin_key: str = ""
    kucoin_secret: str = ""
    kucoin_passphrase: str = ""
    openrouter_key: str = ""
    preferred_model: str = "nvidia/nemotron-3-super-120b-a12b:free"
    max_position_pct: float = 5.0
    max_open_trades: int = 3
    max_daily_drawdown_pct: float = 5.0
    default_stoploss_pct: float = 3.0
    telegram_token: str = ""
    telegram_chat_id: str = ""


class UpdateConfigRequest(BaseModel):
    preferred_model: str | None = None
    max_position_pct: float | None = None
    max_open_trades: int | None = None
    max_daily_drawdown_pct: float | None = None
    default_stoploss_pct: float | None = None
    telegram_token: str | None = None
    telegram_chat_id: str | None = None


def _config_for(db: Session, user_id: str) -> Config | None:
    return db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()


@router.get("/auth-status")
def auth_status(user_id: str = Depends(get_user_id)):
    return {
        "clerk_enabled": clerk_enabled(),
        "user_id": user_id,
    }


@router.post("/setup")
def setup(
    req: SetupRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)

    fields = dict(
        kucoin_key_enc=encrypt(req.kucoin_key, user_id),
        kucoin_secret_enc=encrypt(req.kucoin_secret, user_id),
        kucoin_passphrase_enc=encrypt(req.kucoin_passphrase, user_id),
        openrouter_key_enc=encrypt(req.openrouter_key, user_id),
        preferred_model=req.preferred_model,
        max_position_pct=req.max_position_pct,
        max_open_trades=req.max_open_trades,
        max_daily_drawdown_pct=req.max_daily_drawdown_pct,
        default_stoploss_pct=req.default_stoploss_pct,
        telegram_token=req.telegram_token,
        telegram_chat_id=req.telegram_chat_id,
    )
    if config:
        for k, v in fields.items():
            setattr(config, k, v)
    else:
        db.add(Config(user_id=user_id, **fields))

    db.commit()
    return {"status": "ok", "message": "Configuration saved"}


@router.get("/status")
def get_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)
    if not config:
        return {"configured": False}

    return {
        "configured": True,
        "user_id": user_id,
        "has_kucoin": bool(config.kucoin_key_enc),
        "has_openrouter": bool(config.openrouter_key_enc),
        "preferred_model": config.preferred_model,
        "max_position_pct": config.max_position_pct,
        "max_open_trades": config.max_open_trades,
        "max_daily_drawdown_pct": config.max_daily_drawdown_pct,
        "default_stoploss_pct": config.default_stoploss_pct,
        "has_telegram": bool(config.telegram_token),
    }


@router.put("/update")
def update_config(
    req: UpdateConfigRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)
    if not config:
        return {"error": "Not configured yet. Use /api/config/setup first."}

    for field, value in req.model_dump(exclude_none=True).items():
        setattr(config, field, value)

    db.commit()
    return {"status": "ok"}


@router.post("/test-kucoin")
async def test_kucoin(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)
    if not config or not config.kucoin_key_enc:
        return {"connected": False, "error": "KuCoin keys not configured"}

    try:
        client = KuCoinClient(
            api_key=decrypt(config.kucoin_key_enc, user_id),
            api_secret=decrypt(config.kucoin_secret_enc, user_id),
            passphrase=decrypt(config.kucoin_passphrase_enc, user_id),
        )
    except DecryptError as e:
        return {"connected": False, "error": str(e)}
    return await client.test_connection()


@router.post("/test-openrouter")
async def test_openrouter(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)
    if not config or not config.openrouter_key_enc:
        return {"connected": False, "error": "OpenRouter key not configured"}

    try:
        key = decrypt(config.openrouter_key_enc, user_id)
    except DecryptError as e:
        return {"connected": False, "error": str(e)}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            free_models = [m for m in data.get("data", []) if ":free" in m.get("id", "")]
            return {"connected": True, "free_models": len(free_models), "total_models": len(data.get("data", []))}
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.get("/models")
async def list_models(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    config = _config_for(db, user_id)
    if not config or not config.openrouter_key_enc:
        return {"models": [], "error": "OpenRouter key not configured"}

    try:
        key = decrypt(config.openrouter_key_enc, user_id)
    except DecryptError as e:
        return {"models": [], "error": str(e)}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()
            free_models = [
                {"id": m["id"], "name": m.get("name", m["id"]), "context_length": m.get("context_length")}
                for m in data.get("data", [])
                if ":free" in m.get("id", "")
            ]
            return {"models": free_models}
    except Exception as e:
        return {"models": [], "error": str(e)}
