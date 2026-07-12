from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session
import httpx

from backend.models import get_db, Config
from backend.utils.encryption import encrypt, decrypt, DecryptError
from backend.utils.clerk_auth import get_user_id, clerk_enabled
# kucoin_client.py was deleted in the spot purge (NICE-9). The
# /test-kucoin endpoint now hits the futures API directly via
# native_trading_engine._kucoin_get_signed — see test_kucoin() below.

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


class RegularKeysRequest(BaseModel):
    """Normal (non-lead) KuCoin Futures API keys for the /regular-futures-trade terminal."""
    kucoin_reg_key: str = ""
    kucoin_reg_secret: str = ""
    kucoin_reg_passphrase: str = ""


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


@router.post("/setup-regular")
def setup_regular(
    req: RegularKeysRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Save the normal KuCoin Futures API keys (separate from the Lead keys).

    Only overwrites the three regular-key columns, so it never disturbs the
    Lead credentials or any other config. Requires the base config to exist
    (Lead setup runs first) — but creates a row if somehow missing.
    """
    fields = dict(
        kucoin_reg_key_enc=encrypt(req.kucoin_reg_key, user_id),
        kucoin_reg_secret_enc=encrypt(req.kucoin_reg_secret, user_id),
        kucoin_reg_passphrase_enc=encrypt(req.kucoin_reg_passphrase, user_id),
    )
    config = _config_for(db, user_id)
    if config:
        for k, v in fields.items():
            setattr(config, k, v)
    else:
        db.add(Config(user_id=user_id, **fields))
    db.commit()
    return {"status": "ok", "message": "Regular futures keys saved"}


@router.post("/test-kucoin-regular")
def test_kucoin_regular(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Test the normal KuCoin Futures keys. Reads /account-overview (identical
    endpoint for both modes) so a pass guarantees the regular terminal can read
    balance + place orders on the private account."""
    config = _config_for(db, user_id)
    if not config or not config.kucoin_reg_key_enc:
        return {"connected": False, "error": "Regular futures keys not configured"}
    try:
        from backend.services.futures_mode import load_kucoin_creds
        kk, ks, kp = load_kucoin_creds(config, user_id, "regular")
    except DecryptError as e:
        return {"connected": False, "error": str(e)}
    try:
        from backend.services.native_trading_engine import _kucoin_get_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"}, base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) == "200000":
            acct = data.get("data", {}) or {}
            return {
                "connected": True,
                "account_type": "futures",
                "currency": "USDT",
                "usdt_balance": float(acct.get("accountEquity", 0) or 0),
                "available_balance": float(acct.get("availableBalance", 0) or 0),
                "position_margin": float(acct.get("positionMargin", 0) or 0),
                "order_margin": float(acct.get("orderMargin", 0) or 0),
                "unrealised_pnl": float(acct.get("unrealisedPNL", 0) or 0),
                "frozen_funds": float(acct.get("frozenFunds", 0) or 0),
            }
        return {"connected": False, "error": data.get("msg") or "KuCoin rejected the keys"}
    except Exception as e:
        return {"connected": False, "error": str(e)}


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
        "has_kucoin_regular": bool(config.kucoin_reg_key_enc),
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
    """NICE-9: rewritten to use the FUTURES side directly.

    Previously hit /api/v1/accounts via the deleted spot KuCoinClient.
    Now hits /api/v1/account-overview on api-futures.kucoin.com, which is
    the same endpoint the futures engine uses for balance checks — so a
    successful "Test Connection" guarantees the futures bot can also
    read the balance + place orders."""
    config = _config_for(db, user_id)
    if not config or not config.kucoin_key_enc:
        return {"connected": False, "error": "KuCoin keys not configured"}

    try:
        kk = decrypt(config.kucoin_key_enc, user_id)
        ks = decrypt(config.kucoin_secret_enc, user_id)
        kp = decrypt(config.kucoin_passphrase_enc, user_id)
    except DecryptError as e:
        return {"connected": False, "error": str(e)}

    try:
        from backend.services.native_trading_engine import _kucoin_get_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"},
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) == "200000":
            acct = data.get("data", {}) or {}
            # Response shape MUST match what setup/page.tsx reads —
            # previous version returned `balance`/`equity`/`source`
            # but the UI reads `account_type`/`usdt_balance`/
            # `available_balance`/`position_margin`/`unrealised_pnl`.
            # Mismatch caused "Connected! Spot USDT Balance: NaN"
            # because Number(undefined).toFixed(2) === 'NaN' AND the
            # UI's account_type check fell to the spot-branch fallback.
            return {
                "connected":         True,
                "account_type":      "futures",
                "currency":          "USDT",
                "usdt_balance":      float(acct.get("accountEquity",   0) or 0),
                "available_balance": float(acct.get("availableBalance",0) or 0),
                "position_margin":   float(acct.get("positionMargin",  0) or 0),
                "order_margin":      float(acct.get("orderMargin",     0) or 0),
                "unrealised_pnl":    float(acct.get("unrealisedPNL",   0) or 0),
                "frozen_funds":      float(acct.get("frozenFunds",     0) or 0),
                # Kept for backwards-compat with any callers expecting
                # the old field names.
                "balance":           float(acct.get("availableBalance",0) or 0),
                "equity":            float(acct.get("accountEquity",   0) or 0),
                "source":            "futures",
            }
        return {"connected": False, "error": data.get("msg", "Unknown KuCoin error")}
    except Exception as e:
        return {"connected": False, "error": f"Could not reach KuCoin Futures: {e}"}


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
