"""
Futures trading endpoints — paper and live with leverage.
Completely isolated from spot trading (different market_type='futures').

Includes: order book, recent trades, manual order placement, leverage/margin control,
pending orders, positions, bot management, and account overview.
"""
from __future__ import annotations

import time as _time
from datetime import datetime
from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session
from sqlalchemy import select, desc

from backend.models import get_db
from backend.models.trade import Trade, StrategyInstance, FuturesOrder
from backend.models.config import Config
from backend.utils.clerk_auth import get_user_id
from backend.services.futures_engine import futures_engine_registry
from backend.utils.audit import log_event

router = APIRouter(prefix="/api/futures", tags=["futures"])

# Simple in-memory cache for order book / trades (avoid hammering KuCoin)
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 1.5  # seconds


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
    In live mode, places a real order via KuCoin Lead Trading Futures API.
    """
    from backend.services.native_trading_engine import _kucoin_get, _kucoin_post_signed, _persist_open_trade
    from backend.services.futures_engine import FuturesPosition, _calc_liquidation_price, KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    from datetime import datetime, timezone as _tz

    pair          = req.get("pair", "BTC/USDT")
    direction     = req.get("direction", "long").lower()
    stake_pct     = float(req.get("stake_pct", 5.0))
    req_leverage  = req.get("leverage")

    eng = futures_engine_registry.for_user(user_id)

    leverage = int(req_leverage) if req_leverage else (eng._leverage if (eng._leverage and eng._leverage > 1) else 10)
    mode     = eng._mode     if eng._mode     else "paper"
    balance  = eng.balance   if eng.balance   else 1000.0
    sl_pct   = abs(eng._stoploss or 0.015)
    _raw_tp = getattr(eng, "_take_profit", None) or getattr(eng, "_take_profit_pct", None)
    tp_pct   = float(_raw_tp) * 100 if (_raw_tp and float(_raw_tp) <= 1) else (float(_raw_tp) if _raw_tp else 3.0)

    # Fetch current price
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

    now = datetime.now(_tz.utc)
    pos = FuturesPosition(
        pair=pair, direction=direction,
        entry=entry_price, sl=sl_price, tp=tp_price,
        size=stake, leverage=leverage, opened_at=now,
    )

    with eng._lock:
        existing_pairs = [p.pair for p in eng.positions.values()]
    if pair in existing_pairs:
        return {"error": f"Already have an open position for {pair}. Close it first."}

    # ── Live mode: place real order via Lead Trading API ──────────────
    exchange_order_id = None
    if mode == "live" and eng._api_key:
        try:
            kc_symbol     = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
            side          = "buy" if direction == "long" else "sell"
            position_side = "LONG" if direction == "long" else "SHORT"
            contract_size = stake * leverage
            contracts     = max(1, int(contract_size / entry_price * 1000))
            client_oid    = f"atf-manual-{int(time.time()*1000)}"
            body = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       kc_symbol,
                "type":         "market",
                "size":         contracts,
                "leverage":     leverage,
                "marginMode":   "ISOLATED",
                "positionSide": position_side,
            }
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(resp.get("code")) != "200000":
                return {"error": f"KuCoin Lead Trading order failed: {resp.get('msg', resp)}"}
            exchange_order_id = resp.get("data", {}).get("orderId")
            log.info("[%s] Lead Trading manual ENTRY: %s", user_id, resp)
        except Exception as e:
            return {"error": f"Lead Trading order failed: {e}"}

    pos_key = f"{pair}-{direction}-manual-{int(now.timestamp())}"
    with eng._lock:
        eng.positions[pos_key] = pos

    db_id = _persist_open_trade(
        user_id, pos, mode,
        strategy_id  = eng._strategy_id,
        leverage     = leverage,
        market_type  = "futures",
    )
    pos.db_id = db_id

    log_event(db, user_id, "futures.manual_entry", request, payload={
        "pair": pair, "direction": direction, "entry": entry_price,
        "leverage": leverage, "mode": mode, "exchange_order_id": exchange_order_id,
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
        "exchange_order_id": exchange_order_id,
    }


# ── Force Close ───────────────────────────────────────────────────────────────

@router.post("/force-close/{pair:path}")
def futures_force_close(
    pair: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Close ALL open futures positions for the given pair.
    In live mode, places close orders via KuCoin Lead Trading API."""
    from backend.services.native_trading_engine import _kucoin_get, _kucoin_post_signed, _persist_closed_trade
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    from sqlalchemy import update as sql_update
    from datetime import timezone as _tz

    eng = futures_engine_registry.for_user(user_id)

    # Fetch live exit price
    try:
        sym  = pair.replace("/", "-")
        data = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym})
        exit_price = float(data["data"]["price"]) if str(data.get("code")) == "200000" else None
    except Exception:
        exit_price = None

    now = datetime.now(_tz.utc)
    closed_positions = []
    mode = eng._mode or "paper"

    # ── Close all in-memory positions for this pair ────────────────────────
    with eng._lock:
        matching_keys = [k for k, p in eng.positions.items() if p.pair == pair]
        for trade_key in matching_keys:
            pos = eng.positions.pop(trade_key)
            ep  = exit_price or pos.entry
            pos.close(ep, "force_closed", now)
            eng.balance += pos.pnl_abs
            eng.closed_trades.append(pos)
            closed_positions.append(pos)

    # ── Live mode: place close orders on KuCoin Lead Trading ─────────────
    if mode == "live" and eng._api_key and closed_positions:
        kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        for pos in closed_positions:
            try:
                side          = "sell" if pos.direction == "long" else "buy"
                position_side = "LONG" if pos.direction == "long" else "SHORT"
                contract_size = pos.size * getattr(pos, "leverage", eng._leverage or 10)
                contracts     = max(1, int(contract_size / pos.entry * 1000))
                body = {
                    "clientOid":   f"atf-close-{int(time.time()*1000)}",
                    "side":         side,
                    "symbol":       kc_symbol,
                    "type":         "market",
                    "size":         contracts,
                    "leverage":     getattr(pos, "leverage", eng._leverage or 10),
                    "marginMode":   "ISOLATED",
                    "positionSide": position_side,
                    "reduceOnly":   True,
                }
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                log.info("[%s] Lead Trading CLOSE order: %s", user_id, resp)
            except Exception as e:
                log.error("[%s] Lead Trading close failed for %s: %s", user_id, pair, e)

    # Persist each closed in-memory position
    total_pnl = 0.0
    for pos in closed_positions:
        _persist_closed_trade(user_id, pos, mode, eng._strategy_id, pos.db_id)
        total_pnl += pos.pnl_abs

    # ── Also close any orphaned open DB positions for this pair ───────────
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


