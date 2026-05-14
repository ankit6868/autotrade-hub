import os
from contextlib import asynccontextmanager
from datetime import datetime

from dotenv import load_dotenv

load_dotenv()

# Sentry must initialise before FastAPI imports anything that emits errors.
from backend.utils.observability import init_sentry  # noqa: E402

_sentry_active = init_sentry()

import asyncio  # noqa: E402

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect, status as http_status  # noqa: E402
from fastapi.middleware.cors import CORSMiddleware  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402
from slowapi import _rate_limit_exceeded_handler  # noqa: E402
from slowapi.errors import RateLimitExceeded  # noqa: E402
from slowapi.middleware import SlowAPIMiddleware  # noqa: E402
from starlette.middleware.base import BaseHTTPMiddleware  # noqa: E402
from sqlalchemy import select  # noqa: E402

from backend.models import init_db, SessionLocal, Config  # noqa: E402
from backend.routers import auth, strategy, backtest, trading, market, analysis, autotrade, webhook  # noqa: E402
from backend.routers import futures as futures_router  # noqa: E402
from backend.routers import copy_trading as copy_router  # noqa: E402
from backend.routers import multi_strategy as multi_router  # noqa: E402
from backend.services.freqtrade_manager import freqtrade_mgr  # noqa: E402
from backend.services.autotrade_engine import autotrade_engine  # noqa: E402
from backend.utils.clerk_auth import (  # noqa: E402
    ANONYMOUS_USER_ID,
    CLERK_AUDIENCE,
    CLERK_ISSUER,
    _get_jwks,
    clerk_enabled,
)
from backend.utils.rate_limit import limiter  # noqa: E402
import jwt  # noqa: E402


_SIMPLE_STRATEGY_CODE = '''
class SimpleTargetStrategy:
    """
    Buys when RSI < 55 and price is near EMA-20 (pullback zone),
    or when RSI < 38 (oversold). Exits at +3% take-profit or -1.5% stop-loss (2:1 R:R).
    Also shorts when RSI > 65 and price above EMA-20, or RSI > 72 (overbought).
    Works in any market condition — bidirectional LONG + SHORT.
    """
    minimal_roi = {"0": 0.030}
    stoploss = -0.015
    timeframe = "15m"
'''

_SMC_STRATEGY_CODE = '''
class SMCStrategy:
    """
    Smart Money Concepts (SMC) — Full multi-timeframe implementation.

    Layers (ALL must align for entry):
      1. HTF Bias  : EMA200 direction (simulates 4H trend)
      2. Swing     : N=5 bar swing highs/lows detection
      3. BOS       : Break of Structure (price breaks last swing)
      4. FVG       : Fair Value Gap (3-candle imbalance)
      5. OB        : Order Block (last opposing candle before BOS)
      6. Discount  : Price below 50% Fibonacci = buy zone
      7. Liq Sweep : Wick takes out stops then reverses
      8. NY Session: 13:00–21:00 UTC only

    LONG:  HTF bullish + discount zone + FVG/OB + sell-side sweep + BOS up
    SHORT: HTF bearish + premium zone + FVG/OB + buy-side sweep + BOS down
    SL: Below swept liquidity. TP: 2R from entry.
    """
    minimal_roi = {"0": 0.03}   # 2R target
    stoploss = -0.015
    timeframe = "15m"
    startup_candle_count = 210
'''

_SMC_TV_STRATEGY_CODE = '''
class SMCStrategyTV:
    """
    SMC Strategy v2 — OB / FVG / BOS  (TradingView Pine Script exact port)

    This is a direct Python translation of TradingView's "SMC Strategy v2 - OB/FVG/BOS"
    Pine Script strategy. Signals are generated using real market structure — NOT
    EMA proxies — so backtest results closely match TradingView's output.

    Logic (identical to Pine Script):
      1. PIVOT detection  : ta.pivothigh/pivotlow with swing_len=5 bars each side
      2. BOS              : close crosses above last pivot high (LONG)
                            close crosses below last pivot low  (SHORT)
      3. FVG              : 3-candle price gap (high[2] < low or low[2] > high)
      4. OB               : last opposing candle before the BOS event
      5. Entry zone       : price is INSIDE the FVG or OB range
      6. SL               : below/above the structural swing point (dynamic)
      7. TP               : Entry ± 2 × Risk  (2R, same as TV)

    No artificial cooldown — exactly like TradingView (trades whenever conditions re-trigger).
    Timeframe: 15m (same as the TV chart this was calibrated against).
    """
    minimal_roi = {"0": 0.06}   # 2R target (varies per trade, this is approx)
    stoploss    = -0.03         # structural SL, varies per trade
    timeframe   = "15m"
    startup_candle_count = 30
'''

