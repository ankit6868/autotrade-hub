"""
Multi-Strategy endpoints — manage multiple simultaneous trading engines.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select

from backend.models import get_db
from backend.models.trade import StrategyInstance
from backend.utils.clerk_auth import get_user_id
from backend.services.multi_strategy import multi_strategy_manager
from backend.utils.audit import log_event

router = APIRouter(prefix="/api/strategies/instances", tags=["multi_strategy"])


@router.get("")
def list_instances(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    rows = db.execute(
        select(StrategyInstance).where(StrategyInstance.user_id == user_id)
    ).scalars().all()

    instances = []
    for r in rows:
        status = {}
        if r.is_running and r.engine_key:
            eng = multi_strategy_manager.get_engine(r.engine_key)
            if eng:
                status = eng.status()
        instances.append({
            "id":            r.id,
            "strategy_name": r.strategy_name,
            "strategy_id":   r.strategy_id,
            "market_type":   r.market_type,
            "mode":          r.mode,
            "pairs":         r.pairs,
            "leverage":      r.leverage,
            "timeframe":     r.timeframe,
            "stoploss":      r.stoploss,
            "wallet":        r.wallet,
            "risk_pct":      r.risk_pct,
            "is_running":    r.is_running,
            "engine_key":    r.engine_key,
            "total_trades":  r.total_trades,
            "total_pnl":     r.total_pnl,
            "created_at":    str(r.created_at),
            "live_status":   status,
        })
    return {"instances": instances}


@router.post("")
def create_instance(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Create and start a new strategy instance."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy
    from backend.utils.encryption import decrypt, DecryptError
    from backend.models.config import Config

    strategy_id   = req.get("strategy_id")
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    market_type   = req.get("market_type", "spot")
    mode          = req.get("mode", "paper")
    pairs         = req.get("pairs", "BTC/USDT")
    leverage      = int(req.get("leverage", 1))
    timeframe     = req.get("timeframe", "15m")
    stoploss      = float(req.get("stoploss", -0.03))
    takeprofit    = float(req.get("takeprofit", 0.015))
    wallet        = float(req.get("wallet", 1000.0))
    risk_pct      = float(req.get("risk_pct", 5.0))

    # Validate strategy exists
    if strategy_id:
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa: E712
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name = strat.name

    engine_key = f"{user_id}:{uuid.uuid4().hex[:8]}"

    row = StrategyInstance(
        user_id       = user_id,
        strategy_id   = strategy_id,
        strategy_name = strategy_name,
        market_type   = market_type,
        mode          = mode,
        pairs         = pairs if isinstance(pairs, str) else ",".join(pairs),
        leverage      = leverage,
        timeframe     = timeframe,
        stoploss      = stoploss,
        takeprofit    = takeprofit,
        wallet        = wallet,
        risk_pct      = risk_pct,
        is_running    = True,
        engine_key    = engine_key,
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    # Get credentials if needed
    kk = ks = kp = ""
    if mode == "live":
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            try:
                kk = decrypt(cfg.kucoin_key_enc or "", user_id)
                ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
                kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
            except DecryptError:
                return {"error": "Could not decrypt credentials. Re-enter in Setup."}

    result = multi_strategy_manager.start_instance(row, kk, ks, kp)
    log_event(db, user_id, "multi_strategy.create", request, payload={
        "strategy_name": strategy_name, "market_type": market_type, "mode": mode,
    })
    return {"instance_id": row.id, "engine_key": engine_key, "started": result}


@router.delete("/{instance_id}")
def delete_instance(
    instance_id: int,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    row = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == instance_id,
            StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not row:
        return {"error": "Instance not found"}

    if row.engine_key:
        multi_strategy_manager.stop_instance(row.engine_key)

    row.is_running = False
    db.commit()
    log_event(db, user_id, "multi_strategy.stop", request, payload={"instance_id": instance_id})
    return {"stopped": True, "instance_id": instance_id}


@router.get("/status")
def all_status(
    user_id: str = Depends(get_user_id),
):
    """Get live status of all running engines."""
    all_eng = multi_strategy_manager.all_status()
    user_eng = [e for e in all_eng if e.get("user_id") == user_id]
    return {"engines": user_eng}