# ── Order Book (proxied from KuCoin, cached) ─────────────────────────────

@router.get("/orderbook/{symbol}")
async def futures_orderbook(
    symbol: str,
    user_id: str = Depends(get_user_id),
):
    """Get futures order book (20 levels) — cached 1.5s."""
    cache_key = f"ob:{symbol}"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < CACHE_TTL:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        data = await client.get_order_book(symbol)
        result = {"symbol": symbol, "asks": data.get("asks", []), "bids": data.get("bids", []), "ts": data.get("ts")}
    except Exception:
        from backend.services.kucoin_futures_client import generate_paper_orderbook
        from backend.services.native_trading_engine import _kucoin_get
        pair = symbol.replace("USDTM", "/USDT").replace("-", "/")
        try:
            d = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": pair.replace("/", "-")})
            price = float(d["data"]["price"]) if str(d.get("code")) == "200000" else 50000
        except Exception:
            price = 50000
        ob = generate_paper_orderbook(price)
        result = {"symbol": symbol, "asks": ob["asks"], "bids": ob["bids"], "ts": ob["ts"]}

    _cache[cache_key] = (now, result)
    return result


@router.get("/trades/{symbol}")
async def futures_recent_trades(
    symbol: str,
    user_id: str = Depends(get_user_id),
):
    """Get recent futures trades for a symbol."""
    cache_key = f"rt:{symbol}"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < CACHE_TTL:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        trades = await client.get_recent_trades(symbol)
        result = {"symbol": symbol, "trades": trades[:50]}
    except Exception:
        result = {"symbol": symbol, "trades": []}

    _cache[cache_key] = (now, result)
    return result


@router.get("/contracts")
async def futures_contracts(
    user_id: str = Depends(get_user_id),
):
    """List available futures contracts."""
    cache_key = "contracts"
    now = _time.time()
    if cache_key in _cache and (now - _cache[cache_key][0]) < 60:
        return _cache[cache_key][1]

    try:
        from backend.services.kucoin_futures_client import KuCoinFuturesClient
        client = KuCoinFuturesClient()
        contracts = await client.get_contracts()
        result = {
            "contracts": [
                {
                    "symbol": c.get("symbol"),
                    "baseCurrency": c.get("baseCurrency"),
                    "multiplier": c.get("multiplier"),
                    "tickSize": c.get("tickSize"),
                    "lotSize": c.get("lotSize"),
                    "maxLeverage": c.get("maxLeverage"),
                    "isInverse": c.get("isInverse", False),
                    "status": c.get("status"),
                }
                for c in contracts
                if c.get("status") == "Open"
            ]
        }
    except Exception:
        result = {"contracts": []}

    _cache[cache_key] = (now, result)
    return result


# ── Pending Orders (limit/stop) ──────────────────────────────────────────