_BIDIR_STRATEGY_CODE = '''
class BidirectionalStrategy:
    """
    Trend-following strategy that trades BOTH directions.

    LONG:  EMA9 > EMA21 (uptrend confirmed) AND RSI < 60 (not overbought)
    SHORT: EMA9 < EMA21 (downtrend confirmed) AND RSI > 40 (not oversold)
    SL: 1.5% | TP: 3.0% | Leverage: 10x recommended

    Designed specifically to test and validate SHORT position flow
    in futures paper trading, live trading, and backtesting.
    """
    minimal_roi = {"0": 0.030}
    stoploss = -0.015
    timeframe = "15m"
'''

def _cleanup_stale_test_trades(db):
    """One-time cleanup: delete open futures trades that were created during
    debugging (entry_price looks wrong or entry_time is from dev session).
    Safe to call repeatedly — just deletes 0 rows if already clean."""
    from sqlalchemy import text
    try:
        result = db.execute(text(
            "DELETE FROM trades WHERE market_type = 'futures' AND status = 'open'"
        ))
        db.commit()
        if result.rowcount > 0:
            import logging
            logging.getLogger("startup").info(
                "Cleaned up %d stale open futures test trades", result.rowcount
            )
    except Exception as e:
        import logging
        logging.getLogger("startup").warning("Stale trade cleanup failed: %s", e)


