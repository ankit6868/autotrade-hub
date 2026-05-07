"""
Futures trading endpoints — paper and live with leverage.
Completely isolated from spot trading (different market_type='futures').
"""
from __future__ import annotations

from datetime import datetime
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from backend.models import get_db
from backend.models.trade import Trade, StrategyInstance
from backend.models.config import Config
from backend.utils.clerk_auth import get_user_id
from backend.services.futures_engine import futures_engine_registry
from backend.utils.audit import log_event

router = APIRouter(prefix="/api/futures", tags=["futures"])


# ── Start / Stop ─────────────────────────────────────────────────────────────

@router.post("/start")
def start_futures(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Start a futures trading bot (paper or live)."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy

    strategy_id   = req.get("strategy_id")
    mode          = req.get("mode", "paper")
    pairs         = req.get("pairs", ["BTC/USDT"])
    leverage      = int(req.get("leverage", 10))
    timeframe     = req.get("timeframe", "15m")
    stoploss      = float(req.get("stoploss", -0.03))
    wallet        = float(req.get("wallet", 1000.0))
    take_profit   = float(req.get("take_profit_pct", 1.5))
    max_pos_pct   = float(req.get("max_position_pct", 5.0))

    # Resolve strategy name
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    if strategy_id:
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa: E712
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name = strat.name

    eng = futures_engine_registry.for_user(user_id)

    kk = ks = kp = ""
    if mode == "live":
        from backend.utils.encryption import decrypt, DecryptError
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if not cfg:
            return {"error": "No config found. Complete Setup first."}
        try:
            kk = decrypt(cfg.kucoin_key_enc or "", user_id)
            ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
            kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
        except Exception:
            return {"error": "Could not decrypt KuCoin credentials. Re-enter in Setup."}

    result = eng.start_futures(
        strategy_name    = strategy_name,
        pairs            = pairs,
        leverage         = leverage,
        mode             = mode,
        timeframe        = timeframe,
        stoploss         = stoploss,
        wallet           = wallet,
        max_position_pct = max_pos_pct,
        take_profit_pct  = take_profit,
        kucoin_key       = kk,
        kucoin_secret    = ks,
        kucoin_passphrase= kp,
        strategy_id      = strategy_id,
    )

    if result.get("started"):
        # Persist futures bot state for auto-resume
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            cfg.bot_running       = True
            cfg.bot_mode          = f"futures-{mode}"
            cfg.bot_strategy_name = strategy_name
            cfg.bot_pairs         = ",".join(pairs)
            cfg.bot_timeframe     = timeframe
            cfg.bot_stoploss      = stoploss
            cfg.bot_wallet        = wallet
            db.commit()

    log_event(db, user_id, "futures.start", request, payload={
        "mode": mode, "leverage": leverage, "pairs": pairs, "strategy": strategy_name
    })
    return result


@router.post("/stop")
def stop_futures(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    eng = futures_engine_registry.for_user(user_id)
    result = eng.stop()
    log_event(db, user_id, "futures.stop", request)
    return result


# ── Status ───────────────────────────────────────────────────────────────────

@router.get("/status")
def futures_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    eng = futures_engine_registry.for_user(user_id)
    return eng.status()


# ── Open Positions ────────────────────────────────────────────────────────────

@router.get("/open")
def futures_open_positions(
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import _kucoin_get

    eng = futures_engine_registry.for_user(user_id)
    native_positions = eng.get_open_positions()

    # Fetch live prices
    live_prices: dict[str, float] = {}
    for p in native_positions:
        try:
            sym  = p["pair"].replace("/", "-")
            data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
            if str(data.get("code")) == "200000":
                live_prices[p["pair"]] = float(data["data"]["price"])
        except Exception:
            pass

    native_trades = []
    for p in native_positions:
        entry     = p.get("entry", 0)
        stake     = p.get("stake", 0)
        leverage  = p.get("leverage", eng._leverage)
        cur       = live_prices.get(p["pair"], entry)
        direction = p.get("direction", "long")
        raw_pnl   = (cur - entry) / entry * stake if (entry and direction == "long") \
                    else (entry - cur) / entry * stake if entry else 0
        lev_pnl   = raw_pnl * leverage
        native_trades.append({
            "id":                f"futures-{p['pair']}",
            "pair":              p["pair"],
            "side":              direction,
            "entry_price":       entry,
            "current_price":     round(cur, 6),
            "amount":            stake,
            "leverage":          leverage,
            "liquidation_price": p.get("liquidation_price"),
            "stoploss_price":    p.get("sl"),
            "tp_price":          p.get("tp"),
            "entry_time":        p.get("opened_at"),
            "mode":              eng._mode,
            "market_type":       "futures",
            "unrealized_pnl":    round(lev_pnl, 4),
        })

    # DB open futures positions
    query = (
        select(Trade)
        .where(Trade.status == "open", Trade.user_id == user_id, Trade.market_type == "futures")
        .order_by(desc(Trade.entry_time))
    )
    if mode:
        query = query.where(Trade.mode == mode)

    db_trades = [
        {
            "id":                t.id,
            "pair":              t.pair,
            "side":              t.side,
            "entry_price":       t.entry_price,
            "amount":            t.amount,
            "leverage":          t.leverage,
            "liquidation_price": t.liquidation_price,
            "stoploss_price":    t.stoploss_price,
            "entry_time":        str(t.entry_time),
            "mode":              t.mode,
            "market_type":       "futures",
            "unrealized_pnl":    0,
        }
        for t in db.execute(query).scalars().all()
    ]

    pairs_in_native = {p["pair"] for p in native_positions}
    merged = native_trades + [t for t in db_trades if t["pair"] not in pairs_in_native]
    return {"trades": merged}


# ── Trade History ─────────────────────────────────────────────────────────────

@router.get("/history")
def futures_history(
    mode: str = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    query = (
        select(Trade)
        .where(Trade.status == "closed", Trade.user_id == user_id, Trade.market_type == "futures")
        .order_by(desc(Trade.exit_time))
    )
    if mode:
        query = query.where(Trade.mode == mode)
    query = query.limit(limit).offset(offset)

    trades = [
        {
            "id":           t.id,
            "pair":         t.pair,
            "side":         t.side,
            "entry_price":  t.entry_price,
            "exit_price":   t.exit_price,
            "amount":       t.amount,
            "leverage":     t.leverage,
            "profit_pct":   t.profit_pct,
            "profit_abs":   t.profit_abs,
            "entry_time":   str(t.entry_time),
            "exit_time":    str(t.exit_time),
            "exit_reason":  t.exit_reason,
            "mode":         t.mode,
            "market_type":  "futures",
        }
        for t in db.execute(query).scalars().all()
    ]
    return {"trades": trades}


# ── Force Close ───────────────────────────────────────────────────────────────

@router.post("/force-close/{pair:path}")
def futures_force_close(
    pair: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import _kucoin_get, _persist_closed_trade
    from datetime import timezone as _tz

    eng = futures_engine_registry.for_user(user_id)

    with eng._lock:
        trade_key = None
        pos = None
        for k, p in eng.positions.items():
            if p.pair == pair:
                trade_key = k
                pos = p
                break
        if pos is None:
            return {"error": f"No open futures position for {pair}"}

        try:
            sym  = pair.replace("/", "-")
            data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
            exit_price = float(data["data"]["price"]) if str(data.get("code")) == "200000" else pos.entry
        except Exception:
            exit_price = pos.entry

        pos.close(exit_price, "force_closed", datetime.now(_tz.utc))
        eng.balance += pos.pnl_abs
        eng.closed_trades.append(pos)
        del eng.positions[trade_key]

    _persist_closed_trade(user_id, pos, eng._mode, eng._strategy_id, pos.db_id)
    log_event(db, user_id, "futures.force_close", request,
              payload={"pair": pair, "exit_price": exit_price, "leverage": eng._leverage})
    return {"status": "closed", "pair": pair, "exit_price": exit_price,
            "pnl_abs": round(pos.pnl_abs, 4), "leverage": eng._leverage}
