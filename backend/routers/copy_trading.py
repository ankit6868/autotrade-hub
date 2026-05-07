"""
Copy Trading endpoints — master signal broadcasting + follower subscriptions.
"""
from __future__ import annotations

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from backend.models import get_db
from backend.models.trade import CopySignal, CopySubscription
from backend.models.strategy import Strategy
from backend.utils.clerk_auth import get_user_id
from backend.services.event_logger import log_event

router = APIRouter(prefix="/api/copy", tags=["copy_trading"])


# ── Master endpoints ──────────────────────────────────────────────────────────

@router.post("/become-master")
def become_master(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Enable copy trading on a strategy so followers can copy it."""
    strategy_id = req.get("strategy_id")
    if strategy_id:
        strat = db.execute(
            select(Strategy).where(Strategy.id == strategy_id, Strategy.user_id == user_id)
        ).scalar_one_or_none()
        if strat:
            strat.allow_copy_trading = True
            db.commit()
    log_event(db, user_id, "copy.become_master", request, payload={"strategy_id": strategy_id})
    return {"master": True, "user_id": user_id}


@router.get("/my-signals")
def my_signals(
    limit: int = 50,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get signals you've broadcast as a master."""
    signals = db.execute(
        select(CopySignal)
        .where(CopySignal.master_user_id == user_id)
        .order_by(desc(CopySignal.broadcasted_at))
        .limit(limit)
    ).scalars().all()
    return {
        "signals": [
            {
                "id":             s.id,
                "pair":           s.pair,
                "direction":      s.direction,
                "market_type":    s.market_type,
                "leverage":       s.leverage,
                "entry_price":    s.entry_price,
                "signal_type":    s.signal_type,
                "strategy_name":  s.strategy_name,
                "profit_pct":     s.profit_pct,
                "profit_abs":     s.profit_abs,
                "broadcasted_at": str(s.broadcasted_at),
                "closed_at":      str(s.closed_at) if s.closed_at else None,
            }
            for s in signals
        ]
    }


@router.get("/my-followers")
def my_followers(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get all users subscribed to your signals."""
    subs = db.execute(
        select(CopySubscription).where(CopySubscription.master_user_id == user_id)
    ).scalars().all()
    return {
        "followers": [
            {
                "follower_id":     s.follower_user_id,
                "is_active":       s.is_active,
                "copy_mode":       s.copy_mode,
                "total_copied":    s.total_copied,
                "total_profit":    s.total_profit,
                "created_at":      str(s.created_at),
            }
            for s in subs
        ]
    }


# ── Follower endpoints ────────────────────────────────────────────────────────

@router.post("/subscribe")
def subscribe(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Follow a master trader."""
    master_id    = req.get("master_user_id", "")
    copy_mode    = req.get("copy_mode", "paper")
    max_leverage = int(req.get("max_leverage", 10))
    stake_pct    = req.get("stake_override_pct")
    market_type  = req.get("copy_market_type", "spot")

    if not master_id:
        return {"error": "master_user_id required"}
    if master_id == user_id:
        return {"error": "Cannot follow yourself"}

    # Check if already subscribed
    existing = db.execute(
        select(CopySubscription).where(
            CopySubscription.follower_user_id == user_id,
            CopySubscription.master_user_id == master_id,
        )
    ).scalar_one_or_none()

    if existing:
        existing.is_active       = True
        existing.copy_mode       = copy_mode
        existing.max_leverage    = max_leverage
        existing.stake_override_pct = stake_pct
        existing.copy_market_type = market_type
        db.commit()
        return {"subscribed": True, "updated": True}

    sub = CopySubscription(
        follower_user_id   = user_id,
        master_user_id     = master_id,
        is_active          = True,
        copy_mode          = copy_mode,
        copy_market_type   = market_type,
        max_leverage       = max_leverage,
        stake_override_pct = stake_pct,
    )
    db.add(sub)
    db.commit()
    log_event(db, user_id, "copy.subscribe", request, payload={"master": master_id})
    return {"subscribed": True, "master_user_id": master_id}


@router.delete("/unsubscribe/{master_id}")
def unsubscribe(
    master_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    sub = db.execute(
        select(CopySubscription).where(
            CopySubscription.follower_user_id == user_id,
            CopySubscription.master_user_id   == master_id,
        )
    ).scalar_one_or_none()
    if sub:
        sub.is_active = False
        db.commit()
    return {"unsubscribed": True}


@router.get("/my-subscriptions")
def my_subscriptions(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    subs = db.execute(
        select(CopySubscription).where(
            CopySubscription.follower_user_id == user_id,
            CopySubscription.is_active == True,  # noqa: E712
        )
    ).scalars().all()
    return {
        "subscriptions": [
            {
                "id":              s.id,
                "master_user_id":  s.master_user_id,
                "copy_mode":       s.copy_mode,
                "copy_market_type": s.copy_market_type,
                "max_leverage":    s.max_leverage,
                "total_copied":    s.total_copied,
                "total_profit":    s.total_profit,
                "win_count":       s.win_count,
                "created_at":      str(s.created_at),
            }
            for s in subs
        ]
    }


@router.get("/feed")
def signal_feed(
    limit: int = 50,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Public signal feed — recent signals from masters you follow."""
    # Get masters you follow
    subs = db.execute(
        select(CopySubscription).where(
            CopySubscription.follower_user_id == user_id,
            CopySubscription.is_active == True,  # noqa: E712
        )
    ).scalars().all()
    master_ids = [s.master_user_id for s in subs]

    if not master_ids:
        return {"signals": []}

    signals = db.execute(
        select(CopySignal)
        .where(CopySignal.master_user_id.in_(master_ids))
        .order_by(desc(CopySignal.broadcasted_at))
        .limit(limit)
    ).scalars().all()

    return {
        "signals": [
            {
                "id":            s.id,
                "master":        s.master_user_id[:8] + "...",  # partial for privacy
                "pair":          s.pair,
                "direction":     s.direction,
                "market_type":   s.market_type,
                "leverage":      s.leverage,
                "entry_price":   s.entry_price,
                "signal_type":   s.signal_type,
                "strategy_name": s.strategy_name,
                "profit_pct":    s.profit_pct,
                "profit_abs":    s.profit_abs,
                "broadcasted_at": str(s.broadcasted_at),
            }
            for s in signals
        ]
    }
