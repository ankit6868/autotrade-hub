"""
Futures trading endpoints — paper and live with leverage.
Completely isolated from spot trading (different market_type='futures').

Includes: order book, recent trades, manual order placement, leverage/margin control,
pending orders, positions, bot management, and account overview.
"""
from __future__ import annotations

import logging
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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/futures", tags=["futures"])

# Lead Futures Trading: max leverage allowed by KuCoin lead trading account
LEAD_MAX_LEVERAGE = 20

# Simple in-memory cache for order book / trades (avoid hammering KuCoin)
_cache: dict[str, tuple[float, any]] = {}
CACHE_TTL = 1.5  # seconds


# ── KuCoin Futures lot-size table ────────────────────────────────────────────
# KuCoin contracts have a fixed multiplier — the amount of underlying per
# contract. Smallest order is 1 contract, so 1 contract's *notional* sets the
# minimum order size at a given price/leverage.
# Values cross-checked against KuCoin API /api/v1/contracts/active. Add more
# here if users start trading new pairs; unknown symbols fall back to 0.001.
_LOT_SIZE_BY_SYMBOL: dict[str, float] = {
    "XBTUSDTM":  0.001,
    "ETHUSDTM":  0.01,
    "SOLUSDTM":  0.1,
    "XRPUSDTM":  10.0,
    "DOGEUSDTM": 1000.0,
    "ADAUSDTM":  10.0,
    "AVAXUSDTM": 0.1,
    "BNBUSDTM":  0.01,
    "LTCUSDTM":  0.1,
    "LINKUSDTM": 1.0,
    "MATICUSDTM": 10.0,
    "DOTUSDTM":  1.0,
    "TRXUSDTM":  100.0,
    "ATOMUSDTM": 1.0,
    "OPUSDTM":   1.0,
    "ARBUSDTM":  1.0,
}


def _futures_lot_size(kc_symbol: str) -> float:
    """Return the contract multiplier (underlying-per-lot) for a KuCoin futures
    symbol. Falls back to 0.001 if unknown — safe-ish default but the caller
    should still surface KuCoin's rejection if the guess is wrong."""
    return _LOT_SIZE_BY_SYMBOL.get(kc_symbol.upper(), 0.001)


def _compute_live_sizing(cost_usdt: float, leverage: int, price: float,
                          kc_symbol: str) -> tuple[int | None, float, float, str | None]:
    """
    Convert a user-typed `cost_usdt` (USDT they want to commit as margin) into
    an exact KuCoin contract count, plus the *real* margin and notional that
    will actually be locked.

    Returns: (contracts, real_margin_usdt, real_notional_usdt, error_message)

      contracts          — integer lot count to send to KuCoin (None on error).
      real_margin_usdt   — what KuCoin will actually lock from your wallet.
      real_notional_usdt — position value at entry (margin * leverage).
      error_message      — non-empty when the order would be rejected; the
                            caller should return this to the frontend BEFORE
                            mutating engine state so we don't leave phantoms.

    Why this exists: the old code did `max(1, int(notional / price * 1000))`
    which silently rounded a $1 BTC order at 1x up to 1 contract (0.001 BTC ≈
    $79). Users got 80× the exposure they asked for. Now we compute the
    minimum cost for 1 contract at the chosen leverage and reject below it.
    """
    if leverage <= 0:
        return None, 0.0, 0.0, "Leverage must be ≥ 1."
    if price <= 0:
        return None, 0.0, 0.0, "Could not fetch a valid price for this symbol."
    if cost_usdt <= 0:
        return None, 0.0, 0.0, "Enter a cost greater than 0."

    lot = _futures_lot_size(kc_symbol)
    # Margin required for 1 contract at this leverage:
    #   notional_per_lot = lot * price
    #   margin_per_lot   = notional_per_lot / leverage
    min_margin = (lot * price) / leverage
    if cost_usdt < min_margin:
        return None, 0.0, 0.0, (
            f"Minimum cost for {kc_symbol} at {leverage}x leverage is "
            f"{min_margin:.2f} USDT (1 contract = {lot} {kc_symbol[:-5]} ≈ "
            f"{lot * price:.2f} USDT notional). Increase the Cost or leverage."
        )

    # User can afford ≥1 contract — pick the largest lot count whose margin
    # fits within their cost. round() would over-fill; floor is the right
    # consumer-friendly choice (charges no more than asked).
    notional_target = cost_usdt * leverage
    contracts = int(notional_target / (lot * price))
    contracts = max(1, contracts)  # safety; min_margin guard above guarantees ≥1
    real_notional = contracts * lot * price
    real_margin = real_notional / leverage
    return contracts, round(real_margin, 4), round(real_notional, 4), None


