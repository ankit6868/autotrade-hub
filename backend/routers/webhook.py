"""
/api/webhook/* — inbound webhook endpoints for external signal sources.

Supports TradingView alerts and generic signal webhooks. When a valid
signal arrives for a user, it is routed through the same trade pipeline
as a manual "start" — respecting all risk limits, safety gates and audit
logging.

Security model:
  Each user has a unique `webhook_secret` token (stored in Config).
  Every request must include ?token=<secret> or X-Webhook-Token header.
  Payloads are always validated before any trade action is taken.
"""
import hashlib
import hmac
import logging
import secrets
from datetime import datetime

from fastapi import APIRouter, Depends, Header, Query, Request, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models import get_db, Config
from backend.models.trade import Trade
from backend.services.freqtrade_manager import freqtrade_mgr
from backend.services.risk_manager import RiskManager
from backend.utils.audit import log_event
from backend.utils.clerk_auth import get_user_id

log = logging.getLogger("webhook")
router = APIRouter(prefix="/api/webhook", tags=["webhook"])

# ── Helpers ─────────────────────────────────────────────────────────────────

def _resolve_user_by_token(db: Session, token: str) -> Config | None:
    """Look up the Config row whose webhook_secret matches the supplied token.
    Timing-safe comparison to prevent oracle attacks."""
    rows = db.execute(select(Config).where(Config.webhook_secret.isnot(None))).scalars().all()
    for cfg in rows:
        if cfg.webhook_secret and hmac.compare_digest(cfg.webhook_secret, token):
            return cfg
    return None


# ── Pydantic models ──────────────────────────────────────────────────────────

class WebhookSignal(BaseModel):
    """
    TradingView alert JSON body.  All fields are optional so callers can
    send minimal payloads like {"action":"buy","pair":"BTC/USDT"}.
    """
    action: str                              # "buy" | "sell" | "close"
    pair: str = "BTC/USDT"
    strategy: str | None = None             # strategy name hint
    timeframe: str = "15m"
    price: float | None = None              # entry/exit price hint
    score: float | None = None              # signal quality 0-100
    comment: str | None = None             # free-text note
    # Allow TradingView native fields too
    ticker: str | None = None              # TV uses "ticker" not "pair"
    interval: str | None = None            # TV interval string


class WebhookSecretResponse(BaseModel):
    webhook_secret: str
    webhook_url: str


# ── Endpoints ────────────────────────────────────────────────────────────────

