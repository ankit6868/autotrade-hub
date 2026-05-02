"""
Thin wrapper around the TradeAudit table. Append-only — never updates.

Usage from a router:

    from backend.utils.audit import log_event
    log_event(db, user_id, "trade.start", request,
              mode="live", strategy_id=42, pair="BTC/USDT",
              payload={"wallet": 1000})
"""
from __future__ import annotations

from typing import Any

from fastapi import Request
from sqlalchemy.orm import Session

from backend.models.audit import TradeAudit


def log_event(
    db: Session,
    user_id: str,
    event: str,
    request: Request | None = None,
    *,
    mode: str | None = None,
    strategy_id: int | None = None,
    pair: str | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    db.add(TradeAudit(
        user_id=user_id,
        event=event,
        mode=mode,
        strategy_id=strategy_id,
        pair=pair,
        payload=payload,
        actor_ip=(request.client.host if request and request.client else None),
        user_agent=(request.headers.get("user-agent") if request else None),
    ))
    db.commit()