def _sync_margin_mode_to_kucoin(eng, kc_symbol: str, desired_mode: str,
                                  user_id: str) -> tuple[bool, str | None]:
    """
    Push the user's chosen Cross/Isolated setting to KuCoin Futures BEFORE
    placing an order — so the order body's `marginMode` field matches the
    symbol's configured mode on the exchange.

    Without this, the toggle in the UI only updates engine local memory.
    KuCoin still has the previous mode, and the next order is rejected with
    "The order's margin mode does not match the selected one".

    Returns (ok, error_message).
      - ok=True even if KuCoin is already in `desired_mode` (idempotent).
      - ok=False with a user-facing message if KuCoin refuses (most common
        reason: an open position on the symbol is locking the mode).
    """
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE

    mode_upper = (desired_mode or "ISOLATED").upper()
    if mode_upper not in ("CROSS", "ISOLATED"):
        return False, f"Invalid margin mode: {desired_mode}"

    try:
        resp = _kucoin_post_signed(
            "/api/v2/position/changeMarginMode",
            {"symbol": kc_symbol, "marginMode": mode_upper},
            eng._api_key, eng._api_sec, eng._api_pass,
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("[%s] changeMarginMode network error for %s: %s",
                    user_id, kc_symbol, e)
        # Don't block the order on a network blip — let the actual order
        # attempt either succeed or surface the real error.
        return True, None

    code = str(resp.get("code", ""))
    if code == "200000":
        log.info("[%s] Synced %s margin mode to %s on KuCoin",
                 user_id, kc_symbol, mode_upper)
        return True, None

    msg = (resp.get("msg") or "").lower()
    # 330005 = already in this mode (older API); treat as success.
    # Some accounts return 200000 with msg "already set" — also fine.
    if "already" in msg or code in ("330005", "330006"):
        return True, None

    # KuCoin's typical block: "Please close all open positions first" or
    # "The margin mode cannot be modified". Surface it clearly.
    log.warning("[%s] changeMarginMode rejected: code=%s msg=%s",
                user_id, code, resp.get("msg"))
    return False, (
        f"Could not switch {kc_symbol} to {mode_upper} margin mode on KuCoin: "
        f"{resp.get('msg', 'rejected')}. Close any open positions on this "
        f"symbol first, or pick the margin mode that matches your existing "
        f"position."
    )


def _sync_leverage_to_kucoin(eng, kc_symbol: str, leverage: int,
                              user_id: str) -> None:
    """Push the user's chosen leverage to KuCoin (best-effort, idempotent).

    Failures are logged but never block the order — the leverage is also
    in the order body itself; this just keeps the per-symbol setting on
    KuCoin in sync so other tooling sees the same number.
    """
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    try:
        _kucoin_post_signed(
            "/api/v2/position/changeLeverage",
            {"symbol": kc_symbol, "leverage": str(leverage)},
            eng._api_key, eng._api_sec, eng._api_pass,
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("[%s] changeLeverage failed for %s lev=%s: %s",
                    user_id, kc_symbol, leverage, e)


def _fetch_kucoin_symbol_settings(eng, kc_symbol: str,
                                    user_id: str) -> tuple[int | None, str | None]:
    """
    Read KuCoin's current per-symbol leverage and margin mode.

    Why: KuCoin's Cross mode keeps one shared leverage per symbol that
    doesn't always match what we send in the order body. The app's UI
    needs to show that REAL value (e.g. "Cross 3.00x"), not the requested
    one, so the user isn't surprised when their $1 margin actually leverages
    a $79 position.

    Returns (leverage:int | None, margin_mode:"CROSS"/"ISOLATED" | None).
    Either may be None if KuCoin doesn't respond or has no record for the
    symbol — the caller falls back to engine state in that case.
    """
    from backend.services.native_trading_engine import _kucoin_get_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE

    try:
        resp = _kucoin_get_signed(
            "/api/v1/position",
            eng._api_key, eng._api_sec, eng._api_pass,
            params={"symbol": kc_symbol},
            base_url=KUCOIN_FUTURES_BASE,
        )
    except Exception as e:
        log.warning("[%s] fetch position settings failed for %s: %s",
                    user_id, kc_symbol, e)
        return None, None

    if str(resp.get("code")) != "200000":
        return None, None

    data = resp.get("data") or {}
    # `realLeverage` is the live effective leverage; `leverage` is the per-symbol
    # configured value. Prefer realLeverage when a position is open (matches
    # what KuCoin's UI shows), fall back to the configured leverage otherwise.
    real_lev = data.get("realLeverage")
    cfg_lev  = data.get("leverage")
    try:
        lev_val = float(real_lev) if real_lev not in (None, 0, "0") else float(cfg_lev or 0)
        lev_int = int(round(lev_val)) if lev_val else None
    except (TypeError, ValueError):
        lev_int = None

    # marginMode field in /api/v1/position payload is one of "ISOLATED"/"CROSS".
    # Older API returned crossMode boolean instead; handle both.
    mode_raw = (data.get("marginMode") or "").upper()
    if not mode_raw:
        cross_flag = data.get("crossMode")
        if cross_flag is True:
            mode_raw = "CROSS"
        elif cross_flag is False:
            mode_raw = "ISOLATED"
    return lev_int, (mode_raw or None)


def _ensure_live_credentials(eng, user_id: str, db: Session) -> tuple[bool, str | None]:
    """
    Make sure the futures engine has the user's KuCoin Lead Trading credentials
    loaded — even when no bot was explicitly started in live mode.

    Manual market / limit orders and force-closes call this before talking to
    the Lead Trading REST API. Without it, `eng._api_key` is "" (engine still
    in its default paper state) and the live REST call is silently skipped,
    leaving a phantom position in the UI with nothing on KuCoin.

    Returns: (ok, error_message).
        ok=True  → eng._api_key / _api_sec / _api_pass are populated.
        ok=False → keys missing or undecryptable; error_message is user-facing.
    """
    from backend.utils.encryption import decrypt, DecryptError

    if eng._api_key and eng._api_sec and eng._api_pass:
        return True, None  # already loaded (e.g. live bot is running)

    cfg = db.execute(
        select(Config).where(Config.user_id == user_id).limit(1)
    ).scalar_one_or_none()
    if not cfg or not (cfg.kucoin_key_enc and cfg.kucoin_secret_enc and cfg.kucoin_passphrase_enc):
        return False, ("KuCoin API key not configured. Go to Setup → add a Lead-Trading "
                       "futures API key (General + Trade permissions, no Withdraw).")
    try:
        kk = decrypt(cfg.kucoin_key_enc or "", user_id)
        ks = decrypt(cfg.kucoin_secret_enc or "", user_id)
        kp = decrypt(cfg.kucoin_passphrase_enc or "", user_id)
    except DecryptError:
        return False, "Could not decrypt KuCoin credentials. Re-enter them in Setup."

    if not (kk and ks and kp):
        return False, "KuCoin credentials are blank. Re-enter them in Setup."

    eng._api_key  = kk
    eng._api_sec  = ks
    eng._api_pass = kp
    log.info("[%s] Loaded KuCoin Lead Trading credentials into futures engine on demand.", user_id)
    return True, None


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
    leverage         = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
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
    leverage      = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
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
    # Build positions list filtered by mode.
    # Each position may have a _mode tag (manual trades), otherwise use engine mode.
    native_positions = []
    with eng._lock:
        for p in eng.positions.values():
            pos_mode = getattr(p, "_mode", eng._mode or "paper")
            if mode is not None and pos_mode != mode:
                continue
            liq = getattr(p, "liquidation_price", None)
            lev = getattr(p, "leverage", 1)
            native_positions.append({
                "pair":              p.pair,
                "direction":         p.direction,
                "entry":             round(p.entry, 6),
                "sl":                round(p.effective_sl, 6) if hasattr(p, "effective_sl") else round(p.sl, 6),
                "tp":                round(p.tp, 6),
                "stake":             round(p.size, 2),
                "opened_at":         str(p.opened_at),
                "leverage":          lev,
                "liquidation_price": round(liq, 6) if liq else None,
                "_pos_mode":         pos_mode,
                "exchange_order_id": getattr(p, "exchange_order_id", None),
            })

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
            "id":                f"futures-{p['pair']}-{p.get('_pos_mode','paper')}",
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
            # Use the position's own _mode tag, NOT the engine mode, so a live
            # manual entry placed while the engine is in default-paper still
            # reports mode="live" to the UI.
            "mode":              p.get("_pos_mode") or (eng._mode or "paper"),
            "exchange_order_id": p.get("exchange_order_id"),
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

    # ── Reconcile with KuCoin Lead Trading for live mode ─────────────────
    # Limit orders that fill immediately (e.g. buy-above-market) and any
    # positions opened on KuCoin we don't know about (filled limit orders,
    # external tools, account restored from snapshot) should appear here
    # so the user never has a hidden real-money position.
    if (mode == "live" or mode is None) and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            kc_resp = _kucoin_get_signed(
                "/api/v1/positions",
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(kc_resp.get("code")) == "200000":
                # Map KuCoin futures symbol → app pair: XBTUSDTM → BTC/USDT
                pairs_already_in_merged = {t["pair"] for t in merged}
                for kp in (kc_resp.get("data") or []):
                    qty = float(kp.get("currentQty", 0))
                    if qty == 0:
                        continue   # closed / zero positions
                    kc_sym = kp.get("symbol", "")
                    # Reverse normalize: XBTUSDTM → BTCUSDTM → BTC/USDT
                    base = kc_sym.replace("USDTM", "").replace("XBT", "BTC")
                    pair = f"{base}/USDT"
                    if pair in pairs_already_in_merged:
                        continue   # already tracking via engine/DB
                    direction = "long" if qty > 0 else "short"
                    entry     = float(kp.get("avgEntryPrice", 0))
                    cur       = float(kp.get("markPrice", 0)) or entry
                    margin    = float(kp.get("posMargin", 0) or kp.get("maintMargin", 0))
                    lev       = float(kp.get("realLeverage", 0)) or float(kp.get("leverage", 1)) or 1
                    liq       = float(kp.get("liquidationPrice", 0)) or None
                    unreal    = float(kp.get("unrealisedPnl", 0))
                    merged.append({
                        "id":                f"kucoin-{kc_sym}",
                        "pair":              pair,
                        "side":              direction,
                        "entry_price":       entry,
                        "current_price":     cur,
                        "amount":            round(margin, 4),
                        "leverage":          round(lev, 2),
                        "liquidation_price": liq,
                        "stoploss_price":    None,
                        "tp_price":          None,
                        "entry_time":        kp.get("openingTimestamp") or None,
                        "mode":              "live",
                        "exchange_order_id": None,
                        "market_type":       "futures",
                        "unrealized_pnl":    round(unreal, 4),
                        "_source":           "kucoin",
                    })
        except Exception as e:
            log.warning("[%s] KuCoin position reconcile failed: %s", user_id, e)

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
    # Frontend can pass cost_usdt directly (preferred for live mode — the
    # stake_pct path multiplies against engine.balance which is the paper
    # wallet default of 1000 USDT, NOT the user's real KuCoin balance, so
    # $5 becomes $48 and KuCoin rejects with "insufficient available margin").
    req_cost_usdt = req.get("cost_usdt")
    req_leverage  = req.get("leverage")
    req_mode      = req.get("mode")  # explicit mode from frontend

    eng = futures_engine_registry.for_user(user_id)

    raw_lev  = int(req_leverage) if req_leverage else (eng._leverage if (eng._leverage and eng._leverage > 1) else 10)
    leverage = min(LEAD_MAX_LEVERAGE, raw_lev)
    # Use explicit mode from request first, then engine mode, then default
    mode     = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")
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

    # User's *intended* margin in USDT.
    # Priority 1: explicit cost_usdt from the frontend (live market orders).
    # The stake_pct path is broken for live: it multiplies against
    # engine.balance, which is the paper-wallet default of 1000 USDT when
    # no live bot is running, NOT the user's real KuCoin balance. So $5
    # typed becomes $48 sent, KuCoin rejects "insufficient available margin".
    # Priority 2: stake_pct × engine.balance (paper mode + legacy callers).
    if req_cost_usdt is not None:
        try:
            user_cost = float(req_cost_usdt)
        except (TypeError, ValueError):
            user_cost = balance * (stake_pct / 100)
    else:
        user_cost = balance * (stake_pct / 100)
    # `stake` is what we eventually store on the position. For live mode it
    # gets overwritten with the REAL margin KuCoin will lock (after lot-size
    # rounding); for paper mode it stays equal to user_cost.
    stake = user_cost

    if direction == "long":
        sl_price = round(entry_price * (1 - sl_pct), 6)
        tp_price = round(entry_price * (1 + tp_pct / 100), 6)
    else:
        sl_price = round(entry_price * (1 + sl_pct), 6)
        tp_price = round(entry_price * (1 - tp_pct / 100), 6)

    with eng._lock:
        existing_pairs = [p.pair for p in eng.positions.values()
                          if getattr(p, "_mode", eng._mode) == mode]
    if pair in existing_pairs:
        return {"error": f"Already have an open position for {pair}. Close it first."}

    # ── Live mode: place real order via Lead Trading API ──────────────
    # CRITICAL: must talk to KuCoin BEFORE we mutate engine state. If the API
    # rejects (no creds, balance too low, bad symbol, etc.) we return early
    # without leaving a phantom position in the engine.
    exchange_order_id = None
    real_notional = real_margin = None
    if mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": err}
        kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

        # Lot-size-aware sizing. Bails out with a user-facing error BEFORE we
        # touch the exchange when the requested cost is below the symbol's
        # minimum order at this leverage.
        contracts, real_margin, real_notional, sz_err = _compute_live_sizing(
            cost_usdt=user_cost, leverage=leverage,
            price=entry_price, kc_symbol=kc_symbol,
        )
        if sz_err:
            return {"error": sz_err}

        try:
            side          = "buy" if direction == "long" else "sell"
            position_side = "LONG" if direction == "long" else "SHORT"
            client_oid    = f"atf-manual-{int(_time.time()*1000)}"
            margin_mode   = eng.get_symbol_margin(kc_symbol).upper() or "ISOLATED"

            # Sync the user's chosen margin mode + leverage to KuCoin before
            # placing the order. Without this, KuCoin remembers whatever mode
            # the symbol was last in and rejects the order with
            # "The order's margin mode does not match the selected one".
            sync_ok, sync_err = _sync_margin_mode_to_kucoin(
                eng, kc_symbol, margin_mode, user_id
            )
            if not sync_ok:
                return {"error": sync_err}
            _sync_leverage_to_kucoin(eng, kc_symbol, leverage, user_id)

            # Read back KuCoin's REAL leverage/margin-mode (Cross often keeps
            # its own per-symbol leverage that overrides our request). Use
            # those values for the order body, response, and DB row so the
            # app and KuCoin always agree on what leverage is in effect.
            kc_lev, kc_mode = _fetch_kucoin_symbol_settings(eng, kc_symbol, user_id)
            if kc_lev:
                leverage = kc_lev
            if kc_mode in ("CROSS", "ISOLATED"):
                margin_mode = kc_mode

            body = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       kc_symbol,
                "type":         "market",
                "size":         contracts,
                "leverage":     leverage,
                "marginMode":   margin_mode,
                "positionSide": position_side,
            }
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(resp.get("code")) != "200000":
                msg = resp.get("msg") or resp
                log.warning("[%s] Lead Trading manual entry rejected: %s", user_id, resp)
                return {"error": f"KuCoin Lead Trading rejected the order: {msg}"}
            exchange_order_id = resp.get("data", {}).get("orderId")
            log.info("[%s] Lead Trading manual ENTRY ok: order_id=%s cost=%.2f "
                     "real_margin=%.2f notional=%.2f body=%s",
                     user_id, exchange_order_id, user_cost, real_margin,
                     real_notional, body)
        except Exception as e:
            log.exception("[%s] Lead Trading manual entry failed", user_id)
            return {"error": f"Lead Trading order failed: {e}"}
        # Use the REAL margin (what KuCoin actually locked) as the position
        # size — this is what shows in Positions table, History P&L, etc.
        # If we kept user_cost the app would show $1 while KuCoin shows $79.
        stake = real_margin

    now = datetime.now(_tz.utc)
    pos = FuturesPosition(
        pair=pair, direction=direction,
        entry=entry_price, sl=sl_price, tp=tp_price,
        size=stake, leverage=leverage, opened_at=now,
    )
    pos._mode = mode  # tag position with its mode for filtering
    if exchange_order_id:
        # Stash the exchange order id on the position so /force-close can
        # reconcile with KuCoin even if the engine restarts.
        pos.exchange_order_id = exchange_order_id

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
        "margin": stake, "notional": real_notional,
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
        "margin": round(stake, 4),                   # what KuCoin actually locked
        "notional": real_notional,                    # position value at entry
    }


# ── Force Close ───────────────────────────────────────────────────────────────

@router.post("/force-close/{pair:path}")
async def futures_force_close(
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

    # Parse optional JSON body for mode
    req_mode = None
    try:
        body = await request.json()
        req_mode = body.get("mode") if isinstance(body, dict) else None
    except Exception:
        pass

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
    # Use explicit mode from request first, then engine mode, then default
    mode = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")

    # ── Step 1: identify in-memory positions WITHOUT popping yet ────────
    # Close is transactional for live mode: we only mutate engine/DB state
    # AFTER KuCoin confirms. If KuCoin rejects (mode mismatch, dead lot
    # size, etc.) the in-memory position stays and the user gets a real
    # error to retry with, instead of "everything looks gone here but
    # KuCoin still has it" — which was the previous bug.
    with eng._lock:
        matching = [
            (k, p) for k, p in eng.positions.items()
            if p.pair == pair and getattr(p, "_mode", eng._mode or "paper") == mode
        ]

    # ── Step 2: place close orders on KuCoin Lead Trading ────────────────
    kucoin_errors: list[str] = []
    confirmed_keys: list[str] = []   # keys whose KuCoin close confirmed → safe to pop

    if mode == "live" and matching:
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {
                "error": f"Could not load KuCoin credentials: {err}. "
                         "Position is still open — re-enter your API key in Setup and retry.",
                "kucoin_cancelled": False,
            }

        kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

        # Read the LIVE position's actual margin mode + leverage from KuCoin
        # so the close body matches whatever KuCoin currently has. The
        # engine's local cache can drift from KuCoin (e.g. user opened the
        # position under Isolated but eng.get_symbol_margin still reports
        # the default 'cross') and KuCoin then rejects with margin-mode
        # mismatch.
        kc_margin_mode = "ISOLATED"
        kc_leverage_real: int | None = None
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            pos_resp = _kucoin_get_signed(
                "/api/v1/position",
                eng._api_key, eng._api_sec, eng._api_pass,
                params={"symbol": kc_symbol},
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(pos_resp.get("code")) == "200000":
                pdata = pos_resp.get("data") or {}
                kc_margin_mode = (pdata.get("marginMode") or kc_margin_mode).upper()
                try:
                    rl = float(pdata.get("realLeverage") or pdata.get("leverage") or 0)
                    if rl > 0:
                        kc_leverage_real = max(1, int(round(rl)))
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            log.warning("[%s] Could not pre-fetch KuCoin position for close: %s", user_id, e)

        for trade_key, pos in matching:
            try:
                side          = "sell" if pos.direction == "long" else "buy"
                position_side = "LONG" if pos.direction == "long" else "SHORT"
                pos_lev       = kc_leverage_real or int(getattr(pos, "leverage", eng._leverage or 10))
                contract_size = pos.size * pos_lev
                contracts     = max(1, int(contract_size / pos.entry * 1000))
                body = {
                    "clientOid":   f"atf-close-{int(_time.time()*1000)}",
                    "side":         side,
                    "symbol":       kc_symbol,
                    "type":         "market",
                    "size":         contracts,
                    "leverage":     min(LEAD_MAX_LEVERAGE, pos_lev),
                    "marginMode":   kc_margin_mode,
                    "positionSide": position_side,
                    "reduceOnly":   True,
                }
                resp = _kucoin_post_signed(
                    "/api/v1/copy-trade/futures/orders", body,
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                code = str(resp.get("code", ""))
                if code == "200000":
                    confirmed_keys.append(trade_key)
                    log.info("[%s] Lead Trading CLOSE ok for %s: %s",
                             user_id, pair, resp.get("data"))
                else:
                    msg = resp.get("msg") or f"KuCoin code {code}"
                    kucoin_errors.append(msg)
                    log.warning("[%s] Lead Trading CLOSE rejected for %s: %s",
                                user_id, pair, resp)
            except Exception as e:
                kucoin_errors.append(str(e))
                log.error("[%s] Lead Trading close failed for %s: %s", user_id, pair, e)

        # If ANY close was rejected, surface the error and don't touch
        # local state — the user retries with the real reason in hand.
        if kucoin_errors:
            return {
                "error": (
                    "KuCoin Lead Trading rejected the close: "
                    + "; ".join(kucoin_errors)
                    + ". Position still open on KuCoin — please retry, or "
                      "close it directly on the KuCoin tab."
                ),
                "kucoin_cancelled": False,
            }

    # ── Step 3: paper mode OR live confirmed → pop + persist ────────────
    keys_to_finalize = (
        [k for k, _ in matching] if mode == "paper"
        else confirmed_keys
    )
    with eng._lock:
        for trade_key in keys_to_finalize:
            pos = eng.positions.pop(trade_key, None)
            if pos is None:
                continue
            ep = exit_price or pos.entry
            pos.close(ep, "force_closed", now)
            eng.balance += pos.pnl_abs
            eng.closed_trades.append(pos)
            closed_positions.append(pos)

    # Persist each closed in-memory position
    total_pnl = 0.0
    for pos in closed_positions:
        _persist_closed_trade(user_id, pos, mode, eng._strategy_id, pos.db_id)
        total_pnl += pos.pnl_abs

    # ── Also close any orphaned open DB positions for this pair + mode ──
    orphan_query = select(Trade).where(
        Trade.user_id    == user_id,
        Trade.pair       == pair,
        Trade.market_type == "futures",
        Trade.status     == "open",
    )
    if mode:
        orphan_query = orphan_query.where(Trade.mode == mode)
    orphan_trades = db.execute(orphan_query).scalars().all()

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

    # ── Live mode: also close any KuCoin-only positions for this pair ───
    # These appear when a limit order filled immediately on KuCoin and we
    # never created an engine/DB Position to track it. Without this step
    # /force-close would say "No open position" even though one exists on
    # KuCoin holding real margin.
    kucoin_only_closed = 0
    if mode == "live" and not closed_positions and not orphan_trades:
        ok, _ = _ensure_live_credentials(eng, user_id, db)
        if ok:
            try:
                kc_symbol = normalize_futures_symbol(
                    pair.replace("/", "").replace("USDT", "USDTM")
                )
                # Get the live position for this symbol.
                # NOTE: We use /api/v1/positions (LIST) and filter, not
                # /api/v1/position?symbol=X (SINGLE). The single-position
                # endpoint returns qty=0 for Lead Trading positions even
                # when they're real and visible in the LIST endpoint
                # (which is what powers the reconcile that surfaces them
                # in the UI). Using the LIST guarantees we see the same
                # positions the user sees.
                from backend.services.native_trading_engine import _kucoin_get_signed
                pos_resp = _kucoin_get_signed(
                    "/api/v1/positions",
                    eng._api_key, eng._api_sec, eng._api_pass,
                    base_url=KUCOIN_FUTURES_BASE,
                )
                pdata: dict = {}
                qty = 0
                if str(pos_resp.get("code")) == "200000":
                    for _p in (pos_resp.get("data") or []):
                        if (_p.get("symbol") or "").upper() == kc_symbol.upper():
                            _q = int(_p.get("currentQty", 0) or 0)
                            if _q != 0:
                                pdata = _p
                                qty = _q
                                break
                if qty != 0:
                    direction = "long" if qty > 0 else "short"
                    side          = "sell" if direction == "long" else "buy"
                    position_side = "LONG" if direction == "long" else "SHORT"
                    contracts     = abs(qty)
                    lev_use       = int(pdata.get("realLeverage") or pdata.get("leverage") or 1)
                    body = {
                        "clientOid":   f"atf-kucoin-close-{int(_time.time()*1000)}",
                        "side":         side,
                        "symbol":       kc_symbol,
                        "type":         "market",
                        "size":         contracts,
                        "leverage":     min(LEAD_MAX_LEVERAGE, lev_use),
                        "marginMode":   (pdata.get("marginMode") or "ISOLATED").upper(),
                        "positionSide": position_side,
                        "reduceOnly":   True,
                    }
                    resp = _kucoin_post_signed(
                        "/api/v1/copy-trade/futures/orders", body,
                        eng._api_key, eng._api_sec, eng._api_pass,
                        base_url=KUCOIN_FUTURES_BASE,
                    )
                    if str(resp.get("code")) == "200000":
                        kucoin_only_closed = 1
                        log.info("[%s] Closed KuCoin-only position for %s qty=%s",
                                 user_id, pair, qty)
                    else:
                        log.warning("[%s] Failed to close KuCoin-only position for %s: %s",
                                    user_id, pair, resp)
                        return {"error": f"KuCoin rejected close: {resp.get('msg') or resp}"}
                else:
                    log.info("[%s] /api/v1/positions returned no open qty for %s (code=%s)",
                             user_id, kc_symbol, pos_resp.get("code"))
            except Exception as e:
                log.error("[%s] KuCoin-only close attempt failed: %s", user_id, e)

    total_closed = len(closed_positions) + len(orphan_trades) + kucoin_only_closed
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
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    import time as _t

    # Frontend sends 'BTCUSDTM' but KuCoin Lead Trading expects 'XBTUSDTM'
    # (Bitcoin uses XBT, the original ISO 4217-style code, on KuCoin futures).
    # normalize_futures_symbol() handles the BTC→XBT swap and is a no-op for
    # symbols that are already correct.
    symbol     = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    side       = req.get("side", "buy")
    order_type = req.get("order_type", "limit")
    size       = float(req.get("size", 0))
    cost_usdt  = float(req.get("cost_usdt", 0))   # USDT cost sent by frontend
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
    req_mode   = req.get("mode")  # explicit mode from frontend

    if size <= 0 and cost_usdt <= 0:
        return {"error": "size or cost_usdt must be positive"}

    eng = futures_engine_registry.for_user(user_id)

    if price is not None:
        price = float(price)
    if stop_price is not None:
        stop_price = float(stop_price)
    if leverage is not None:
        leverage = int(leverage)

    lev = min(LEAD_MAX_LEVERAGE, leverage or eng._leverage or 10)
    # Use explicit mode from request first, then engine mode, then default
    mode = req_mode if req_mode in ("paper", "live") else (eng._mode or "paper")

    # ── Recalculate size from cost_usdt when provided ────────────────────
    # The frontend sends cost_usdt (the USDT margin the user typed). For
    # live KuCoin we need an integer lot count and the *real* margin that
    # KuCoin will lock (1 lot is the minimum, so $1 BTC at 1x rounds up to
    # ~$79 — we must catch this and refuse with a clear error).
    real_margin = None
    real_notional = None
    if cost_usdt > 0:
        ref_price = price or stop_price
        if ref_price is None:
            try:
                from backend.services.native_trading_engine import _kucoin_get
                sym_p = symbol.replace("USDTM", "-USDT").replace("XBTUSDTM", "BTC-USDT")
                _pdata = _kucoin_get("/api/v1/market/orderbook/level1", {"symbol": sym_p})
                if str(_pdata.get("code")) == "200000":
                    ref_price = float(_pdata["data"]["price"])
            except Exception:
                pass

        if mode == "live":
            contracts, real_margin, real_notional, sz_err = _compute_live_sizing(
                cost_usdt=cost_usdt, leverage=lev,
                price=float(ref_price or 0), kc_symbol=symbol,
            )
            if sz_err:
                return {"error": sz_err}
            size = contracts
        else:
            # Paper mode: store USDT margin as the size unit (consistent with
            # manual-entry — Positions row shows the margin column).
            size = cost_usdt
            real_margin = cost_usdt
            real_notional = cost_usdt * lev

    # Determine position side
    if not position_side:
        if reduce_only:
            position_side = "SHORT" if side == "buy" else "LONG"
        else:
            position_side = "LONG" if side == "buy" else "SHORT"

    # ── Live mode: send to Lead Trading API ──────────────────────────────
    exchange_order_id = None
    if mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": err}
        try:
            client_oid = f"atf-ord-{int(_t.time()*1000)}"
            margin_mode = eng.get_symbol_margin(symbol).upper() or "ISOLATED"

            # Sync margin mode + leverage to KuCoin first — see comment in
            # /manual-entry for full rationale.
            sync_ok, sync_err = _sync_margin_mode_to_kucoin(
                eng, symbol, margin_mode, user_id
            )
            if not sync_ok:
                return {"error": sync_err}
            _sync_leverage_to_kucoin(eng, symbol, lev, user_id)

            # Read back KuCoin's REAL leverage/margin-mode so the DB row +
            # success response reflect what KuCoin will actually apply.
            kc_lev, kc_mode = _fetch_kucoin_symbol_settings(eng, symbol, user_id)
            if kc_lev:
                lev = kc_lev
            if kc_mode in ("CROSS", "ISOLATED"):
                margin_mode = kc_mode

            body: dict = {
                "clientOid":   client_oid,
                "side":         side,
                "symbol":       symbol,
                "type":         order_type if order_type in ("market", "limit") else "limit",
                "size":         int(size),
                "leverage":     lev,
                "marginMode":   margin_mode,
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
        cost_usdt=cost_usdt,
    )

    # Persist to DB — include `mode` so paper limit orders don't leak into
    # the Live tab's Open Orders panel and vice versa.
    order_rec = FuturesOrder(
        user_id=user_id, mode=mode, symbol=symbol, side=side, order_type=order_type,
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
    if real_margin is not None:
        result["margin"] = real_margin
    if real_notional is not None:
        result["notional"] = real_notional

    log_event(db, user_id, "futures.place_order", request, payload=result)
    return result


@router.delete("/order/{order_id}")
def cancel_futures_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Cancel a pending futures order. In live mode, cancels on KuCoin Lead Trading.

    Supports two id formats:
      * Regular pending orders use `client_oid` (matches FuturesOrder.client_oid).
      * Advanced orders (TP/SL stops) returned by /orders use `stop:<exchange_id>`
        because they live only on KuCoin, not in our DB.
    """
    from backend.services.native_trading_engine import _kucoin_post_signed
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    import urllib.request, json as _json

    eng = futures_engine_registry.for_user(user_id)

    # ── Stop-order (Advanced Order) cancel branch ───────────────────────
    if order_id.startswith("stop:"):
        stop_exchange_id = order_id.split(":", 1)[1]
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            return {"error": f"Cannot cancel stop order: {err}", "order_id": order_id}
        try:
            from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
            from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
            ts = str(int(_time.time() * 1000))
            endpoint = f"/api/v1/stopOrders/{stop_exchange_id}"
            headers = _sign_request(
                eng._api_sec, eng._api_pass, eng._api_key,
                ts, "DELETE", endpoint,
            )
            url = f"{_base}{endpoint}"
            req_obj = urllib.request.Request(url, headers=headers, method="DELETE")
            with _proxy_urlopen(req_obj, timeout=15) as resp:
                cancel_resp = _json.loads(resp.read().decode())
            code = str(cancel_resp.get("code", ""))
            if code != "200000":
                return {
                    "error": f"KuCoin rejected stop-order cancel: {cancel_resp.get('msg') or cancel_resp}",
                    "order_id": order_id,
                    "kucoin_cancelled": False,
                }
            log_event(db, user_id, "futures.cancel_stop_order", request,
                      payload={"order_id": order_id})
            return {"kucoin_cancelled": True, "order_id": order_id}
        except Exception as e:
            log.error("[%s] Stop-order cancel failed: %s", user_id, e)
            return {"error": f"Stop-order cancel failed: {e}", "order_id": order_id}

    # An order is "live" if it has an exchange_order_id (it was forwarded to
    # KuCoin Lead Trading). Engine mode is unreliable here because the user
    # may have placed the order in live mode then this request comes through
    # before any bot was ever started.
    db_order = db.execute(
        select(FuturesOrder).where(
            FuturesOrder.client_oid == order_id,
            FuturesOrder.user_id == user_id,
        )
    ).scalar_one_or_none()
    is_live_order = bool(db_order and db_order.exchange_order_id)

    # Track whether the KuCoin DELETE actually succeeded. Previously the
    # DB row was always marked `cancelled` regardless, so the UI showed
    # "cancelled" while the order was still alive on KuCoin — exactly the
    # bug the user reported. Now we only flip the DB status when KuCoin
    # confirms the cancel (or when the order is paper-only).
    kucoin_cancelled = not is_live_order   # paper orders need no exchange call
    kucoin_error: str | None = None
    if is_live_order:
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            log.warning("[%s] cancel-order skipped Lead Trading call: %s", user_id, err)
            kucoin_error = err
        else:
            try:
                from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
                ts = str(int(_time.time() * 1000))
                endpoint = f"/api/v1/copy-trade/futures/orders/{db_order.exchange_order_id}"
                headers = _sign_request(
                    eng._api_sec, eng._api_pass, eng._api_key,
                    ts, "DELETE", endpoint,
                )
                url = f"{_base}{endpoint}"
                req_obj = urllib.request.Request(url, headers=headers, method="DELETE")
                with urllib.request.urlopen(req_obj, timeout=15) as resp:
                    cancel_resp = _json.loads(resp.read().decode())
                code = str(cancel_resp.get("code", ""))
                if code == "200000":
                    kucoin_cancelled = True
                    log.info("[%s] Lead Trading cancel order ok: %s", user_id, cancel_resp)
                else:
                    kucoin_error = cancel_resp.get("msg") or f"KuCoin code {code}"
                    log.warning("[%s] Lead Trading cancel rejected: %s", user_id, cancel_resp)
            except Exception as e:
                kucoin_error = str(e)
                log.error("[%s] Lead Trading cancel failed: %s", user_id, e)

    # If the live cancel failed, surface the error and DON'T mark the row
    # cancelled — leaves it pending so the UI keeps showing it, the user
    # can retry, and KuCoin remains the source of truth.
    if is_live_order and not kucoin_cancelled:
        return {
            "error": (
                f"KuCoin Lead Trading could not cancel the order: {kucoin_error}. "
                "The order is still active on KuCoin — please retry or cancel "
                "directly from the KuCoin tab."
            ),
            "order_id": order_id,
            "kucoin_cancelled": False,
        }

    result = eng.cancel_pending_order(order_id)

    from sqlalchemy import update as sql_update
    db.execute(
        sql_update(FuturesOrder)
        .where(FuturesOrder.client_oid == order_id, FuturesOrder.user_id == user_id)
        .values(status="cancelled", cancelled_at=datetime.utcnow())
    )
    db.commit()

    log_event(db, user_id, "futures.cancel_order", request,
              payload={"order_id": order_id, "kucoin_cancelled": kucoin_cancelled})
    return {**(result or {}), "kucoin_cancelled": kucoin_cancelled, "order_id": order_id}


@router.get("/orders")
def get_futures_orders(
    symbol: str = None,
    status: str = "pending",
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get pending or filled futures orders, filtered by mode for strict
    paper/live isolation. The Live Open Orders tab passes mode=live so paper
    limit orders never leak in, and vice versa.

    For live mode, also reconciles DB rows against KuCoin's actual order
    status — limit orders that filled (e.g. buy-above-market) get flipped
    to 'filled' here so they stop showing in the Open Orders tab while a
    real position is open."""
    eng = futures_engine_registry.for_user(user_id)
    # NOTE: `eng.get_pending_orders()` is intentionally NOT used here.
    # Engine in-memory pending orders are kept for the paper-mode matching
    # loop, but the API responds from the FuturesOrder DB rows so the
    # `mode` column drives strict paper/live separation. See the long
    # comment near the return statement below.

    # ── Live-mode reconcile: ask KuCoin if our 'pending' LIVE rows really
    # are still pending. KuCoin only returns truly-active orders, so any of
    # our rows with an exchange_order_id NOT in that list has either filled
    # or been cancelled. Paper orders never had an exchange_order_id so
    # they're naturally excluded from this reconcile.
    if (status == "pending" or status is None) and mode != "paper" and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            kc_resp = _kucoin_get_signed(
                "/api/v1/orders", eng._api_key, eng._api_sec, eng._api_pass,
                params={"status": "active"},
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(kc_resp.get("code")) == "200000":
                items = (kc_resp.get("data") or {}).get("items") or []
                live_active_ids = {str(o.get("id")) for o in items if o.get("id")}
                pending_rows = db.execute(
                    select(FuturesOrder).where(
                        FuturesOrder.user_id == user_id,
                        FuturesOrder.status == "pending",
                        FuturesOrder.mode == "live",
                        FuturesOrder.exchange_order_id.isnot(None),
                    )
                ).scalars().all()
                changed = False
                for o in pending_rows:
                    if str(o.exchange_order_id) not in live_active_ids:
                        o.status     = "filled"
                        o.filled_at  = datetime.utcnow()
                        changed = True
                        log.info("[%s] Reconcile: order %s no longer active on "
                                 "KuCoin → marked filled", user_id, o.exchange_order_id)
                if changed:
                    db.commit()
        except Exception as e:
            log.warning("[%s] KuCoin order reconcile failed: %s", user_id, e)

    # ── Live-mode: also fetch KuCoin Lead Trading "Advanced Orders" (stop
    # orders, i.e. attached TP/SL). These don't live in our DB because they
    # were placed directly via /st-orders against KuCoin and KuCoin is the
    # source of truth. We merge them into the response so the Open Orders
    # tab can show what KuCoin's "Advanced Orders" sub-tab shows.
    stop_orders: list[dict] = []
    if (status == "pending" or status is None) and mode != "paper" and _ensure_live_credentials(eng, user_id, db)[0]:
        try:
            from backend.services.native_trading_engine import _kucoin_get_signed
            from backend.services.futures_engine import KUCOIN_FUTURES_BASE
            params = {"status": "active"}
            if symbol:
                params["symbol"] = symbol
            so_resp = _kucoin_get_signed(
                "/api/v1/stopOrders", eng._api_key, eng._api_sec, eng._api_pass,
                params=params, base_url=KUCOIN_FUTURES_BASE,
            )
            if str(so_resp.get("code")) == "200000":
                items = (so_resp.get("data") or {}).get("items") or []
                for s in items:
                    stop_dir = (s.get("stop") or "").lower()   # "up" | "down"
                    side     = (s.get("side") or "").lower()
                    is_reduce = bool(s.get("reduceOnly") or s.get("closeOrder"))
                    # Classify TP vs SL by stop direction + close side:
                    #   sell + up   → TP for LONG
                    #   sell + down → SL for LONG
                    #   buy  + down → TP for SHORT
                    #   buy  + up   → SL for SHORT
                    if is_reduce and stop_dir and side:
                        if (side == "sell" and stop_dir == "up") or (side == "buy" and stop_dir == "down"):
                            tp_or_sl = "tp"
                        else:
                            tp_or_sl = "sl"
                    else:
                        tp_or_sl = None
                    stop_orders.append({
                        "order_id":   f"stop:{s.get('id')}",
                        "symbol":     s.get("symbol"),
                        "side":       side,
                        "order_type": s.get("type") or "market",
                        "size":       s.get("size") or 0,
                        "price":      s.get("price"),
                        "stop_price": s.get("stopPrice"),
                        "stop":       stop_dir,
                        "stop_price_type": s.get("stopPriceType"),
                        "leverage":   s.get("leverage"),
                        "margin_mode": s.get("marginMode"),
                        "mode":       "live",
                        "status":     "pending",
                        "kind":       "stop",            # marker for frontend
                        "tp_or_sl":   tp_or_sl,
                        "reduce_only": is_reduce,
                        "close_order": bool(s.get("closeOrder")),
                        "created_at": s.get("createdAt"),
                    })
        except Exception as e:
            log.warning("[%s] KuCoin stop-orders fetch failed: %s", user_id, e)

    query = select(FuturesOrder).where(FuturesOrder.user_id == user_id)
    if status:
        query = query.where(FuturesOrder.status == status)
    if symbol:
        query = query.where(FuturesOrder.symbol == symbol)
    if mode in ("paper", "live"):
        query = query.where(FuturesOrder.mode == mode)
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
            "mode": o.mode,
            "status": o.status,
            "filled_size": o.filled_size,
            "filled_price": o.filled_price,
            "tp_price": o.tp_price,
            "sl_price": o.sl_price,
            "created_at": str(o.created_at),
        }
        for o in db.execute(query).scalars().all()
    ]

    # Always return DB orders — the previous logic returned `engine_orders`
    # for pending status, gated by `eng._mode == mode`. That was buggy:
    #   - User places a live limit order → DB row gets mode='live' ✓
    #   - But `eng._mode` is 'paper' (default — no live bot running)
    #   - Live tab queries ?mode=live → eng._mode != 'live' → returned []
    #   - Paper tab queries ?mode=paper → eng._mode == 'paper' → returned
    #     engine_orders which CONTAINED the live order
    #   ⇒ live limit orders leaked into the Paper tab.
    # All orders are persisted to FuturesOrder on creation (see
    # place_futures_order), so the DB rows are the single source of truth.
    # The mode filter above (`FuturesOrder.mode == mode`) does the right
    # paper/live separation directly off the column.
    # Merge KuCoin stop orders ("Advanced Orders") at the top so users see
    # active TP/SL alongside their regular pending orders.
    return {"orders": stop_orders + db_orders}


@router.get("/orders/history")
def get_futures_order_history(
    symbol: str = None,
    limit: int = 50,
    mode: str = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get filled/cancelled order history, filtered by mode."""
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
    if mode in ("paper", "live"):
        query = query.where(FuturesOrder.mode == mode)

    orders = [
        {
            "order_id": o.client_oid or str(o.id),
            "symbol": o.symbol, "side": o.side, "order_type": o.order_type,
            "size": o.size, "price": o.price, "filled_size": o.filled_size,
            "filled_price": o.filled_price, "fee": o.fee, "status": o.status,
            "mode": o.mode,
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
    """Set leverage for a symbol — updates engine state AND pushes to KuCoin."""
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol   = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    leverage = int(req.get("leverage", 10))
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_leverage(symbol, leverage)
    # Best-effort sync to KuCoin so order placement uses the same value.
    # Silent if creds aren't loaded yet — order path will sync on demand.
    if _ensure_live_credentials(eng, user_id, db)[0]:
        _sync_leverage_to_kucoin(eng, symbol, leverage, user_id)
    log_event(db, user_id, "futures.set_leverage", request, payload=result)
    return result


@router.post("/margin-mode")
def set_futures_margin_mode(
    req: dict,
    request: Request,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Set margin mode (cross/isolated) for a symbol — updates engine AND KuCoin.

    Without the KuCoin sync, the next order would be rejected with
    "The order's margin mode does not match the selected one".
    """
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol = normalize_futures_symbol(req.get("symbol", "XBTUSDTM"))
    mode   = req.get("mode", "cross")
    eng = futures_engine_registry.for_user(user_id)
    result = eng.set_symbol_margin(symbol, mode)

    # Push to KuCoin if creds available. If KuCoin refuses (e.g. open
    # position locks the mode), surface the error so the toggle visibly
    # reverts in the UI instead of silently going out of sync.
    if _ensure_live_credentials(eng, user_id, db)[0]:
        ok, err = _sync_margin_mode_to_kucoin(eng, symbol, mode, user_id)
        if not ok:
            result["warning"] = err
            result["synced_with_kucoin"] = False
        else:
            result["synced_with_kucoin"] = True

    log_event(db, user_id, "futures.set_margin_mode", request, payload=result)
    return result


@router.get("/leverage/{symbol}")
def get_futures_leverage(
    symbol: str,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """Get current leverage + margin mode for a symbol.

    For users with live KuCoin creds, returns KuCoin's REAL values (so the
    leverage selector in the UI reflects what's actually on the exchange,
    not just the engine's last local setting). Falls back to engine memory
    when creds aren't loaded.
    """
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    symbol = normalize_futures_symbol(symbol)
    eng = futures_engine_registry.for_user(user_id)

    leverage    = eng.get_symbol_leverage(symbol)
    margin_mode = eng.get_symbol_margin(symbol)
    source      = "engine"

    # Live mode: prefer KuCoin's reality. This is also what we'll pre-select
    # in the leverage modal on page load, so it matches whatever the user
    # set in KuCoin's own trading UI before opening AutoTrade.
    if _ensure_live_credentials(eng, user_id, db)[0]:
        kc_lev, kc_mode = _fetch_kucoin_symbol_settings(eng, symbol, user_id)
        if kc_lev:
            leverage = kc_lev
            # Mirror into engine so subsequent orders use the same value
            try:
                eng.set_symbol_leverage(symbol, kc_lev)
            except Exception:
                pass
            source = "kucoin"
        if kc_mode in ("CROSS", "ISOLATED"):
            margin_mode = kc_mode.lower()
            try:
                eng.set_symbol_margin(symbol, margin_mode)
            except Exception:
                pass

    return {
        "symbol":      symbol,
        "leverage":    leverage,
        "margin_mode": margin_mode,
        "source":      source,    # "kucoin" if from exchange, "engine" otherwise
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

    Works for three sources of "open position":
      1. Engine in-memory positions (`eng.positions`) — local paper or
         live positions opened through our app.
      2. KuCoin-only positions — opened through our app but not tracked
         locally because the engine was restarted, OR opened externally
         on KuCoin. Falls back to /api/v1/position to fetch size/leverage
         from the exchange.
      3. DB open Trade rows — stores TP/SL for paper mode reconciliation.

    For live positions (whichever source), also places reduceOnly TP/SL
    stop orders on KuCoin Lead Trading so the TP/SL is visible there too.
    """
    from backend.services.native_trading_engine import (
        _kucoin_get_signed, _kucoin_post_signed,
    )
    from backend.services.futures_engine import KUCOIN_FUTURES_BASE
    from backend.services.kucoin_futures_client import normalize_futures_symbol
    import time as _t

    pair     = req.get("pair", "BTC/USDT")
    tp_price = req.get("tp_price")
    sl_price = req.get("sl_price")

    if tp_price is None and sl_price is None:
        return {"error": "Provide tp_price and/or sl_price."}

    eng = futures_engine_registry.for_user(user_id)
    kc_symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))

    # ── Step 1: find the position to attach TP/SL to ────────────────────
    matched_pos = None
    with eng._lock:
        for pos in eng.positions.values():
            if pos.pair == pair:
                if tp_price is not None:
                    pos.tp = float(tp_price)
                if sl_price is not None:
                    pos.sl = float(sl_price)
                matched_pos = pos
                break

    # KuCoin-side metadata (filled in below when needed). Either from the
    # local position object OR from a /api/v1/position fetch.
    kc_direction: str | None = None        # "long" | "short"
    kc_contracts: int = 0
    kc_leverage:  int = max(1, int(getattr(matched_pos, "leverage", 0) or eng._leverage or 1))
    kc_mode:      str = "paper"            # default when nothing is known
    kc_margin:    str = (eng.get_symbol_margin(kc_symbol) or "isolated").upper()

    if matched_pos:
        kc_direction = matched_pos.direction
        contract_size = matched_pos.size * kc_leverage
        kc_contracts  = max(1, int(contract_size / matched_pos.entry * 1000))
        kc_mode       = getattr(matched_pos, "_mode", None) or eng._mode or "paper"

    # ── Step 2: if no engine position, try to fetch from KuCoin ─────────
    if not matched_pos:
        if not _ensure_live_credentials(eng, user_id, db)[0]:
            return {"error": f"No open position for {pair}. Connect a Lead Trading API key in Setup to enable TP/SL on KuCoin-only positions."}
        # Use /api/v1/positions (LIST) + filter, NOT /api/v1/position?symbol=X
        # (SINGLE). The single-position endpoint returns qty=0 for Lead
        # Trading positions even when they're real and visible in the LIST
        # endpoint (which is what powers the reconcile that surfaces them
        # in the UI). Same bug we fixed for force-close.
        try:
            pos_resp = _kucoin_get_signed(
                "/api/v1/positions",
                eng._api_key, eng._api_sec, eng._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
        except Exception as e:
            log.exception("[%s] Failed to fetch KuCoin position for TP/SL", user_id)
            return {"error": f"Could not read your KuCoin position: {e}"}
        if str(pos_resp.get("code")) != "200000":
            return {"error": f"KuCoin rejected position lookup: {pos_resp.get('msg', pos_resp)}"}
        pdata: dict = {}
        qty = 0
        for _p in (pos_resp.get("data") or []):
            if (_p.get("symbol") or "").upper() == kc_symbol.upper():
                _q = int(_p.get("currentQty", 0) or 0)
                if _q != 0:
                    pdata = _p
                    qty = _q
                    break
        if qty == 0:
            return {"error": f"No open position for {pair} on KuCoin Lead Trading either. Open one before setting TP/SL."}
        kc_direction = "long" if qty > 0 else "short"
        kc_contracts = abs(qty)
        # Prefer the real per-symbol leverage KuCoin returns; falls back to
        # configured if KuCoin's value is missing or zero.
        try:
            real_lev = float(pdata.get("realLeverage") or 0)
            cfg_lev  = float(pdata.get("leverage") or 0)
            chosen   = real_lev if real_lev > 0 else cfg_lev
            if chosen > 0:
                kc_leverage = max(1, int(round(chosen)))
        except (TypeError, ValueError):
            pass
        kc_margin = (pdata.get("marginMode") or kc_margin).upper()
        # An open position on KuCoin means this is a LIVE flow.
        kc_mode = "live"

    # ── Step 3: place TP/SL stop orders on KuCoin for live positions ────
    kc_results: dict[str, object] = {}
    if kc_mode == "live":
        ok, err = _ensure_live_credentials(eng, user_id, db)
        if not ok:
            kc_results["warning"] = err
        elif kc_direction is None:
            kc_results["warning"] = "Could not determine position direction."
        else:
            position_side = "LONG" if kc_direction == "long" else "SHORT"
            close_side    = "sell" if kc_direction == "long" else "buy"
            lev           = min(LEAD_MAX_LEVERAGE, kc_leverage)
            margin_mode   = kc_margin or "ISOLATED"

            def _stop_order(label: str, price: float, is_tp: bool) -> dict:
                # Trigger side depends on direction × order kind:
                # Long TP / Short SL  → trigger up
                # Long SL / Short TP  → trigger down
                trig_up = (kc_direction == "long" and is_tp) or (kc_direction == "short" and not is_tp)
                # KuCoin's Lead Trading /copy-trade/futures/st-orders endpoint
                # expects `triggerStopUpPrice` / `triggerStopDownPrice` (NOT
                # the regular-futures `stop` + `stopPrice` field pair). This
                # matches `kucoin_futures_client.place_tp_sl_order` and the
                # existing entry-stop code path in this router.
                body: dict = {
                    "clientOid":     f"atf-{label}-{int(_t.time() * 1000)}",
                    "symbol":         kc_symbol,
                    "marginMode":     margin_mode,
                    "leverage":       lev,
                    "positionSide":   position_side,
                    "side":           close_side,
                    "type":           "market",
                    "size":           kc_contracts,
                    "stopPriceType":  "TP",   # TP = trigger from Last Trade Price
                    "reduceOnly":     True,
                }
                if trig_up:
                    body["triggerStopUpPrice"]   = str(float(price))
                else:
                    body["triggerStopDownPrice"] = str(float(price))
                return body

            if tp_price is not None:
                try:
                    resp = _kucoin_post_signed(
                        "/api/v1/copy-trade/futures/st-orders",
                        _stop_order("tp", float(tp_price), True),
                        eng._api_key, eng._api_sec, eng._api_pass,
                        base_url=KUCOIN_FUTURES_BASE,
                    )
                    code = str(resp.get("code", ""))
                    kc_results["tp"] = {"code": code, "msg": resp.get("msg"), "data": resp.get("data")}
                    if code == "200000":
                        log.info("[%s] Lead Trading TP order placed for %s @ %s", user_id, pair, tp_price)
                    else:
                        log.warning("[%s] Lead Trading TP rejected: %s", user_id, resp)
                except Exception as e:
                    log.exception("[%s] Lead Trading TP failed", user_id)
                    kc_results["tp"] = {"error": str(e)}

            if sl_price is not None:
                try:
                    resp = _kucoin_post_signed(
                        "/api/v1/copy-trade/futures/st-orders",
                        _stop_order("sl", float(sl_price), False),
                        eng._api_key, eng._api_sec, eng._api_pass,
                        base_url=KUCOIN_FUTURES_BASE,
                    )
                    code = str(resp.get("code", ""))
                    kc_results["sl"] = {"code": code, "msg": resp.get("msg"), "data": resp.get("data")}
                    if code == "200000":
                        log.info("[%s] Lead Trading SL order placed for %s @ %s", user_id, pair, sl_price)
                    else:
                        log.warning("[%s] Lead Trading SL rejected: %s", user_id, resp)
                except Exception as e:
                    log.exception("[%s] Lead Trading SL failed", user_id)
                    kc_results["sl"] = {"error": str(e)}

    # ── Step 4: persist on the DB Trade row (paper reconciliation + UI) ──
    trade = db.execute(
        select(Trade).where(
            Trade.user_id == user_id, Trade.pair == pair,
            Trade.market_type == "futures", Trade.status == "open",
        ).order_by(desc(Trade.entry_time)).limit(1)
    ).scalar_one_or_none()
    if trade:
        if sl_price is not None:
            trade.stoploss_price = float(sl_price)
        db.commit()

    log_event(db, user_id, "futures.set_tp_sl", request,
              payload={"pair": pair, "tp": tp_price, "sl": sl_price,
                       "source": "engine" if matched_pos else "kucoin"})
    return {
        "updated": True,
        "pair": pair,
        "tp_price": tp_price,
        "sl_price": sl_price,
        "source": "engine" if matched_pos else "kucoin",
        "kucoin": kc_results,
    }


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
    mode: str | None = None,
    db: Session = Depends(get_db),
    user_id: str = Depends(get_user_id),
):
    """List futures bot instances for this user, optionally filtered by mode (paper/live)."""
    query = (
        select(StrategyInstance)
        .where(StrategyInstance.user_id == user_id, StrategyInstance.market_type == "futures")
    )
    if mode in ("paper", "live"):
        query = query.where(StrategyInstance.mode == mode)
    query = query.order_by(desc(StrategyInstance.created_at))
    instances = db.execute(query).scalars().all()

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
    leverage      = min(LEAD_MAX_LEVERAGE, int(req.get("leverage", 10)))
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

    # ── Deduplication: stop any existing running bot for same strategy+pair+mode ──
    pairs_csv = ",".join(sorted(p.strip() for p in pairs))
    existing = db.execute(
        select(StrategyInstance).where(
            StrategyInstance.user_id == user_id,
            StrategyInstance.strategy_name == strategy_name,
            StrategyInstance.pairs == pairs_csv,
            StrategyInstance.mode == mode,
            StrategyInstance.market_type == "futures",
            StrategyInstance.is_running == True,
        )
    ).scalars().all()
    for ex in existing:
        # Stop the old engine if running
        if ex.engine_key:
            bot_engines = {k: e for k, e in futures_engine_registry.user_bot_engines(user_id)}
            old_eng = bot_engines.get(ex.engine_key)
            if old_eng and old_eng.is_running:
                old_eng._stop_evt.set()
        ex.is_running = False
    if existing:
        db.commit()
        log.info("[%s] Stopped %d duplicate bot(s) for %s/%s/%s",
                 user_id, len(existing), strategy_name, pairs_csv, mode)

    engine_key = f"bot-{strategy_name}-{int(_time.time() * 1000)}"
    instance = StrategyInstance(
        user_id=user_id, strategy_id=strategy_id, strategy_name=strategy_name,
        market_type="futures", mode=mode, pairs=pairs_csv,
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