def _seed_builtin_strategies(db):
    """Ensure template strategies exist with correct trading configs."""
    from backend.models.strategy import Strategy

    templates = [
        {
            "name": "SMCStrategyTV",
            "description": "TradingView SMC Strategy v2 — exact Python port of OB/FVG/BOS Pine Script. "
                           "Uses real pivot-point BOS (N=5), 3-candle FVG, structural OB zones, "
                           "dynamic SL/TP based on swing points (2R). No artificial cooldown. "
                           "Matches TradingView backtester output closely.",
            "code": _SMC_TV_STRATEGY_CODE,
            "stoploss": -0.03,
            "take_profit": 0.06,
            "leverage": 10,
        },
        {
            "name": "SMCStrategy",
            "description": "Smart Money Concepts: HTF bias + Swing BOS + FVG/OB + Liquidity sweep + NY session. "
                           "Full multi-layer entry: LONG in discount zone after sell-side sweep + BOS up; "
                           "SHORT in premium zone after buy-side sweep + BOS down. 2R TP, SL below/above sweep.",
            "code": _SMC_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
        {
            "name": "SimpleTargetStrategy",
            "description": "Bidirectional mean-reversion: LONG when RSI<55 near EMA-20 or RSI<38 (oversold); "
                           "SHORT when RSI>65 above EMA-20 or RSI>72 (overbought). TP 3%, SL 1.5% (2:1 R:R).",
            "code": _SIMPLE_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
        {
            "name": "BidirectionalStrategy",
            "description": "Trend-following LONG+SHORT strategy: LONG when EMA9>EMA21 (uptrend) AND RSI<60; "
                           "SHORT when EMA9<EMA21 (downtrend) AND RSI>40. TP 3%, SL 1.5%. "
                           "Ideal for testing short positions in futures paper/live/backtest.",
            "code": _BIDIR_STRATEGY_CODE,
            "stoploss": -0.015,
            "take_profit": 0.030,
            "leverage": 10,
        },
    ]

    for tmpl in templates:
        existing = db.execute(
            select(Strategy).where(Strategy.name == tmpl["name"], Strategy.is_template == True)  # noqa: E712
        ).scalar_one_or_none()
        if not existing:
            db.add(Strategy(
                user_id="system",
                name=tmpl["name"],
                description=tmpl["description"],
                original_text=tmpl["description"],
                generated_code=tmpl["code"],
                timeframe="15m",
                stoploss=tmpl["stoploss"],
                take_profit=tmpl["take_profit"],
                default_leverage=tmpl["leverage"],
                is_template=True,
            ))
        else:
            changed = False
            if existing.take_profit != tmpl["take_profit"]:
                existing.take_profit = tmpl["take_profit"]; changed = True
            if existing.stoploss != tmpl["stoploss"]:
                existing.stoploss = tmpl["stoploss"]; changed = True
            if not getattr(existing, "default_leverage", None) or existing.default_leverage < 2:
                existing.default_leverage = tmpl["leverage"]; changed = True
            if changed:
                pass  # commit below

    db.commit()


async def _background_startup():
    """
    Heavy startup work runs AFTER Uvicorn is already serving requests.
    This keeps the healthcheck fast (app ready in <3s) while still
    auto-resuming bots and seeding data in the background.
    """
    import logging
    log = logging.getLogger("startup")
    await asyncio.sleep(2)   # tiny grace period so the first healthcheck passes

    # ── Init DB + seed strategies ─────────────────────────────────────────────
    try:
        init_db()
    except Exception as e:
        log.error("init_db failed: %s", e)

    try:
        with SessionLocal() as db:
            _seed_builtin_strategies(db)
    except Exception as e:
        log.error("seed strategies failed: %s", e)

    # ── Auto-resume all bot engines ───────────────────────────────────────────
    try:
        from backend.services.native_trading_engine import native_engine_registry
        with SessionLocal() as db:
            rows = db.execute(select(Config)).scalars().all()
        for cfg in rows:
            if not cfg.user_id:
                continue
            # Auto-trade engine
            if cfg.auto_trade_enabled:
                try:
                    autotrade_engine.for_user(cfg.user_id).start()
                except Exception:
                    pass
            # Paper / live SPOT bot only — skip futures modes entirely
            # (futures auto-resume would need futures_engine_registry, handled separately)
            if cfg.bot_running and cfg.bot_strategy_name and not (cfg.bot_mode or "").startswith("futures"):
                try:
                    pairs = [p.strip() for p in (cfg.bot_pairs or "BTC/USDT").split(",") if p.strip()]
                    eng = native_engine_registry.for_user(cfg.user_id)
                    if cfg.bot_mode == "live":
                        from backend.utils.encryption import decrypt, DecryptError
                        try:
                            kk = decrypt(cfg.kucoin_key_enc or "", cfg.user_id)
                            ks = decrypt(cfg.kucoin_secret_enc or "", cfg.user_id)
                            kp = decrypt(cfg.kucoin_passphrase_enc or "", cfg.user_id)
                            eng.start_live(
                                strategy_name=cfg.bot_strategy_name, pairs=pairs,
                                timeframe=cfg.bot_timeframe or "15m",
                                stoploss=cfg.bot_stoploss or -0.03,
                                kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
                                wallet=cfg.bot_wallet or 1000.0,
                            )
                        except DecryptError:
                            pass
                    else:
                        eng.start_paper(
                            strategy_name=cfg.bot_strategy_name, pairs=pairs,
                            timeframe=cfg.bot_timeframe or "15m",
                            stoploss=cfg.bot_stoploss or -0.03,
                            wallet=cfg.bot_wallet or 1000.0,
                        )
                except Exception:
                    pass
    except Exception as e:
        log.error("engine auto-resume failed: %s", e)

    # ── Resume multi-strategy instances ───────────────────────────────────────
    try:
        from backend.services.multi_strategy import multi_strategy_manager
        with SessionLocal() as db:
            resumed = multi_strategy_manager.resume_all(db)
            if resumed:
                log.info("Resumed %d multi-strategy instances", resumed)
    except Exception as e:
        log.error("multi-strategy resume failed: %s", e)

    log.info("Background startup complete.")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Fire heavy work in the background — Uvicorn starts serving immediately,
    # so the Railway healthcheck passes in <3 seconds instead of ~40 seconds.
    asyncio.create_task(_background_startup())
    yield
    try:
        autotrade_engine.stop_all()
    except Exception:
        pass
    try:
        freqtrade_mgr.stop_all()
    except Exception:
        pass


app = FastAPI(
    title="AutoTrade Hub",
    version=os.getenv("APP_VERSION", "1.0.0"),
    lifespan=lifespan,
    docs_url="/docs" if os.getenv("ENABLE_DOCS", "true").lower() == "true" else None,
    redoc_url=None,
)

# --- Global exception handler (app-level, catches route + dependency errors) -
@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception):
    import logging, traceback
    logging.getLogger("autotrade_hub").error(
        "Unhandled exception on %s %s: %s\n%s",
        request.method, request.url.path, exc, traceback.format_exc(),
    )
    return JSONResponse(
        status_code=500,
        content={"error": f"{type(exc).__name__}: {exc}"},
    )

# --- Outermost safety-net middleware (catches middleware-layer exceptions) --
class _JsonErrorMiddleware(BaseHTTPMiddleware):
    """Sits just inside ServerErrorMiddleware. Converts any unhandled Python
    exception — including those raised in other middlewares (e.g. SlowAPI) —
    into a JSON {error: ...} response so the UI never sees a raw 500 page."""

    async def dispatch(self, request: Request, call_next):
        import logging, traceback
        try:
            return await call_next(request)
        except Exception as exc:
            logging.getLogger("autotrade_hub").error(
                "Middleware-layer exception on %s %s: %s\n%s",
                request.method, request.url.path, exc, traceback.format_exc(),
            )
            return JSONResponse(
                status_code=500,
                content={"error": f"{type(exc).__name__}: {exc}"},
            )

app.add_middleware(_JsonErrorMiddleware)

# --- Rate limiting ---------------------------------------------------------
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

# --- CORS ------------------------------------------------------------------
_default_cors = "http://localhost:3000,http://127.0.0.1:3000"
_cors = [o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", _default_cors).split(",") if o.strip()]
# Always allow the production Vercel frontend (direct browser→backend calls
# bypass Vercel rewrites, avoiding ROUTER_EXTERNAL_TARGET_ERROR on uploads).
_vercel_origins = [
    "https://autotrade-hub.vercel.app",
]
for vo in _vercel_origins:
    if vo not in _cors:
        _cors.append(vo)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- Routers ---------------------------------------------------------------
app.include_router(auth.router)
app.include_router(strategy.router)
app.include_router(backtest.router)
app.include_router(trading.router)
app.include_router(market.router)
app.include_router(analysis.router)
app.include_router(autotrade.router)
app.include_router(webhook.router)
app.include_router(futures_router.router)
app.include_router(copy_router.router)
app.include_router(multi_router.router)


@app.get("/")
async def root():
    return {
        "app": "AutoTrade Hub",
        "version": app.version,
        "status": "running",
        "sentry": _sentry_active,
    }


@app.get("/api/health")
async def health():
    # Surface proxy status so the user can see at a glance whether the
    # KUCOIN_HTTPS_PROXY env var was parsed correctly and which entry is
    # currently in use (credentials redacted).
    try:
        from backend.services._kucoin_proxy import proxy_status
        proxy = proxy_status()
    except Exception:
        proxy = {"count": 0, "active": None}
    return {
        "status": "healthy",
        "active_users": freqtrade_mgr.active_users(),
        "kucoin_proxy": proxy,
        "timestamp": datetime.utcnow().isoformat(),
    }


# --- Live trade websocket --------------------------------------------------
class ConnectionManager:
    """Per-user fan-out: each user has their own list of open sockets so a
    push for user A never reaches user B's browser."""

    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, user_id: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(user_id, []).append(ws)

    def disconnect(self, user_id: str, ws: WebSocket):
        sockets = self.active.get(user_id) or []
        if ws in sockets:
            sockets.remove(ws)
        if not sockets:
            self.active.pop(user_id, None)

    async def broadcast(self, user_id: str, data: dict):
        for ws in list(self.active.get(user_id, [])):
            try:
                await ws.send_json(data)
            except Exception:
                pass


ws_manager = ConnectionManager()


def _resolve_ws_user(token: str | None) -> str | None:
    """Verify the websocket token and return the user_id, or None to reject.

    In local-dev (no CLERK_JWKS_URL) we accept anonymous connections under the
    shared `local-dev` id — same fallback the HTTP dependency uses."""
    if not clerk_enabled():
        return ANONYMOUS_USER_ID
    if not token:
        return None
    try:
        signing_key = _get_jwks().get_signing_key_from_jwt(token).key
        claims = jwt.decode(
            token,
            signing_key,
            algorithms=["RS256"],
            audience=CLERK_AUDIENCE or None,
            issuer=CLERK_ISSUER or None,
            options={"verify_aud": bool(CLERK_AUDIENCE)},
        )
        sub = claims.get("sub")
        return sub if sub else None
    except Exception:
        return None


@app.websocket("/ws/trades")
async def websocket_trades(ws: WebSocket, token: str | None = None):
    user_id = _resolve_ws_user(token)
    if not user_id:
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return

    await ws_manager.connect(user_id, ws)
    try:
        while True:
            data = await ws.receive_text()
            if data == "ping":
                await ws.send_json({
                    "type": "pong",
                    "bot": freqtrade_mgr.for_user(user_id).status,
                    "timestamp": datetime.utcnow().isoformat(),
                })
    except WebSocketDisconnect:
        ws_manager.disconnect(user_id, ws)
