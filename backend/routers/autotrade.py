"""
/api/autotrade/* — manage the per-user auto-trade engine.

The engine periodically runs the opportunity scanner and auto-deploys the
best (pair, strategy) combo through Freqtrade. All settings live on the
user's Config row; this router is the control + observability surface.
Every endpoint is scoped to the authenticated user_id, so two concurrent
users each see and steer their own engine.
"""
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import get_db, Config
from backend.services.autotrade_engine import autotrade_engine
from backend.utils.clerk_auth import get_user_id

router = APIRouter(prefix="/api/autotrade", tags=["autotrade"])


class AutoTradeSettings(BaseModel):
    auto_trade_enabled: bool | None = None
    auto_trade_mode: str | None = None             # 'paper' | 'live'
    auto_trade_min_score: float | None = None       # 0..100
    auto_trade_timeframe: str | None = None         # '5m' | '15m' | '1h' | '4h'
    auto_trade_scan_interval_s: int | None = None
    trailing_stop_pct: float | None = None
    take_profit_pct: float | None = None
    position_adjustment: bool | None = None
    auto_trade_strategy_id: int | None = None       # null = auto-select
    auto_trade_pairs: str | None = None             # CSV "BTC/USDT,ETH/USDT" or null
    max_open_trades: int | None = None
    max_position_pct: float | None = None
    auto_sell_enabled: bool | None = None           # auto-sell on SELL/STRONG_SELL signals
    auto_sell_mode: str | None = None               # 'paper' | 'live'


@router.get("/status")
def status(user_id: str = Depends(get_user_id)):
    return autotrade_engine.for_user(user_id).status()


@router.post("/start")
def start(user_id: str = Depends(get_user_id)):
    eng = autotrade_engine.for_user(user_id)
    res = eng.start()
    return {**res, **eng.status()}


@router.post("/stop")
def stop(user_id: str = Depends(get_user_id)):
    eng = autotrade_engine.for_user(user_id)
    res = eng.stop()
    return {**res, **eng.status()}


@router.get("/settings")
def get_settings(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not cfg:
        return {"error": "Run the Setup Wizard first"}
    return {
        "auto_trade_enabled": bool(getattr(cfg, "auto_trade_enabled", False)),
        "auto_trade_mode": getattr(cfg, "auto_trade_mode", "paper"),
        "auto_trade_min_score": getattr(cfg, "auto_trade_min_score", 70.0),
        "auto_trade_timeframe": getattr(cfg, "auto_trade_timeframe", "15m"),
        "auto_trade_scan_interval_s": getattr(cfg, "auto_trade_scan_interval_s", 600),
        "trailing_stop_pct": getattr(cfg, "trailing_stop_pct", 0.0),
        "take_profit_pct": getattr(cfg, "take_profit_pct", 0.0),
        "position_adjustment": bool(getattr(cfg, "position_adjustment", False)),
        "max_position_pct": cfg.max_position_pct,
        "max_open_trades": cfg.max_open_trades,
        "auto_trade_strategy_id": getattr(cfg, "auto_trade_strategy_id", None),
        "auto_trade_pairs": getattr(cfg, "auto_trade_pairs", None),
        "auto_sell_enabled": bool(getattr(cfg, "auto_sell_enabled", False)),
        "auto_sell_mode": getattr(cfg, "auto_sell_mode", "paper"),
    }


@router.put("/settings")
def update_settings(
    req: AutoTradeSettings,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not cfg:
        return {"error": "Run the Setup Wizard first"}
    # Use exclude_unset so only fields the caller sent are written.
    # Crucially this lets auto_trade_strategy_id=null clear the pin, while
    # fields the caller omitted entirely are left unchanged.
    payload = req.model_dump(exclude_unset=True)
    for k, v in payload.items():
        setattr(cfg, k, v)
    db.commit()
    eng = autotrade_engine.for_user(user_id)
    # If the user just toggled auto-trade on, kick the engine immediately so
    # they don't have to call /start separately.
    if payload.get("auto_trade_enabled") and not eng.state.running:
        eng.start()
    elif payload.get("auto_trade_enabled") is False and eng.state.running:
        eng.stop()
    return {"updated": list(payload.keys()), "engine": eng.status()}
