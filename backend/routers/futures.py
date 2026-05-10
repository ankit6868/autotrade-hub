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


# ── Futures Backtest ─────────────────────────────────────────────────────────

@router.post("/backtest/run")
def run_futures_backtest(
    req: dict,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Run a leveraged futures backtest using historical KuCoin candle data."""
    from sqlalchemy import or_
    from backend.models.strategy import Strategy
    from backend.models.trade import FuturesBacktest
    from backend.services.futures_backtester import run_futures_backtest as _run

    strategy_id      = req.get("strategy_id")
    pairs            = req.get("pairs", ["BTC/USDT"])
    timeframe        = req.get("timeframe", "15m")
    timerange        = req.get("timerange", "20240101-20240401")
    leverage         = int(req.get("leverage", 10))
    starting_balance = float(req.get("starting_balance", 1000))
    stoploss_pct     = float(req.get("stoploss_pct", 3.0))
    take_profit_pct  = float(req.get("take_profit_pct", 1.5))

    # Resolve strategy
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    strategy = None
    if strategy_id:
        strategy = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),  # noqa
            )
        ).scalar_one_or_none()
        if strategy:
            strategy_name = strategy.name

    result = _run(
        strategy_name    = strategy_name,
        pairs            = pairs,
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = starting_balance,
        stoploss_pct     = stoploss_pct,
        take_profit_pct  = take_profit_pct,
    )

    if "error" in result:
        return result

    m = result["metrics"]

    # Persist to DB
    bt = FuturesBacktest(
        user_id          = user_id,
        strategy_id      = strategy_id,
        strategy_name    = strategy_name,
        pairs            = ",".join(pairs),
        timeframe        = timeframe,
        timerange        = timerange,
        leverage         = leverage,
        starting_balance = starting_balance,
        final_balance    = m["final_balance"],
        total_profit_pct = m["total_profit_pct"],
        total_profit_abs = m["total_profit_abs"],
        win_rate         = m["win_rate"],
        max_drawdown     = m["max_drawdown"],
        total_trades     = m["total_trades"],
        winning_trades   = m["winning_trades"],
        losing_trades    = m["losing_trades"],
        liquidations     = m["liquidations"],
        long_trades      = m["long_trades"],
        short_trades     = m["short_trades"],
        avg_leverage_pnl = m["avg_leverage_pnl"],
        results_json     = select_desc_json(result),
    )
    db.add(bt)
    db.commit()
    db.refresh(bt)

    return {**result, "id": bt.id}


def select_desc_json(result: dict) -> str:
    """Store only metrics + trade count (not full trade list) to keep DB rows small."""
    import json
    return json.dumps({
        "metrics":      result.get("metrics", {}),
        "trade_count":  len(result.get("trades", [])),
        "equity_curve": result.get("equity_curve", [])[-50:],  # last 50 points
    })


@router.get("/backtest/history")
def futures_backtest_history(
    limit: int = 20,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Return past futures backtest runs for this user."""
    from backend.models.trade import FuturesBacktest
    from sqlalchemy import desc as sql_desc
    rows = db.execute(
        select(FuturesBacktest)
        .where(FuturesBacktest.user_id == user_id)
        .order_by(sql_desc(FuturesBacktest.created_at))
        .limit(limit)
    ).scalars().all()
    return {
        "backtests": [
            {
                "id":               r.id,
                "strategy_name":    r.strategy_name,
                "pairs":            r.pairs,
                "timeframe":        r.timeframe,
                "timerange":        r.timerange,
                "leverage":         r.leverage,
                "starting_balance": r.starting_balance,
                "final_balance":    r.final_balance,
                "total_profit_pct": r.total_profit_pct,
                "win_rate":         r.win_rate,
                "max_drawdown":     r.max_drawdown,
                "total_trades":     r.total_trades,
                "liquidations":     r.liquidations,
                "created_at":       str(r.created_at),
            }
            for r in rows
        ]
    }


# ── One-time cleanup ──────────────────────────────────────────────────────────

@router.delete("/cleanup-test-trades")
def cleanup_test_trades(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Delete all open futures trades for this user (removes stale test records)."""
    from sqlalchemy import delete as sql_delete
    result = db.execute(
        sql_delete(Trade).where(
            Trade.user_id == user_id,
            Trade.market_type == "futures",
            Trade.status == "open",
        )
    )
    db.commit()
    return {"deleted": result.rowcount, "user_id": user_id}


# ── Balance ──────────────────────────────────────────────────────────────────

@router.get("/balance")
def futures_balance(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Fetch KuCoin Futures account USDT balance (real money, live accounts only)."""
    from backend.services.native_trading_engine import _kucoin_get_signed
    from backend.utils.encryption import decrypt, DecryptError

    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        return {"error": "Add your KuCoin Futures API key in Setup first.", "balance": None}
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return {"error": "Could not decrypt KuCoin credentials. Re-enter in Setup.", "balance": None}

    # Guard: if keys are blank, no point calling the API
    if not kk or not ks:
        return {"error": "KuCoin Futures API key not configured. Go to Setup → add your Futures key.", "balance": None}

    try:
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        # Correct KuCoin Futures endpoint: /api/v1/account-overview (hyphen, not camelCase)
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"},
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) != "200000":
            msg = data.get("msg", "unknown")
            # KuCoin error codes: 400100 = invalid credentials, 400006 = no permission
            code = str(data.get("code", ""))
            if code in ("400100", "400006", "400200"):
                return {"error": "Invalid API key or missing Futures permissions. Check Setup.", "balance": None}
            return {"error": f"KuCoin Futures API error {code}: {msg}", "balance": None}
        account    = data.get("data", {})
        available  = float(account.get("availableBalance", 0))
        total      = float(account.get("accountEquity", available))
        unrealized = float(account.get("unrealisedPNL", 0))
        return {
            "balance":    round(available, 4),
            "equity":     round(total, 4),
            "unrealized": round(unrealized, 4),
            "currency":   "USDT",
            "type":       "futures",
        }
    except Exception as e:
        err = str(e)
        if "404" in err:
            return {"error": "KuCoin Futures API endpoint not found. Check your Futures API key.", "balance": None}
        if "403" in err or "401" in err:
            return {"error": "KuCoin API permission denied. Ensure your Futures key has 'General' permission.", "balance": None}
        return {"error": f"Could not reach KuCoin Futures: {err}", "balance": None}


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
    return eng.status   # status is a @property, NOT a method — no () needed


# ── Open Positions ────────────────────────────────────────────────────────────

@router.get("/open")
def futures_open_positions(
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    from backend.services.native_trading_engine import _kucoin_get

    eng = futures_engine_registry.for_user(user_id)
    # Only show in-memory positions when engine is running in the REQUESTED mode.
    # Prevents paper bot positions leaking into the live page and vice-versa.
    native_positions = eng.get_open_positions() if (mode is None or eng._mode == mode) else []

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

    db_rows = db.execute(query).scalars().all()

    # Fetch live prices for DB-only pairs not already retrieved above
    pairs_in_native = {p["pair"] for p in native_positions}
    db_only_pairs   = {t.pair for t in db_rows if t.pair not in pairs_in_native}
    for pair_name in db_only_pairs:
        if pair_name not in live_prices:
            try:
                sym  = pair_name.replace("/", "-")
                data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
                if str(data.get("code")) == "200000":
                    live_prices[pair_name] = float(data["data"]["price"])
            except Exception:
                pass

    db_trades = []
    for t in db_rows:
        entry   = t.entry_price or 0
        cur     = live_prices.get(t.pair, entry)
        lev     = t.leverage or 1
        side    = t.side or "long"
        raw_pnl = (cur - entry) / entry if (entry and side == "long") \
                  else (entry - cur) / entry if entry else 0
        unreal  = round(t.amount * raw_pnl * lev, 4) if entry else 0
        db_trades.append({
            "id":                t.id,
            "pair":              t.pair,
            "side":              side,
            "entry_price":       entry,
            "current_price":     round(cur, 6) if cur != entry else 0,
            "amount":            t.amount,
            "leverage":          lev,
            "liquidation_price": t.liquidation_price,
            "stoploss_price":    t.stoploss_price,
            "entry_time":        str(t.entry_time),
            "mode":              t.mode,
            "market_type":       "futures",
            "unrealized_pnl":    unreal,
        })

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


# ── Manual Entry (futures) ────────────────────────────────────────────────────

@router.post("/manual-entry")
def futures_manual_entry(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """
    Manually open a futures position at the current market price.
    Uses the FuturesEngine — 100% isolated from the spot engine.
    The position appears ONLY in futures open positions / trade log, never spot.
    """
    from backend.services.native_trading_engine import _kucoin_get, _persist_open_trade
    from backend.services.futures_engine import FuturesPosition, _calc_liquidation_price
    from datetime import datetime, timezone as _tz

    pair          = req.get("pair", "BTC/USDT")
    direction     = req.get("direction", "long").lower()   # "long" or "short"
    stake_pct     = float(req.get("stake_pct", 5.0))       # % of balance as margin
    req_leverage  = req.get("leverage")                    # optional override from UI

    eng = futures_engine_registry.for_user(user_id)

    # Default sensible values if engine hasn't been started yet
    # NOTE: eng._leverage defaults to 1 (not None), so check > 1 to detect "not configured"
    # UI can also pass leverage directly (for standalone manual entries without bot running)
    leverage = int(req_leverage) if req_leverage else (eng._leverage if (eng._leverage and eng._leverage > 1) else 10)
    mode     = eng._mode     if eng._mode     else "paper"
    balance  = eng.balance   if eng.balance   else 1000.0
    sl_pct   = abs(eng._stoploss or 0.015)
    # Fix: engine stores _take_profit as decimal (0.03), not percent (3.0)
    _raw_tp = getattr(eng, "_take_profit", None) or getattr(eng, "_take_profit_pct", None)
    tp_pct   = float(_raw_tp) * 100 if (_raw_tp and float(_raw_tp) <= 1) else (float(_raw_tp) if _raw_tp else 3.0)

    # Fetch current price from KuCoin
    try:
        sym  = pair.replace("/", "-")
        data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
        if str(data.get("code")) != "200000":
            return {"error": f"Could not fetch price for {pair}"}
        entry_price = float(data["data"]["price"])
    except Exception as e:
        return {"error": f"Price fetch failed: {e}"}

    stake = balance * (stake_pct / 100)

    if direction == "long":
        sl_price = round(entry_price * (1 - sl_pct), 6)
        tp_price = round(entry_price * (1 + tp_pct / 100), 6)
    else:
        sl_price = round(entry_price * (1 + sl_pct), 6)
        tp_price = round(entry_price * (1 - tp_pct / 100), 6)

    # Build a FuturesPosition (liquidation is computed inside __init__)
    now = datetime.now(_tz.utc)
    pos = FuturesPosition(
        pair=pair, direction=direction,
        entry=entry_price, sl=sl_price, tp=tp_price,
        size=stake, leverage=leverage, opened_at=now,
    )

    # Guard: only allow 1 open position per pair at a time
    with eng._lock:
        existing_pairs = [p.pair for p in eng.positions.values()]
    if pair in existing_pairs:
        return {"error": f"Already have an open position for {pair}. Close it first."}

    pos_key = f"{pair}-{direction}-manual-{int(now.timestamp())}"
    with eng._lock:
        eng.positions[pos_key] = pos

    # Persist to DB — market_type="futures" ensures it's NOT mixed with spot
    db_id = _persist_open_trade(
        user_id, pos, mode,
        strategy_id  = eng._strategy_id,
        leverage     = leverage,
        market_type  = "futures",
    )
    pos.db_id = db_id

    log_event(db, user_id, "futures.manual_entry", request, payload={
        "pair": pair, "direction": direction, "entry": entry_price,
        "leverage": leverage, "mode": mode,
    })
    return {
        "entered": True,
        "pair": pair,
        "direction": direction,
        "entry": entry_price,
        "sl": sl_price,
        "tp": tp_price,
        "liq": pos.liquidation_price,
        "leverage": leverage,
        "mode": mode,
    }


# ── Force Close ───────────────────────────────────────────────────────────────

@router.post("/force-close/{pair:path}")
def futures_force_close(
    pair: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Close ALL open in-memory + DB futures positions for the given pair."""
    from backend.services.native_trading_engine import _kucoin_get, _persist_closed_trade
    from sqlalchemy import update as sql_update
    from datetime import timezone as _tz

    eng = futures_engine_registry.for_user(user_id)

    # Fetch live exit price once
    try:
        sym  = pair.replace("/", "-")
        data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
        exit_price = float(data["data"]["price"]) if str(data.get("code")) == "200000" else None
    except Exception:
        exit_price = None

    now = datetime.now(_tz.utc)
    closed_positions = []

    # ── Close all in-memory positions for this pair ────────────────────────
    with eng._lock:
        matching_keys = [k for k, p in eng.positions.items() if p.pair == pair]
        if not matching_keys:
            # No in-memory positions — still close DB ones below
            pass
        for trade_key in matching_keys:
            pos = eng.positions.pop(trade_key)
            ep  = exit_price or pos.entry
            pos.close(ep, "force_closed", now)
            eng.balance += pos.pnl_abs
            eng.closed_trades.append(pos)
            closed_positions.append(pos)

    # Persist each closed in-memory position
    total_pnl = 0.0
    for pos in closed_positions:
        _persist_closed_trade(user_id, pos, eng._mode, eng._strategy_id, pos.db_id)
        total_pnl += pos.pnl_abs

    # ── Also close any orphaned open DB positions for this pair ───────────
    # (positions that exist in DB but not in engine memory — from previous sessions)
    orphan_trades = db.execute(
        select(Trade).where(
            Trade.user_id    == user_id,
            Trade.pair       == pair,
            Trade.market_type == "futures",
            Trade.status     == "open",
        )
    ).scalars().all()

    for t in orphan_trades:
        ep = exit_price or t.entry_price
        t.exit_price  = ep
        t.exit_time   = now
        t.exit_reason = "force_closed"
        t.status      = "closed"
        # Fix: SHORT P&L is inverted vs LONG
        side = getattr(t, "side", "long") or "long"
        if side == "short":
            t.profit_pct = round((t.entry_price - ep) / t.entry_price * 100 * (t.leverage or 1), 4)
        else:
            t.profit_pct = round((ep - t.entry_price) / t.entry_price * 100 * (t.leverage or 1), 4)
        t.profit_abs  = round(t.amount * t.profit_pct / 100, 4)
        total_pnl    += t.profit_abs

    if orphan_trades:
        db.commit()

    total_closed = len(closed_positions) + len(orphan_trades)
    if total_closed == 0:
        return {"error": f"No open futures position for {pair}"}

    log_event(db, user_id, "futures.force_close", request,
              payload={"pair": pair, "exit_price": exit_price, "count": total_closed})
    return {
        "status":       "closed",
        "pair":         pair,
        "exit_price":   exit_price,
        "closed_count": total_closed,
        "pnl_abs":      round(total_pnl, 4),
    }