@router.post("/generate-secret")
def generate_secret(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Generate (or regenerate) this user's webhook secret token."""
    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not cfg:
        raise HTTPException(status_code=400, detail="Run Setup Wizard first")

    new_secret = secrets.token_urlsafe(32)
    cfg.webhook_secret = new_secret
    db.commit()
    return {
        "webhook_secret": new_secret,
        "webhook_url": f"/api/webhook/signal?token={new_secret}",
        "message": "Save this secret — it won't be shown again.",
    }


@router.get("/secret")
def get_secret_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Return whether a webhook secret is configured (not the secret itself)."""
    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    has_secret = bool(cfg and cfg.webhook_secret)
    return {
        "configured": has_secret,
        "webhook_url": "/api/webhook/signal?token=<your-secret>" if not has_secret else None,
    }


@router.post("/signal")
async def receive_signal(
    payload: WebhookSignal,
    request: Request,
    token: str = Query(..., description="User webhook secret"),
    db: Session = Depends(get_db),
):
    """
    Receive an inbound trading signal from TradingView (or any system).

    TradingView Alert Message format:
    {
      "action": "{{strategy.order.action}}",
      "pair":   "{{ticker}}",
      "price":  {{close}},
      "timeframe": "{{interval}}"
    }

    On success returns the triggered action and trade details.
    """
    # Resolve user from token
    cfg = _resolve_user_by_token(db, token)
    if not cfg:
        log.warning("Webhook: invalid token from %s", request.client.host if request.client else "unknown")
        raise HTTPException(status_code=401, detail="Invalid webhook token")

    user_id = cfg.user_id

    # Normalise action
    action = (payload.action or "").lower().strip()
    if action not in ("buy", "sell", "close", "long", "short"):
        raise HTTPException(status_code=400, detail=f"Unknown action '{action}'. Use: buy/sell/close")

    # Normalise pair — TradingView often sends "BTCUSDT" or uses `ticker`
    pair = payload.ticker or payload.pair or "BTC/USDT"
    pair = pair.upper().replace("USDT", "/USDT").replace("//", "/").strip()
    if "/" not in pair:
        pair = f"{pair}/USDT"

    timeframe = payload.interval or payload.timeframe or "15m"

    log.info("Webhook signal user=%s action=%s pair=%s tf=%s", user_id, action, pair, timeframe)

    # Audit the inbound signal regardless of whether we act on it
    log_event(
        db, user_id, "webhook.signal",
        mode=cfg.auto_trade_mode or "paper",
        payload={
            "action": action, "pair": pair, "timeframe": timeframe,
            "price": payload.price, "score": payload.score, "comment": payload.comment,
            "source_ip": request.client.host if request.client else "unknown",
        },
    )

    # ── Buy / Long signal ─────────────────────────────────────────────────
    if action in ("buy", "long"):
        # Check risk gates
        rm = RiskManager(cfg)
        risk = rm.check_can_open_trade(db, cfg.auto_trade_mode or "paper", user_id)
        if not risk["allowed"]:
            return {"status": "blocked", "reason": risk["reason"], "action": action, "pair": pair}

        # Find the best strategy for this pair or use auto_trade_strategy_id
        strategy_id = cfg.auto_trade_strategy_id
        if not strategy_id:
            from backend.models.strategy import Strategy as StrategyModel
            first_strat = db.execute(
                select(StrategyModel).where(StrategyModel.user_id == user_id).limit(1)
            ).scalar_one_or_none()
            if not first_strat:
                return {"status": "blocked", "reason": "No strategy configured", "pair": pair}
            strategy_id = first_strat.id

        # Deploy via Freqtrade
        mode = cfg.auto_trade_mode or "paper"
        mgr = freqtrade_mgr.for_user(user_id)
        result = mgr.start(
            strategy_id=strategy_id,
            mode=mode,
            pairs=[pair],
            timeframe=timeframe,
            stoploss=-(cfg.default_stoploss_pct or 3) / 100,
            wallet=1000,
        )
        log_event(db, user_id, "webhook.trade_opened", mode=mode,
                  strategy_id=strategy_id, pair=pair,
                  payload={"action": action, "result": str(result)[:500]})
        return {"status": "triggered", "action": "buy", "pair": pair, "mode": mode, "result": result}

    # ── Sell / Close signal ───────────────────────────────────────────────
    if action in ("sell", "close", "short"):
        # Find open trade for this pair and force-close it
        open_trade = db.execute(
            select(Trade).where(
                Trade.user_id == user_id,
                Trade.pair == pair,
                Trade.status == "open",
            ).limit(1)
        ).scalar_one_or_none()

        if not open_trade:
            return {"status": "no_open_trade", "pair": pair, "message": f"No open {pair} position to close"}

        mgr = freqtrade_mgr.for_user(user_id)
        try:
            mgr.force_close_trade(open_trade.id)
            log_event(db, user_id, "webhook.trade_closed", mode=open_trade.mode,
                      pair=pair, payload={"action": action, "trade_id": open_trade.id})
            return {"status": "closed", "action": "sell", "pair": pair, "trade_id": open_trade.id}
        except Exception as e:
            return {"status": "error", "pair": pair, "error": str(e)}

    return {"status": "unknown", "action": action}


@router.get("/logs")
def webhook_logs(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
    limit: int = 50,
):
    """Return recent webhook events from the audit log for this user."""
    from backend.models.audit import TradeAudit
    rows = db.execute(
        select(TradeAudit)
        .where(
            TradeAudit.user_id == user_id,
            TradeAudit.event.in_(["webhook.signal", "webhook.trade_opened", "webhook.trade_closed"]),
        )
        .order_by(TradeAudit.created_at.desc())
        .limit(limit)
    ).scalars().all()
    return {
        "logs": [
            {
                "id": r.id,
                "event": r.event,
                "mode": r.mode,
                "pair": r.pair,
                "payload": r.payload,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ]
    }