@router.post("/order")
def place_futures_order(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Place a futures order (limit, stop, etc.).
    In live mode, sends to KuCoin Lead Trading API."""
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    import time as _t

    symbol     = req.get("symbol", "XBTUSDTM")
    side       = req.get("side", "buy")
    order_type = req.get("order_type", "limit")
    size       = float(req.get("size", 0))
    price      = req.get("price")
    stop_price = req.get("stop_price")
    leverage   = req.get("leverage")
    tp_price   = req.get("tp_price")
    sl_price   = req.get("sl_price")
    hidden     = req.get("hidden", False)
    post_only  = req.get("post_only", False)
    reduce_only = req.get("reduce_only", False)
    time_in_force = req.get("time_in_force", "GTC")
    position_side = req.get("position_side")

    if size <= 0:
        return {"error": "size must be positive"}

    eng = futures_engine_registry.for_user(user_id)

    if price is not None:
        price = float(price)
    if stop_price is not None:
        stop_price = float(stop_price)
    if leverage is not None:
        leverage = int(leverage)

    lev = leverage or eng._leverage or 10
    mode = eng._mode or "paper"

    # Determine position side
    if not position_side:
        if reduce_only:
            position_side = "SHORT" if side == "buy" else "LONG"
        else:
            position_side = "LONG" if side == "buy" else "SHORT"

    # ── Live mode: send to Lead Trading API ──────────────────────────────
    exchange_order_id = None
    if mode == "live" and eng._api_key:
        try:
            client_oid = f"atf-ord-{int(_t.time()*1000)}"
            body: dict = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       symbol,
                "type":         order_type if order_type in ("market", "limit") else "limit",
                "size":         int(size),
                "leverage":     lev,
                "marginMode":   "ISOLATED",
                "positionSide": position_side,
            }
            if price is not None and order_type == "limit":
                body["price"] = str(price)
            if reduce_only:
                body["reduceOnly"] = True
            if time_in_force:
                body["timeInForce"] = time_in_force

            # TP/SL stop orders use a different endpoint
            if stop_price is not None or order_type in ("stop", "stop_limit"):
                body["stopPriceType"] = "TP"
                if stop_price:
                    if side == "buy":
                        body["triggerStopUpPrice"] = str(stop_price)
                    else:
                        body["triggerStopDownPrice"] = str(stop_price)
                body["reduceOnly"] = True
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/st-orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
            else:
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )

            if str(resp.get("code")) != "200000":
                return {"error": f"KuCoin Lead Trading: {resp.get('msg', resp)}"}
            exchange_order_id = resp.get("data", {}).get("orderId")
            log.info("[%s] Lead Trading order placed: %s", user_id, resp)
        except Exception as e:
            return {"error": f"Lead Trading order failed: {e}"}

    # Also track in engine (paper mode uses this for matching)
    result = eng.place_pending_order(
        symbol=symbol, side=side, order_type=order_type, size=size,
        price=price, stop_price=stop_price, leverage=leverage,
        tp_price=float(tp_price) if tp_price else None,
        sl_price=float(sl_price) if sl_price else None,
        hidden=hidden, post_only=post_only, reduce_only=reduce_only,
        time_in_force=time_in_force,
    )

    # Persist to DB
    order_rec = FuturesOrder(
        user_id=user_id, symbol=symbol, side=side, order_type=order_type,
        size=size, price=price, stop_price=stop_price,
        leverage=lev, margin_mode="isolated",
        client_oid=result.get("order_id"), status="pending",
        exchange_order_id=exchange_order_id,
        time_in_force=time_in_force, hidden=hidden, post_only=post_only,
        reduce_only=reduce_only, tp_price=float(tp_price) if tp_price else None,
        sl_price=float(sl_price) if sl_price else None,
    )
    db.add(order_rec)
    db.commit()
    db.refresh(order_rec)
    result["db_id"] = order_rec.id
    result["exchange_order_id"] = exchange_order_id

    log_event(db, user_id, "futures.place_order", request, payload=result)
    return result


@router.delete("/order/{order_id}")
def cancel_futures_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Cancel a pending futures order. In live mode, cancels on KuCoin Lead Trading."""
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    import urllib.request, json as _json

    eng = futures_engine_registry.for_user(user_id)
    mode = eng._mode or "paper"

    # ── Live mode: cancel on KuCoin Lead Trading ─────────────────────────
    if mode == "live" and eng._api_key:
        # Check if there's an exchange_order_id in the DB
        db_order = db.execute(
            select(FuturesOrder).where(
                FuturesOrder.client_oid == order_id,
                FuturesOrder.user_id == user_id,
            )
        ).scalar_one_or_none()

        if db_order and db_order.exchange_order_id:
            try:
                from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
                ts = str(int(time.time() * 1000))
                endpoint = f"/api/v1/copy-trade/futures/orders/{db_order.exchange_order_id}"
                headers = _sign_request(
                    eng._api_sec, eng._api_pass, eng._api_key,
                    ts, "DELETE", endpoint,
                )
                url = f"{_base}{endpoint}"
                req_obj = urllib.request.Request(url, headers=headers, method="DELETE")
                with urllib.request.urlopen(req_obj, timeout=15) as resp:
                    cancel_resp = _json.loads(resp.read().decode())
                log.info("[%s] Lead Trading cancel order: %s", user_id, cancel_resp)
            except Exception as e:
                log.error("[%s] Lead Trading cancel failed: %s", user_id, e)

    result = eng.cancel_pending_order(order_id)

    from sqlalchemy import update as sql_update
    db.execute(
        sql_update(FuturesOrder)
        .where(FuturesOrder.client_oid == order_id, FuturesOrder.user_id == user_id)
        .values(status="cancelled", cancelled_at=datetime.utcnow())
    )
    db.commit()

    log_event(db, user_id, "futures.cancel_order", request, payload={"order_id": order_id})
    return result


@router.get("/orders")
def get_futures_orders(
    symbol: str = None,
    status: str = "pending",
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get pending or filled futures orders."""
    eng = futures_engine_registry.for_user(user_id)
    # Combine engine pending + DB records
    engine_orders = eng.get_pending_orders(symbol)

    query = select(FuturesOrder).where(FuturesOrder.user_id == user_id)
    if status:
        query = query.where(FuturesOrder.status == status)
    if symbol:
        query = query.where(FuturesOrder.symbol == symbol)
    query = query.order_by(desc(FuturesOrder.created_at)).limit(100)

    db_orders = [
        {
            "order_id": o.client_oid or str(o.id),
            "db_id": o.id,
            "symbol": o.symbol,
            "side": o.side,
            "order_type": o.order_type,
            "size": o.size,
            "price": o.price,
            "stop_price": o.stop_price,
            "leverage": o.leverage,
            "margin_mode": o.margin_mode,
            "status": o.status,
            "filled_size": o.filled_size,
            "filled_price": o.filled_price,
            "tp_price": o.tp_price,
            "sl_price": o.sl_price,
            "created_at": str(o.created_at),
        }
        for o in db.execute(query).scalars().all()
    ]

    return {"orders": engine_orders if status == "pending" else db_orders}


@router.get("/orders/history")
def get_futures_order_history(
    symbol: str = None,
    limit: int = 50,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get filled/cancelled order history."""
    query = (
        select(FuturesOrder)
        .where(
            FuturesOrder.user_id == user_id,
            FuturesOrder.status.in_(["filled", "cancelled", "partially_filled"]),
        )
        .order_by(desc(FuturesOrder.created_at))
        .limit(limit)
    )
    if symbol:
        query = query.where(FuturesOrder.symbol == symbol)

    orders = [
        {
            "order_id": o.client_oid or str(o.id),
            "symbol": o.symbol, "side": o.side, "order_type": o.order_type,
            "size": o.size, "price": o.price, "filled_size": o.filled_size,
            "filled_price": o.filled_price, "fee": o.fee, "status": o.status,
            "created_at": str(o.created_at), "filled_at": str(o.filled_at) if o.filled_at else None,
        }
        for o in db.execute(query).scalars().all()
    ]
    return {"orders": orders}


# ── Leverage & Margin Mode ───────────────────────────────────────────────

@router.post("/leverage")
def set_futures_leverage(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set leverage for a symbol."""
    symbol   = req.get("symbol", "XBTUSDTM")
    leverage = int(req.get("leverage", 10))
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_leverage(symbol, leverage)
    log_event(db, user_id, "futures.set_leverage", request, payload=result)
    return result


@router.post("/margin-mode")
def set_futures_margin_mode(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set margin mode (cross/isolated) for a symbol."""
    symbol = req.get("symbol", "XBTUSDTM")
    mode   = req.get("mode", "cross")
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_margin(symbol, mode)
    log_event(db, user_id, "futures.set_margin_mode", request, payload=result)
    return result


@router.get("/leverage/{symbol}")
def get_futures_leverage(
    symbol: str,
    user_id: str = Depends(get_user_id),
):
    """Get current leverage for a symbol."""
    eng = futures_engine_registry.for_user(user_id)
    return {
        "symbol": symbol,
        "leverage": eng.get_symbol_leverage(symbol),
        "margin_mode": eng.get_symbol_margin(symbol),
    }


# ── Account Overview ─────────────────────────────────────────────────────

@router.get("/account")
def futures_account(
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Futures account overview.
    mode=paper → always return paper engine balance.
    mode=live or None → try KuCoin, fallback to paper."""
    from backend.utils.encryption import decrypt, DecryptError

    eng = futures_engine_registry.for_user(user_id)

    # Paper mode: skip KuCoin, return paper engine balance directly
    if mode == "paper":
        return _paper_account(eng)

    # Live mode: try to fetch live data from KuCoin Futures account
    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if cfg:
        try:
            kk = decrypt(cfg.kucoin_key_enc or "", user_id)
            ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
            kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
            if kk and ks:
                from backend.services.native_trading_engine import _kucoin_get_signed
                from backend.services.futures_engine import KUCOIN_FUTURES_BASE

                # Fetch account overview from KuCoin Futures
                # Lead Trading shares the same futures account — this is the real balance
                data = _kucoin_get_signed(
                    "/api/v1/account-overview", kk, ks, kp,
                    params={"currency": "USDT"},
                    base_url=KUCOIN_FUTURES_BASE,
                )
                if str(data.get("code")) == "200000":
                    acct = data.get("data", {})
                    account_equity = float(acct.get("accountEquity", 0))
                    margin_balance = float(acct.get("marginBalance", 0))
                    available_balance = float(acct.get("availableBalance", 0))
                    unrealised_pnl = float(acct.get("unrealisedPNL", 0))
                    position_margin = float(acct.get("positionMargin", 0))
                    order_margin = float(acct.get("orderMargin", 0))
                    frozen_funds = float(acct.get("frozenFunds", 0))
                    risk_ratio = float(acct.get("riskRatio", 0))
                    max_withdraw = float(acct.get("maxWithdrawAmount", 0))

                    return {
                        "mode": "live",
                        "source": "kucoin_lead_trading",
                        "balance": account_equity,
                        "margin_balance": margin_balance,
                        "equity": account_equity,
                        "available_balance": available_balance,
                        "available_margin": float(acct.get("availableMargin", available_balance)),
                        "unrealized_pnl": unrealised_pnl,
                        "used_margin": position_margin,
                        "order_margin": order_margin,
                        "margin_mode": "Cross",
                        "frozen_funds": frozen_funds,
                        "risk_ratio": risk_ratio,
                        "max_withdraw": max_withdraw,
                        "currency": acct.get("currency", "USDT"),
                    }
                else:
                    log.warning("KuCoin account-overview error: %s %s",
                                data.get("code"), data.get("msg"))
        except (DecryptError, Exception) as exc:
            log.warning("Failed to fetch KuCoin lead trading account for %s: %s", user_id, exc)

    # Fallback: paper account from engine state (no KuCoin keys configured)
    return _paper_account(eng)


def _paper_account(eng):
    """Return paper engine balance as account overview."""
    open_positions = eng.get_open_positions() if eng.is_running else []
    total_unrealized = sum(p.get("unrealized_pnl", 0) for p in open_positions)
    total_margin = sum(p.get("stake", 0) for p in open_positions)

    return {
        "mode": "paper",
        "source": "paper_engine",
        "balance": round(eng.balance, 4),
        "margin_balance": round(eng.balance, 4),
        "equity": round(eng.balance + total_unrealized, 4),
        "available_balance": round(eng.balance, 4),
        "available_margin": round(eng.balance - total_margin, 4),
        "unrealized_pnl": round(total_unrealized, 4),
        "used_margin": round(total_margin, 4),
        "order_margin": 0,
        "margin_mode": "Isolated",
        "frozen_funds": 0,
        "risk_ratio": round(total_margin / max(eng.balance, 0.01) * 100, 2) if total_margin > 0 else 0,
        "max_withdraw": round(eng.balance, 4),
        "position_count": len(open_positions),
        "currency": "USDT",
    }


# ── Position TP/SL Management ────────────────────────────────────────────

@router.post("/position/tp-sl")
def set_position_tp_sl(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set or update TP/SL on an open futures position.
    In live mode, places TP/SL stop orders via Lead Trading API."""
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    import time as _t

    pair     = req.get("pair", "BTC/USDT")
    tp_price = req.get("tp_price")
    sl_price = req.get("sl_price")

    eng = futures_engine_registry.for_user(user_id)
    updated = False
    matched_pos = None
    with eng._lock:
        for key, pos in eng.positions.items():
            if pos.pair == pair:
                if tp_price is not None:
                    pos.tp = float(tp_price)
                if sl_price is not None:
                    pos.sl = float(sl_price)
                updated = True
                matched_pos = pos
                break

    if not updated:
        return {"error": f"No open position for {pair}"}

    mode = eng._mode or "paper"

    # ── Live mode: place TP/SL orders on KuCoin Lead Trading ─────────────
    if mode == "live" and eng._api_key and matched_pos:
        kc_symbol     = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        direction     = matched_pos.direction
        position_side = "LONG" if direction == "long" else "SHORT"
        close_side    = "sell" if direction == "long" else "buy"
        lev           = getattr(matched_pos, "leverage", eng._leverage or 10)
        contract_size = matched_pos.size * lev
        contracts     = max(1, int(contract_size / matched_pos.entry * 1000))

        # TP order
        if tp_price is not None:
            try:
                tp_body = {
                    "clientOid":   f"atf-tp-{int(_t.time()*1000)}",
                    "symbol":       kc_symbol,
                    "marginMode":   "ISOLATED",
                    "leverage":     lev,
                    "positionSide": position_side,
                    "side":         close_side,
                    "type":         "market",
                    "size":         contracts,
                    "stopPriceType": "TP",
                    "reduceOnly":   True,
                }
                if direction == "long":
                    tp_body["triggerStopUpPrice"] = str(float(tp_price))
                else:
                    tp_body["triggerStopDownPrice"] = str(float(tp_price))
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/st-orders", tp_body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                log.info("[%s] Lead Trading TP order: %s", user_id, resp)
            except Exception as e:
                log.error("[%s] Lead Trading TP order failed: %s", user_id, e)

        # SL order
        if sl_price is not None:
            try:
                sl_body = {
                    "clientOid":   f"atf-sl-{int(_t.time()*1000)}",
                    "symbol":       kc_symbol,
                    "marginMode":   "ISOLATED",
                    "leverage":     lev,
                    "positionSide": position_side,
                    "side":         close_side,
                    "type":         "market",
                    "size":         contracts,
                    "stopPriceType": "TP",
                    "reduceOnly":   True,
                }
                if direction == "long":
                    sl_body["triggerStopDownPrice"] = str(float(sl_price))
                else:
                    sl_body["triggerStopUpPrice"] = str(float(sl_price))
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/st-orders", sl_body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                log.info("[%s] Lead Trading SL order: %s", user_id, resp)
            except Exception as e:
                log.error("[%s] Lead Trading SL order failed: %s", user_id, e)

    # Update DB
    trade = db.execute(
        select(Trade).where(
            Trade.user_id == user_id, Trade.pair == pair,
            Trade.market_type == "futures", Trade.status == "open",
        ).order_by(desc(Trade.entry_time)).limit(1)
    ).scalar_one_or_none()
    if trade and sl_price is not None:
        trade.stoploss_price = float(sl_price)
        db.commit()

    log_event(db, user_id, "futures.set_tp_sl", request, payload={"pair": pair, "tp": tp_price, "sl": sl_price})
    return {"updated": True, "pair": pair, "tp_price": tp_price, "sl_price": sl_price}


# ── Bot Management ───────────────────────────────────────────────────────

@router.get("/lead-trading-status")
def lead_trading_status(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Check if the user has a KuCoin Futures Lead Trading API key configured."""
    from backend.utils.encryption import decrypt, DecryptError

    cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
    if not cfg:
        return {"connected": False, "reason": "No config found"}
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return {"connected": False, "reason": "Could not decrypt credentials"}
    if not kk or not ks:
        return {"connected": False, "reason": "No API key configured"}

    # Try to hit KuCoin Futures account overview to verify credentials + permissions
    try:
        from backend.services.native_trading_engine import _kucoin_get_signed
        from backend.services.futures_engine import KUCOIN_FUTURES_BASE
        data = _kucoin_get_signed(
            "/api/v1/account-overview", kk, ks, kp,
            params={"currency": "USDT"},
            base_url=KUCOIN_FUTURES_BASE,
        )
        if str(data.get("code")) == "200000":
            acct = data.get("data", {})
            return {
                "connected": True,
                "account_type": "futures_lead_trading",
                "balance": float(acct.get("availableBalance", 0)),
                "equity": float(acct.get("accountEquity", 0)),
                "unrealized_pnl": float(acct.get("unrealisedPNL", 0)),
            }
        code = str(data.get("code", ""))
        return {"connected": False, "reason": f"KuCoin error {code}: {data.get('msg', 'unknown')}"}
    except Exception as e:
        return {"connected": False, "reason": str(e)}


# Built-in signal criteria extracted from strategy signal function docstrings
_BUILTIN_SIGNAL_CRITERIA: dict[str, list[dict]] = {
    "SimpleTargetStrategy": [
        {"name": "LONG", "conditions": ["RSI < 55 AND price near/below EMA20", "OR RSI < 38 (strong oversold)"]},
        {"name": "SHORT", "conditions": ["RSI > 65 AND price above EMA20", "OR RSI > 72 (strong overbought)"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%", "R:R = 2:1"]},
    ],
    "BidirectionalStrategy": [
        {"name": "LONG", "conditions": ["EMA9 > EMA21 (uptrend, 2+ bars)", "RSI < 60"]},
        {"name": "SHORT", "conditions": ["EMA9 < EMA21 (downtrend, 2+ bars)", "RSI > 40"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%", "R:R = 2:1"]},
    ],
    "SMCStrategy": [
        {"name": "HTF Bias", "conditions": ["EMA50 direction — bullish if close > EMA50"]},
        {"name": "BOS", "conditions": ["Price breaks 20-bar swing high (LONG) or low (SHORT)"]},
        {"name": "FVG", "conditions": ["3-candle Fair Value Gap within last 30 bars"]},
        {"name": "OB Zone", "conditions": ["Last opposing candle before the move (Order Block)"]},
        {"name": "LONG", "conditions": ["Bullish BOS + Bullish FVG + price in OB zone + close > EMA50"]},
        {"name": "SHORT", "conditions": ["Bearish BOS + Bearish FVG + price in OB zone + close < EMA50"]},
        {"name": "Risk", "conditions": ["SL: below swing low (LONG) / above swing high (SHORT)", "TP: 2R from entry"]},
    ],
    "SMCStrategyTV": [
        {"name": "HTF Bias", "conditions": ["EMA50 direction — bullish if close > EMA50"]},
        {"name": "BOS", "conditions": ["Price breaks 20-bar swing high (LONG) or low (SHORT)"]},
        {"name": "FVG", "conditions": ["3-candle Fair Value Gap within last 30 bars"]},
        {"name": "OB Zone", "conditions": ["Last opposing candle before the move (Order Block)"]},
        {"name": "LONG", "conditions": ["Bullish BOS + Bullish FVG + price in OB zone + close > EMA50"]},
        {"name": "SHORT", "conditions": ["Bearish BOS + Bearish FVG + price in OB zone + close < EMA50"]},
        {"name": "Risk", "conditions": ["SL: below swing low (LONG) / above swing high (SHORT)", "TP: 2R from entry"]},
    ],
    "MissCandleLongStrategy": [
        {"name": "LONG", "conditions": ["Close crosses above upper Bollinger Band", "RSI momentum confirmation"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "MissCandleShortStrategy": [
        {"name": "SHORT", "conditions": ["Close crosses below lower Bollinger Band", "RSI momentum confirmation"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "MacdCrossoverStrategy": [
        {"name": "LONG", "conditions": ["MACD line crosses above Signal line"]},
        {"name": "SHORT", "conditions": ["MACD line crosses below Signal line"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 3.0%"]},
    ],
    "RsiBollingerStrategy": [
        {"name": "LONG", "conditions": ["RSI < 30 (oversold)", "Price touches lower Bollinger Band"]},
        {"name": "SHORT", "conditions": ["RSI > 70 (overbought)", "Price touches upper Bollinger Band"]},
        {"name": "Risk", "conditions": ["SL: 1.5%", "TP: 1.5%"]},
    ],
    "EmaScalpingStrategy": [
        {"name": "LONG", "conditions": ["EMA9 crosses above EMA21", "Volume confirmation"]},
        {"name": "SHORT", "conditions": ["EMA9 crosses below EMA21", "Volume confirmation"]},
        {"name": "Risk", "conditions": ["SL: 0.5%", "TP: 1.0%"]},
    ],
}


def _extract_signal_criteria(strategy_name: str, strategy_id: int | None, db, user_id: str) -> list[dict]:
    """Extract signal firing criteria from strategy. Uses built-in map or strategy description."""
    # Check built-in strategies first
    for key, criteria in _BUILTIN_SIGNAL_CRITERIA.items():
        if key.lower() in (strategy_name or "").lower() or (strategy_name or "").lower() in key.lower():
            return criteria

    # For user strategies, extract from description
    if strategy_id:
        from backend.models.strategy import Strategy
        from sqlalchemy import or_
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),
            )
        ).scalar_one_or_none()
        if strat and strat.description:
            return [{"name": "Strategy", "conditions": [strat.description[:200]]}]

    return []


@router.get("/bots")
def list_futures_bots(
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """List all futures bot instances for this user."""
    instances = db.execute(
        select(StrategyInstance)
        .where(StrategyInstance.user_id == user_id, StrategyInstance.market_type == "futures")
        .order_by(desc(StrategyInstance.created_at))
    ).scalars().all()

    # Check actual engine status for running bots
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}

    # Auto-resume: restart engines for bots marked running in DB but with no live thread
    _kk = _ks = _kp = ""
    _creds_loaded = False
    for i in instances:
        if not i.is_running or not i.engine_key:
            continue
        eng = bot_engines.get(i.engine_key)
        if eng and eng.is_running:
            continue
        # Engine is dead — resume it
        if not _creds_loaded:
            from backend.utils.encryption import decrypt, DecryptError
            cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
            if cfg:
                try:
                    _kk = decrypt(cfg.kucoin_key_enc or "", user_id)
                    _ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
                    _kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
                except Exception:
                    pass
            _creds_loaded = True
        pairs = [p.strip() for p in (i.pairs or "BTC/USDT").split(",")]
        eng = futures_engine_registry.for_bot(user_id, i.engine_key)
        try:
            eng.start_futures(
                strategy_name=i.strategy_name, pairs=pairs, leverage=i.leverage or 10,
                mode=i.mode or "paper", timeframe=i.timeframe or "15m",
                stoploss=i.stoploss or -0.03, wallet=i.wallet or 1000,
                take_profit_pct=(i.takeprofit or 0.015) * 100,
                max_position_pct=(i.risk_pct or 5.0),
                strategy_id=i.strategy_id,
                kucoin_key=_kk, kucoin_secret=_ks, kucoin_passphrase=_kp,
            )
            log.info("Auto-resumed bot %s for user %s", i.engine_key, user_id)
        except Exception as exc:
            log.warning("Failed to auto-resume bot %s: %s", i.engine_key, exc)
    # Refresh engine list after potential resumes
    bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}

    # Count trades from DB per strategy for fallback
    from sqlalchemy import func
    db_trade_counts = {}
    for i in instances:
        count = db.execute(
            select(func.count(Trade.id)).where(
                Trade.user_id == user_id,
                Trade.market_type == "futures",
                Trade.strategy_id == i.strategy_id,
            )
        ).scalar() or 0
        db_trade_counts[i.id] = count

    bots = []
    for i in instances:
        eng = bot_engines.get(i.engine_key) if i.engine_key else None
        engine_running = eng.is_running if eng else False
        engine_status = eng.status if eng else None
        db_count = db_trade_counts.get(i.id, 0)
        eng_total = (engine_status or {}).get("total_trades", 0) + (engine_status or {}).get("open_trades", 0)
        winding = (engine_status or {}).get("winding_down", False)
        bots.append({
            "id": i.id,
            "strategy_name": i.strategy_name,
            "strategy_id": i.strategy_id,
            "mode": i.mode,
            "pairs": i.pairs,
            "leverage": i.leverage,
            "timeframe": i.timeframe,
            "wallet": i.wallet,
            "is_running": (i.is_running and engine_running) or winding,
            "winding_down": winding,
            "engine_running": engine_running,
            "total_trades": eng_total or db_count or i.total_trades or 0,
            "closed_trades": (engine_status or {}).get("total_trades", i.total_trades or 0),
            "total_pnl": (engine_status or {}).get("realized_pnl", i.total_pnl or 0),
            "open_positions": (engine_status or {}).get("open_trades", 0),
            "ticks": (engine_status or {}).get("ticks", 0),
            "signals": (engine_status or {}).get("signal_count", 0),
            "last_action": (engine_status or {}).get("last_action", ""),
            "risk_pct": i.risk_pct,
            "stoploss": i.stoploss,
            "takeprofit": i.takeprofit,
            "engine_key": i.engine_key,
            "created_at": str(i.created_at),
        })
    return {"bots": bots}


@router.post("/bots")
def create_futures_bot(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Create and start a new futures bot instance (supports multiple concurrent bots)."""
    strategy_id   = req.get("strategy_id")
    strategy_name = req.get("strategy_name", "SimpleTargetStrategy")
    mode          = req.get("mode", "paper")
    pairs         = req.get("pairs", ["BTC/USDT"])
    leverage      = int(req.get("leverage", 10))
    timeframe     = req.get("timeframe", "15m")
    wallet        = float(req.get("wallet", 1000))
    stoploss      = float(req.get("stoploss", -0.03))
    takeprofit    = float(req.get("takeprofit", 0.015))
    drawdown_tolerance = float(req.get("drawdown_tolerance", 50))
    max_position_pct   = float(req.get("max_position_pct", 5.0))

    if strategy_id:
        from backend.models.strategy import Strategy
        from sqlalchemy import or_
        strat = db.execute(
            select(Strategy).where(
                Strategy.id == strategy_id,
                or_(Strategy.user_id == user_id, Strategy.is_template == True),
            )
        ).scalar_one_or_none()
        if strat:
            strategy_name = strat.name

    engine_key = f"bot-{strategy_name}-{int(_time.time())}"
    instance = StrategyInstance(
        user_id=user_id, strategy_id=strategy_id, strategy_name=strategy_name,
        market_type="futures", mode=mode, pairs=",".join(pairs),
        leverage=leverage, timeframe=timeframe, wallet=wallet,
        stoploss=stoploss, takeprofit=takeprofit, risk_pct=max_position_pct,
        is_running=True, engine_key=engine_key,
    )
    db.add(instance)
    db.commit()
    db.refresh(instance)

    # Resolve KuCoin credentials for live mode
    kk = ks = kp = ""
    if mode == "live":
        from backend.utils.encryption import decrypt, DecryptError
        cfg = db.execute(select(Config).where(Config.user_id == user_id).limit(1)).scalar_one_or_none()
        if cfg:
            try:
                kk = decrypt(cfg.kucoin_key_enc or "", user_id)
                ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
                kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
            except Exception:
                pass

    # Start an ISOLATED engine for this bot (supports multiple concurrent bots)
    eng = futures_engine_registry.for_bot(user_id, engine_key)
    result = eng.start_futures(
        strategy_name=strategy_name, pairs=pairs, leverage=leverage,
        mode=mode, timeframe=timeframe, stoploss=stoploss,
        wallet=wallet, take_profit_pct=takeprofit * 100,
        max_position_pct=max_position_pct,
        strategy_id=strategy_id,
        kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
    )

    log_event(db, user_id, "futures.create_bot", request, payload={
        "instance_id": instance.id, "strategy": strategy_name, "leverage": leverage,
        "mode": mode, "max_position_pct": max_position_pct,
    })
    return {"bot_id": instance.id, "engine_key": engine_key, **result}


@router.delete("/bots/{bot_id}")
def stop_futures_bot(
    bot_id: int,
    request: Request,
    force: bool = False,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Stop a futures bot. If open positions exist, enters wind-down mode
    (manages TP/SL to exit profitably, no new entries). Use ?force=true to kill immediately."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}

    eng = None
    if instance.engine_key:
        bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
        eng = bot_engines.get(instance.engine_key)

    has_open = eng and eng.is_running and len(eng.positions) > 0

    if has_open and not force:
        eng.wind_down()
        log_event(db, user_id, "futures.wind_down_bot", request, payload={
            "bot_id": bot_id, "open_positions": len(eng.positions),
        })
        return {
            "stopped": False,
            "winding_down": True,
            "open_positions": len(eng.positions),
            "message": f"Bot has {len(eng.positions)} open position(s) — entering wind-down mode. "
                       "Engine will manage TP/SL exits and stop automatically when all positions close.",
        }

    # Immediate stop (no open positions or force=true)
    instance.is_running = False
    db.commit()

    if instance.engine_key:
        futures_engine_registry.stop_bot(user_id, instance.engine_key)
    else:
        eng_default = futures_engine_registry.for_user(user_id)
        eng_default.stop()

    log_event(db, user_id, "futures.stop_bot", request, payload={"bot_id": bot_id})
    return {"stopped": True, "bot_id": bot_id}


@router.get("/bots/{bot_id}/performance")
def futures_bot_performance(
    bot_id: int,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get performance metrics for a specific bot."""
    instance = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.id == bot_id, StrategyInstance.user_id == user_id,
        )
    ).scalar_one_or_none()
    if not instance:
        return {"error": "Bot not found"}

    # Only fetch trades belonging to THIS bot's strategy, created after the bot was started
    trade_filter = [
        Trade.user_id == user_id, Trade.market_type == "futures",
        Trade.status == "closed",
    ]
    if instance.strategy_id:
        trade_filter.append(Trade.strategy_id == instance.strategy_id)
    if instance.created_at:
        trade_filter.append(Trade.entry_time >= instance.created_at)

    trades = db.execute(
        select(Trade).where(*trade_filter).order_by(desc(Trade.exit_time)).limit(100)
    ).scalars().all()

    total_pnl = sum(t.profit_abs or 0 for t in trades)
    wins = sum(1 for t in trades if (t.profit_abs or 0) > 0)
    win_rate = round(wins / len(trades) * 100, 1) if trades else 0

    engine_data = {}
    winding_down = False
    if instance.engine_key:
        bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
        eng = bot_engines.get(instance.engine_key)
        if eng:
            s = eng.status
            winding_down = s.get("winding_down", False)
            engine_data = {
                "action_log": s.get("action_log", []),
                "open_positions_detail": s.get("open_positions_detail", []),
                "closed_trades_detail": s.get("closed_trades_detail", []),
                "balance": s.get("balance", 0),
                "ticks": s.get("ticks", 0),
                "signal_count": s.get("signal_count", 0),
                "last_action": s.get("last_action", ""),
                "unrealized_pnl": s.get("unrealized_pnl", 0),
                "realized_pnl": s.get("realized_pnl", 0),
            }

    # Extract signal criteria from strategy description/docstring
    signal_criteria = _extract_signal_criteria(instance.strategy_name, instance.strategy_id, db, user_id)

    return {
        "bot_id": bot_id,
        "strategy_name": instance.strategy_name,
        "total_trades": len(trades),
        "total_pnl": round(total_pnl, 4),
        "win_rate": win_rate,
        "is_running": instance.is_running,
        "winding_down": winding_down,
        "mode": instance.mode,
        "pairs": instance.pairs,
        "leverage": instance.leverage,
        "risk_pct": instance.risk_pct,
        "signal_criteria": signal_criteria,
        "trades": [
            {
                "pair": t.pair, "direction": getattr(t, "side", "long"),
                "entry_price": t.entry_price, "exit_price": t.exit_price,
                "profit_abs": round(t.profit_abs or 0, 4),
                "profit_pct": round(t.profit_pct or 0, 2),
                "exit_reason": t.exit_reason,
                "entry_time": str(t.entry_time) if t.entry_time else None,
                "exit_time": str(t.exit_time) if t.exit_time else None,
                "leverage": t.leverage,
            }
            for t in trades[:20]
        ],
        **engine_data,
    }
