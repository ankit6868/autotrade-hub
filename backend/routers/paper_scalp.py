"""
API endpoints for the WebSocket-driven paper scalp engine.

Separate from the existing /api/futures/start /stop endpoints (which
drive the REST-polling engine and stay unchanged). Lets users run a
1m-scalp paper trade in parallel to their regular paper trade without
the two interfering.

Endpoints:
  POST   /api/paper-scalp/start    — start a new engine for the user
  POST   /api/paper-scalp/stop     — stop the engine matching pair+tf
  GET    /api/paper-scalp/status   — get status of all user's engines
  GET    /api/paper-scalp/status/{pair}/{timeframe}  — one engine's status
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.models.database import get_db
from backend.models.strategy import Strategy
from backend.routers.auth import get_user_id
from backend.services.paper_scalp_engine import (
    PaperScalpEngine, paper_scalp_registry,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/paper-scalp", tags=["paper-scalp"])


@router.post("/start")
async def start_paper_scalp(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Start a WebSocket-driven paper scalp engine.

    Request body (JSON):
      strategy_id (int, required): which strategy to run
      pair (str, default "BTC/USDT")
      timeframe (str, default "1m")
      starting_balance (float, default 1000)
      leverage (int, default 10)
      margin_pct (float, default 5.0)
      sltp_source ("strategy"|"slider", default "strategy")
      stoploss_pct (float, default 3.0)        # used when sltp_source=slider
      take_profit_pct (float, default 1.5)     # used when sltp_source=slider
      vip_tier (int 0..12, default 0)
      maker_only_entry (bool, default False)
      arm_enabled (bool, default False)
      arm_tp1_close_pct (float 1..99, default 50)
      arm_be_mode ("leverage"|"manual_pct"|"entry", default "leverage")
      arm_be_buffer_pct (float, default 1.0)
      arm_trail_to_tp1 (bool, default True)
    """
    strategy_id = req.get("strategy_id")
    if not strategy_id:
        raise HTTPException(400, "strategy_id is required")

    # Resolve strategy
    strat = db.execute(
        select(Strategy).where(
            Strategy.id == strategy_id,
        )
    ).scalar_one_or_none()
    if not strat:
        raise HTTPException(404, f"strategy {strategy_id} not found")
    # Check ownership / template access
    if strat.user_id != user_id and not getattr(strat, "is_template", False):
        raise HTTPException(403, "not your strategy")

    pair      = req.get("pair", "BTC/USDT")
    timeframe = req.get("timeframe", "1m")

    # Don't start a duplicate engine for the same user/pair/tf.
    existing = paper_scalp_registry.get(user_id, pair, timeframe)
    if existing and existing.state.status in ("connecting", "warming_up", "active"):
        return {
            "ok":      False,
            "error":   f"engine already running for {pair} {timeframe}",
            "status":  existing.status_snapshot(),
        }

    # Sanity-clamp args
    starting_balance  = max(10.0, float(req.get("starting_balance", 1000.0)))
    leverage          = max(1, min(125, int(req.get("leverage", 10))))
    margin_pct        = max(0.1, min(100.0, float(req.get("margin_pct", 5.0))))
    sltp_source       = req.get("sltp_source", "strategy")
    if sltp_source not in ("strategy", "slider"):
        sltp_source = "strategy"
    vip_tier          = max(0, min(12, int(req.get("vip_tier", 0))))
    maker_only_entry  = bool(req.get("maker_only_entry", False))

    engine = PaperScalpEngine(
        user_id           = user_id,
        strategy_name     = strat.name,
        generated_code    = strat.generated_code,
        pair              = pair,
        timeframe         = timeframe,
        starting_balance  = starting_balance,
        leverage          = leverage,
        margin_pct        = margin_pct,
        sltp_source       = sltp_source,
        stoploss_pct      = float(req.get("stoploss_pct", 3.0)),
        take_profit_pct   = float(req.get("take_profit_pct", 1.5)),
        vip_tier          = vip_tier,
        maker_only_entry  = maker_only_entry,
        arm_enabled       = bool(req.get("arm_enabled", False)),
        arm_tp1_close_pct = max(1.0, min(99.0, float(req.get("arm_tp1_close_pct", 50.0)))),
        arm_be_mode       = req.get("arm_be_mode", "leverage"),
        arm_be_buffer_pct = max(0.0, min(10.0, float(req.get("arm_be_buffer_pct", 1.0)))),
        arm_trail_to_tp1  = bool(req.get("arm_trail_to_tp1", True)),
    )
    paper_scalp_registry.add(engine)

    try:
        await engine.start()
    except Exception as e:
        paper_scalp_registry.remove(user_id, pair, timeframe)
        log.exception("paper-scalp start failed")
        raise HTTPException(500, f"start failed: {e}")

    return {
        "ok":     True,
        "status": engine.status_snapshot(),
    }


@router.post("/stop")
async def stop_paper_scalp(
    req: dict,
    user_id: str = Depends(get_user_id),
):
    """Stop a paper scalp engine for the given pair+timeframe.

    Request body:
      pair (str, default "BTC/USDT")
      timeframe (str, default "1m")
    """
    pair      = req.get("pair", "BTC/USDT")
    timeframe = req.get("timeframe", "1m")
    engine = paper_scalp_registry.get(user_id, pair, timeframe)
    if not engine:
        return {"ok": False, "error": f"no engine running for {pair} {timeframe}"}
    try:
        await engine.stop()
    except Exception:
        log.exception("paper-scalp stop failed")
    final = engine.status_snapshot()
    paper_scalp_registry.remove(user_id, pair, timeframe)
    return {"ok": True, "status": final}


@router.get("/status")
def status_all(user_id: str = Depends(get_user_id)):
    """List status of every paper scalp engine for the user."""
    engines = paper_scalp_registry.list_for_user(user_id)
    return {
        "engines": [e.status_snapshot() for e in engines],
        "count":   len(engines),
    }


@router.get("/status/{pair_base}/{pair_quote}/{timeframe}")
def status_one(
    pair_base: str,
    pair_quote: str,
    timeframe: str,
    user_id: str = Depends(get_user_id),
):
    """Status of one specific engine. Pair is split because '/' in URLs is
    awkward; client sends BTC/USDT as base=BTC&quote=USDT."""
    pair = f"{pair_base.upper()}/{pair_quote.upper()}"
    engine = paper_scalp_registry.get(user_id, pair, timeframe)
    if not engine:
        raise HTTPException(404, f"no engine running for {pair} {timeframe}")
    return engine.status_snapshot()
