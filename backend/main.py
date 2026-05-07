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
    or when RSI < 38 (oversold). Exits at +1.5% take-profit or -1.5% stop-loss.
    Works in any market condition — fires every few candles.
    """
    minimal_roi = {"0": 0.015}
    stoploss = -0.015
    timeframe = "15m"
'''

def _seed_builtin_strategies(db):
    """Ensure SimpleTargetStrategy exists as a global template in the DB."""
    from backend.models.strategy import Strategy
    existing = db.execute(
        select(Strategy).where(Strategy.name == "SimpleTargetStrategy", Strategy.is_template == True)  # noqa: E712
    ).scalar_one_or_none()
    if not existing:
        db.add(Strategy(
            user_id="system",
            name="SimpleTargetStrategy",
            description="Buys on RSI dips near EMA-20. Takes profit at +1.5%, stops at -1.5%. "
                        "Fires frequently in any market — good for testing the bot works.",
            original_text="Buy on pullbacks, sell at target",
            generated_code=_SIMPLE_STRATEGY_CODE,
            timeframe="15m",
            stoploss=-0.015,
            is_template=True,
        ))
        db.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    # Seed built-in strategies
    try:
        with SessionLocal() as db:
            _seed_builtin_strategies(db)
    except Exception:
        pass
    # ── Auto-resume all engines that were running before a container restart ──
    try:
        from backend.services.native_trading_engine import native_engine_registry
        with SessionLocal() as db:
            rows = db.execute(select(Config)).scalars().all()
            for cfg in rows:
                if not cfg.user_id:
                    continue
                # 1. Auto-trade engine
                if cfg.auto_trade_enabled:
                    try:
                        autotrade_engine.for_user(cfg.user_id).start()
                    except Exception:
                        pass
                # 2. Paper / live bot — auto-resume if it was running
                if cfg.bot_running and cfg.bot_strategy_name:
                    try:
                        pairs = [p.strip() for p in (cfg.bot_pairs or "BTC/USDT").split(",") if p.strip()]
                        eng = native_engine_registry.for_user(cfg.user_id)
                        if cfg.bot_mode == "live":
                            # Live requires credentials — skip if not decryptable
                            from backend.utils.encryption import decrypt, DecryptError
                            try:
                                kk = decrypt(cfg.kucoin_key_enc or "", cfg.user_id)
                                ks = decrypt(cfg.kucoin_secret_enc or "", cfg.user_id)
                                kp = decrypt(cfg.kucoin_passphrase_enc or "", cfg.user_id)
                                eng.start_live(
                                    strategy_name=cfg.bot_strategy_name,
                                    pairs=pairs,
                                    timeframe=cfg.bot_timeframe or "15m",
                                    stoploss=cfg.bot_stoploss or -0.03,
                                    kucoin_key=kk, kucoin_secret=ks, kucoin_passphrase=kp,
                                    wallet=cfg.bot_wallet or 1000.0,
                                )
                            except DecryptError:
                                pass  # Credentials changed; user must restart manually
                        else:
                            eng.start_paper(
                                strategy_name=cfg.bot_strategy_name,
                                pairs=pairs,
                                timeframe=cfg.bot_timeframe or "15m",
                                stoploss=cfg.bot_stoploss or -0.03,
                                wallet=cfg.bot_wallet or 1000.0,
                            )
                    except Exception:
                        pass
    except Exception:
        pass
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
    return {
        "status": "healthy",
        "active_users": freqtrade_mgr.active_users(),
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
