import logging
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

log = logging.getLogger("trading")
from sqlalchemy import select, desc
from sqlalchemy.orm import Session

from backend.models import get_db, Config, Strategy
from backend.models.trade import Trade
from backend.utils.encryption import decrypt, DecryptError
from backend.utils.clerk_auth import get_user_id
from backend.utils.audit import log_event
from backend.utils.rate_limit import limiter, TRADE_LIMIT
from backend.services.freqtrade_manager import freqtrade_mgr
from backend.services.risk_manager import RiskManager
from backend.services.trade_sync import sync as sync_trades

router = APIRouter(prefix="/api/trade", tags=["trading"])


class StartRequest(BaseModel):
    strategy_id: int
    mode: str = "paper"  # "paper" or "live"
    pairs: list[str] = ["BTC/USDT"]
    timeframe: str = "15m"
    stoploss: float = -0.03
    wallet: float = 1000
    confirmation: str = ""
    override_safety: bool = False  # True = user acknowledged warnings, don't hard-block


@router.post("/start")
@limiter.limit(TRADE_LIMIT)
def start_trading(
    req: StartRequest,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    cfg_result = db.execute(select(Config).where(Config.user_id == user_id).limit(1))
    config = cfg_result.scalar_one_or_none()
    if not config:
        return {"error": "Not configured. Complete setup first."}

    from sqlalchemy import or_
    strat_result = db.execute(
        select(Strategy).where(
            Strategy.id == req.strategy_id,
            or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa: E712
        )
    )
    strategy = strat_result.scalar_one_or_none()
    if not strategy:
        return {"error": "Strategy not found"}

    # Extract class name — works for both Freqtrade-style (class X(IStrategy))
    # and plain classes (class SimpleTargetStrategy:)
    strategy_name = strategy.name or f"strategy_{strategy.id}"
    for line in (strategy.generated_code or "").split("\n"):
        line = line.strip()
        if line.startswith("class "):
            strategy_name = line.split("(")[0].split(":")[0].replace("class ", "").strip()
            break

    # Use Freqtrade if actually importable; otherwise use the native engine.
    # shutil.which / path checks are unreliable when FREQTRADE_PATH points to
    # a non-existent file (e.g. /opt/venv/bin/freqtrade on Railway without it).
    from backend.services.native_trading_engine import native_engine_registry
    try:
        import freqtrade as _ft_mod  # noqa: F401
        _ft_available = True
    except ImportError:
        _ft_available = False

    if _ft_available:
        mgr_engine = freqtrade_mgr.for_user(user_id)
        _use_native = False
    else:
        mgr_engine = native_engine_registry.for_user(user_id)
        _use_native = True

    if req.mode == "live":
        if req.confirmation != "CONFIRM":
            return {"error": "Must type 'CONFIRM' to start live trading"}

        risk_mgr = RiskManager(config)
        safety = risk_mgr.check_live_safety(db, user_id)
        if not safety["safe"]:
            if not req.override_safety:
                # Hard block — user hasn't acknowledged warnings yet
                return {"error": "Safety checks failed", "details": safety["errors"]}
            # override_safety=True: user acknowledged all warnings, log but proceed
            log.warning(
                "User %s starting live trade with safety overrides: %s",
                user_id, safety["errors"],
            )

        try:
            kucoin_key = decrypt(config.kucoin_key_enc, user_id)
            kucoin_secret = decrypt(config.kucoin_secret_enc, user_id)
            kucoin_passphrase = decrypt(config.kucoin_passphrase_enc, user_id)
        except DecryptError:
            return {
                "error": (
                    "Your KuCoin API credentials could not be decrypted. "
                    "Please go to Setup and re-enter your KuCoin API keys."
                )
            }

        result = mgr_engine.start_live(
            strategy_name=strategy_name,
            pairs=req.pairs,
            timeframe=req.timeframe,
            stoploss=req.stoploss,
            kucoin_key=kucoin_key,
            kucoin_secret=kucoin_secret,
            kucoin_passphrase=kucoin_passphrase,
            wallet=req.wallet,
            max_open_trades=config.max_open_trades or 3,
            max_position_pct=config.max_position_pct or 5.0,
            trailing_stop_pct=getattr(config, "trailing_stop_pct", 0.0) or 0.0,
            take_profit_pct=getattr(config, "take_profit_pct", 0.0) or 0.0,
            position_adjustment=bool(getattr(config, "position_adjustment", False)),
        )
    else:
        result = mgr_engine.start_paper(
            strategy_name=strategy_name,
            pairs=req.pairs,
            timeframe=req.timeframe,
            stoploss=req.stoploss,
            wallet=req.wallet,
            max_open_trades=config.max_open_trades or 3,
            max_position_pct=config.max_position_pct or 5.0,
            trailing_stop_pct=getattr(config, "trailing_stop_pct", 0.0) or 0.0,
            take_profit_pct=getattr(config, "take_profit_pct", 0.0) or 0.0,
            position_adjustment=bool(getattr(config, "position_adjustment", False)),
            strategy_id=req.strategy_id,
        )

    # Persist bot state to DB so it auto-resumes after container restart
    if result.get("started"):
        try:
            config.bot_running = True
            config.bot_mode = req.mode
            config.bot_strategy_name = strategy_name
            config.bot_pairs = ",".join(req.pairs)
            config.bot_timeframe = req.timeframe
            config.bot_wallet = req.wallet
            config.bot_stoploss = req.stoploss
            db.commit()
        except Exception:
            pass

    log_event(
        db, user_id, "trade.start", request,
        mode=req.mode, strategy_id=req.strategy_id, pair=",".join(req.pairs),
        payload={
            "timeframe": req.timeframe,
            "stoploss": req.stoploss,
            "wallet": req.wallet,
            "result": result,
        },
    )
    return result


@router.post("/stop")
@limiter.limit(TRADE_LIMIT)
async def stop_trading(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import native_engine_registry
    # Stop whichever engine is running
    ft_result  = freqtrade_mgr.for_user(user_id).stop()
    nat_result = native_engine_registry.for_user(user_id).stop()
    result = ft_result if ft_result.get("stopped") else nat_result

    # Clear bot_running flag so it does NOT auto-resume on next container restart
    try:
        from sqlalchemy import select as _sel
        cfg = db.execute(_sel(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            cfg.bot_running = False
            db.commit()
    except Exception:
        pass

    log_event(db, user_id, "trade.stop", request, payload={"result": result})
    return result


@router.get("/status")
async def get_status(user_id: str = Depends(get_user_id)):
    from backend.services.native_trading_engine import native_engine_registry
    ft_status  = freqtrade_mgr.for_user(user_id).status
    nat_status = native_engine_registry.for_user(user_id).status
    # Return whichever is running (native takes priority on Railway)
    if nat_status.get("running"):
        return nat_status
    return ft_status


@router.get("/open-positions")
async def get_open_positions(user_id: str = Depends(get_user_id)):
    from backend.services.native_trading_engine import native_engine_registry
    return {"positions": native_engine_registry.for_user(user_id).get_open_positions()}


@router.get("/trade-history")
async def get_trade_history(user_id: str = Depends(get_user_id)):
    from backend.services.native_trading_engine import native_engine_registry
    return {"trades": native_engine_registry.for_user(user_id).get_trades()}


class ManualEntryRequest(BaseModel):
    pair: str = "BTC/USDT"
    direction: str = "long"   # "long" or "short"
    stake: float = 0          # 0 = use engine's default risk_pct


@router.post("/manual-entry")
def manual_entry(
    req: ManualEntryRequest,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Immediately enter a paper position at the current live market price.
    Used by the Buy Now / Sell Now manual buttons in the UI.
    The bot does NOT need to be running to use this — it starts a
    lightweight in-memory session automatically if needed.
    """
    from backend.services.native_trading_engine import native_engine_registry
    eng = native_engine_registry.for_user(user_id)

    # Auto-start in paper mode if not already running
    if not eng.is_running:
        cfg = db.execute(
            select(Config).where(Config.user_id == user_id).limit(1)
        ).scalar_one_or_none()
        wallet = (cfg.bot_wallet or 1000.0) if cfg else 1000.0
        stoploss = (cfg.bot_stoploss or -0.03) if cfg else -0.03
        eng._wallet  = wallet
        eng._stoploss = stoploss
        eng._risk_pct = (cfg.max_position_pct or 5.0) / 100.0 if cfg else 0.05
        eng.balance  = eng.balance if eng.balance > 0 else wallet
        eng._mode    = "paper"

    result = eng.manual_entry(req.pair, req.direction, req.stake)
    log_event(db, user_id, "trade.manual_entry", None,
              payload={"pair": req.pair, "direction": req.direction, "result": result})
    return result


@router.post("/sync")
def sync_from_freqtrade(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Manually pull trades from freqtrade's SQLite DBs into our table."""
    return sync_trades(db, user_id)


@router.get("/open")
def get_open_trades(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import native_engine_registry

    # 1. Native engine open positions (paper/live bot running in this container)
    native_positions = native_engine_registry.for_user(user_id).get_open_positions()
    # Fetch live prices for unrealized P&L calculation
    live_prices: dict[str, float] = {}
    for p in native_positions:
        try:
            sym = p["pair"].replace("/", "-")
            data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
            if str(data.get("code")) == "200000":
                live_prices[p["pair"]] = float(data["data"]["price"])
        except Exception:
            pass

    native_trades = []
    for p in native_positions:
        entry = p.get("entry", 0)
        stake = p.get("stake", 0)
        cur = live_prices.get(p["pair"], entry)
        direction = p.get("direction", "long")
        if direction == "long":
            unreal = (cur - entry) / entry * stake if entry else 0
        else:
            unreal = (entry - cur) / entry * stake if entry else 0
        native_trades.append({
            "id": f"native-{p['pair']}",
            "pair": p["pair"],
            "side": direction,
            "entry_price": entry,
            "current_price": round(cur, 6),
            "amount": stake,
            "stoploss_price": p.get("sl"),
            "tp_price": p.get("tp"),          # take-profit level for chart
            "entry_time": p.get("opened_at"),
            "mode": "paper",
            "unrealized_pnl": round(unreal, 4),
        })

    # 2. DB-persisted trades (from Freqtrade sync or previous sessions)
    try:
        sync_trades(db, user_id)
    except Exception:
        pass
    result = db.execute(
        select(Trade)
        .where(Trade.status == "open", Trade.user_id == user_id)
        .order_by(desc(Trade.entry_time))
    )
    db_trades = [
        {
            "id": t.id,
            "pair": t.pair,
            "side": t.side,
            "entry_price": t.entry_price,
            "amount": t.amount,
            "stoploss_price": t.stoploss_price,
            "entry_time": str(t.entry_time),
            "mode": t.mode,
            "unrealized_pnl": 0,
        }
        for t in result.scalars().all()
    ]

    # Merge: native positions take priority; avoid duplicates by pair
    pairs_in_native = {p["pair"] for p in native_positions}
    merged = native_trades + [t for t in db_trades if t["pair"] not in pairs_in_native]
    return {"trades": merged}


@router.get("/history")
def get_trade_history(
    mode: str = None,
    strategy_id: int = None,
    limit: int = 100,
    offset: int = 0,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import native_engine_registry

    # 1. In-memory closed trades from native engine (current session)
    native_closed = native_engine_registry.for_user(user_id).get_trades()
    native_trade_list = [
        {
            "id": f"native-{i}",
            "pair": t.get("pair"),
            "side": t.get("direction", "long"),
            "entry_price": t.get("open_rate"),
            "exit_price": t.get("close_rate"),
            "amount": t.get("stake", 0),
            "profit_pct": t.get("profit_pct", 0),
            "profit_abs": t.get("profit_abs", 0),
            "entry_time": t.get("open_date"),
            "exit_time": t.get("close_date"),
            "exit_reason": t.get("exit_reason"),
            "mode": "paper",
            "strategy_id": None,
        }
        for i, t in enumerate(native_closed)
        if not mode or mode == "paper"
    ]

    # 2. DB-persisted trades from Freqtrade sync / previous sessions
    try:
        sync_trades(db, user_id)
    except Exception:
        pass
    query = (
        select(Trade)
        .where(Trade.status == "closed", Trade.user_id == user_id)
        .order_by(desc(Trade.exit_time))
    )
    if mode:
        query = query.where(Trade.mode == mode)
    if strategy_id:
        query = query.where(Trade.strategy_id == strategy_id)
    query = query.limit(limit).offset(offset)
    db_trades = [
        {
            "id": t.id,
            "pair": t.pair,
            "side": t.side,
            "entry_price": t.entry_price,
            "exit_price": t.exit_price,
            "amount": t.amount,
            "profit_pct": t.profit_pct,
            "profit_abs": t.profit_abs,
            "entry_time": str(t.entry_time),
            "exit_time": str(t.exit_time),
            "exit_reason": t.exit_reason,
            "mode": t.mode,
            "strategy_id": t.strategy_id,
        }
        for t in db.execute(query).scalars().all()
    ]

    # DB trades now include persisted native trades, so in-memory list is
    # only a fallback for trades that happened before the persistence fix.
    # Deduplicate by (pair, entry_price, entry_time) to avoid showing the same trade twice.
    db_keys = {
        (str(t["pair"]), str(t["entry_price"]), str(t["entry_time"])[:16])
        for t in db_trades
    }
    unique_native = [
        t for t in native_trade_list
        if (str(t["pair"]), str(t["entry_price"]), str(t["entry_time"])[:16]) not in db_keys
    ]
    merged = unique_native + db_trades
    return {"trades": merged[:limit]}


@router.post("/force-close/{trade_id:path}")
def force_close(
    trade_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Close a trade — handles both native engine positions (id='native-BTC/USDT')
    and DB-persisted trades (id='42')."""
    from backend.services.native_trading_engine import native_engine_registry, _kucoin_get

    # ── Native engine position (id starts with "native-") ──────────────
    if trade_id.startswith("native-"):
        pair = trade_id[len("native-"):]   # e.g. "BTC/USDT"
        eng = native_engine_registry.for_user(user_id)
        with eng._lock:
            pos = eng.positions.get(pair)
            if pos is None:
                return {"error": f"No open native position for {pair}"}
            # Fetch current price for a realistic exit
            try:
                symbol = pair.replace("/", "-")
                data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": symbol})
                exit_price = float(data["data"]["price"]) if str(data.get("code")) == "200000" else pos.entry
            except Exception:
                exit_price = pos.entry
            from datetime import timezone as _tz
            pos.close(exit_price, "force_closed", datetime.now(_tz.utc))
            eng.balance += pos.pnl_abs
            eng.closed_trades.append(pos)
            del eng.positions[pair]
        # Persist the closed trade to DB (updates open row if db_id set, else inserts)
        from backend.services.native_trading_engine import _persist_closed_trade
        _persist_closed_trade(user_id, pos, eng._mode, eng._strategy_id, pos.db_id)
        log_event(db, user_id, "trade.force_close", request,
                  payload={"pair": pair, "exit_price": exit_price})
        return {"status": "closed", "pair": pair, "exit_price": exit_price,
                "pnl_abs": round(pos.pnl_abs, 4)}

    # ── DB trade (numeric id) ───────────────────────────────────────────
    try:
        int_id = int(trade_id)
    except ValueError:
        return {"error": f"Invalid trade_id '{trade_id}'"}

    result = db.execute(
        select(Trade).where(
            Trade.id == int_id,
            Trade.status == "open",
            Trade.user_id == user_id,
        )
    )
    trade = result.scalar_one_or_none()
    if not trade:
        return {"error": "Open trade not found"}

    trade.status = "closed"
    trade.exit_time = datetime.utcnow()
    trade.exit_reason = "force_closed"
    db.commit()

    log_event(
        db, user_id, "trade.force_close", request,
        mode=trade.mode, strategy_id=trade.strategy_id, pair=trade.pair,
        payload={"trade_id": int_id},
    )
    return {"status": "closed", "trade_id": int_id}


@router.post("/emergency-stop")
def emergency_stop(
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    stop_result = freqtrade_mgr.for_user(user_id).stop()

    result = db.execute(
        select(Trade).where(Trade.status == "open", Trade.user_id == user_id)
    )
    open_trades = result.scalars().all()
    closed_count = 0
    for trade in open_trades:
        trade.status = "closed"
        trade.exit_time = datetime.utcnow()
        trade.exit_reason = "emergency_stop"
        closed_count += 1

    db.commit()

    log_event(
        db, user_id, "trade.emergency_stop", request,
        payload={"trades_closed": closed_count, "bot": stop_result},
    )

    return {
        "stopped": True,
        "trades_closed": closed_count,
        "bot": stop_result,
    }


@router.get("/audit")
def get_audit_log(
    limit: int = 100,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Last N audit events for the current user. Read-only."""
    from backend.models.audit import TradeAudit
    rows = db.execute(
        select(TradeAudit)
        .where(TradeAudit.user_id == user_id)
        .order_by(desc(TradeAudit.created_at))
        .limit(min(limit, 500))
    ).scalars().all()
    return {
        "events": [
            {
                "id": r.id,
                "event": r.event,
                "mode": r.mode,
                "strategy_id": r.strategy_id,
                "pair": r.pair,
                "payload": r.payload,
                "actor_ip": r.actor_ip,
                "created_at": str(r.created_at),
            }
            for r in rows
        ]
    }
