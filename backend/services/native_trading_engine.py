"""
NativeTradingEngine — Production-grade trading engine for AutoTrade Hub.

Replaces the Freqtrade subprocess with a fully native Python implementation.
Works on any cloud host (Railway, Render, Fly.io) with zero C-library deps.

Architecture
────────────
NativeTradingRegistry  — process-wide map of user_id → NativeTradingEngine
NativeTradingEngine    — one trading session per user (paper or live)
  ├─ CandleManager     — polls KuCoin REST for OHLCV, builds indicator frames
  ├─ StrategyRunner    — signal generation for all built-in strategies
  ├─ PositionManager   — open/closed position tracking with SL/TP/trailing
  └─ OrderExecutor     — virtual (paper) or real (KuCoin API) order placement

Features
────────
• Multi-user isolated (each user's engine is a separate thread, separate state)
• Fail-safe: network errors retry with exponential back-off; engine auto-resumes
• Supports: MissCandleShort/Long, MacdCrossover, RsiBollinger, EmaScalping
• Full risk management: fixed SL/TP, trailing stop, max drawdown guard
• Persists trades to the app DB so History page shows real data
• Paper + Live modes; Live places actual KuCoin REST orders
• Thread-safe: all shared state protected by threading.Lock
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
import traceback
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

log = logging.getLogger("native_engine")


# ─── DB persistence helpers (open + closed trades) ────────────────────────

def _persist_open_trade(user_id: str, pos: "Position", mode: str,
                        strategy_id: int | None = None,
                        leverage: int = 1, market_type: str = "spot") -> int | None:
    """Insert an open Position into the DB Trade table. Returns the new trade DB id."""
    try:
        from backend.models.database import SessionLocal
        from backend.models.trade import Trade as TradeModel
        db = SessionLocal()
        try:
            trade = TradeModel(
                user_id           = user_id,
                mode              = mode if mode in ("paper", "live") else "paper",
                market_type       = market_type,
                pair              = pos.pair,
                side              = pos.direction,
                leverage          = leverage,
                liquidation_price = getattr(pos, "liquidation_price", None),
                entry_price       = round(pos.entry, 8),
                amount            = round(pos.size, 8),
                stoploss_price    = round(pos.sl, 8),
                entry_time        = pos.opened_at,
                status            = "open",
                strategy_id       = strategy_id,
            )
            db.add(trade)
            db.commit()
            db.refresh(trade)
            log.info("[%s] Open trade saved to DB id=%s %s @ %.4f", user_id, trade.id, pos.pair, pos.entry)
            return trade.id
        except Exception as e:
            db.rollback()
            log.error("[%s] Failed to save open trade to DB: %s", user_id, e)
            return None
        finally:
            db.close()
    except Exception as e:
        log.error("DB persistence import error: %s", e)
        return None


def _persist_closed_trade(user_id: str, pos: "Position", mode: str,
                           strategy_id: int | None = None, db_id: int | None = None) -> None:
    """Update existing open DB Trade to closed, or insert new closed record."""
    try:
        from backend.models.database import SessionLocal
        from backend.models.trade import Trade as TradeModel
        from sqlalchemy import select
        db = SessionLocal()
        try:
            trade = None
            # Try to update existing open record first (matched by db_id or pair+user+open)
            if db_id:
                trade = db.get(TradeModel, db_id)
            if trade is None:
                # Fallback: find open record by pair + user
                result = db.execute(
                    select(TradeModel).where(
                        TradeModel.user_id == user_id,
                        TradeModel.pair == pos.pair,
                        TradeModel.status == "open",
                    ).order_by(TradeModel.id.desc()).limit(1)
                )
                trade = result.scalar_one_or_none()

            if trade:
                # Update existing row
                trade.exit_price  = round(pos.exit_price or pos.entry, 8)
                trade.profit_pct  = round(pos.pnl_pct, 4)
                trade.profit_abs  = round(pos.pnl_abs, 4)
                trade.exit_time   = pos.closed_at or datetime.now(timezone.utc)
                trade.exit_reason = pos.exit_reason or "unknown"
                trade.status      = "closed"
            else:
                # Insert new closed record (fallback for trades opened before this fix)
                trade = TradeModel(
                    user_id        = user_id,
                    mode           = mode if mode in ("paper", "live") else "paper",
                    pair           = pos.pair,
                    side           = pos.direction,
                    entry_price    = round(pos.entry, 8),
                    exit_price     = round(pos.exit_price or pos.entry, 8),
                    amount         = round(pos.size, 8),
                    profit_pct     = round(pos.pnl_pct, 4),
                    profit_abs     = round(pos.pnl_abs, 4),
                    stoploss_price = round(pos.sl, 8),
                    entry_time     = pos.opened_at,
                    exit_time      = pos.closed_at or datetime.now(timezone.utc),
                    exit_reason    = pos.exit_reason or "unknown",
                    status         = "closed",
                    strategy_id    = strategy_id,
                )
                db.add(trade)
            db.commit()
            log.info("[%s] Closed trade saved to DB: %s %s pnl=%.4f", user_id, pos.pair, pos.exit_reason, pos.pnl_abs)
        except Exception as e:
            db.rollback()
            log.error("[%s] Failed to save closed trade to DB: %s", user_id, e)
        finally:
            db.close()
    except Exception as e:
        log.error("DB persistence import error: %s", e)

# ─────────────────────────── constants ────────────────────────────────────

TF_SECONDS = {
    "1m": 60, "3m": 180, "5m": 300, "15m": 900, "30m": 1800,
    "1h": 3600, "2h": 7200, "4h": 14400, "6h": 21600,
    "8h": 28800, "12h": 43200, "1d": 86400,
}
TF_KUCOIN = {
    "1m": "1min", "3m": "3min", "5m": "5min", "15m": "15min",
    "30m": "30min", "1h": "1hour", "2h": "2hour", "4h": "4hour",
    "6h": "6hour", "8h": "8hour", "12h": "12hour", "1d": "1day",
}
CANDLE_HISTORY = 100   # number of closed candles to keep in memory
KUCOIN_BASE = "https://api.kucoin.com"


# ─────────────────────────── helpers ──────────────────────────────────────

def _kucoin_get(path: str, params: dict | None = None) -> dict:
    """GET from KuCoin public REST API (no auth required)."""
    from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    url = f"{KUCOIN_BASE}{path}{qs}"
    req = urllib.request.Request(url, headers={"User-Agent": "AutoTradeHub/2.0"})
    with _proxy_urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _kucoin_post_signed(path: str, body: dict, api_key: str,
                         api_secret: str, passphrase: str,
                         base_url: str = KUCOIN_BASE) -> dict:
    """POST to KuCoin private REST API (signed). base_url allows switching to Futures API."""
    import base64, hashlib, hmac as _hmac
    from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
    ts = str(int(time.time() * 1000))
    body_str = json.dumps(body)
    str_to_sign = f"{ts}POST{path}{body_str}"
    sig = base64.b64encode(_hmac.new(
        api_secret.encode(), str_to_sign.encode(), hashlib.sha256
    ).digest()).decode()
    pp_sig = base64.b64encode(_hmac.new(
        api_secret.encode(), passphrase.encode(), hashlib.sha256
    ).digest()).decode()
    headers = {
        "KC-API-KEY": api_key,
        "KC-API-SIGN": sig,
        "KC-API-TIMESTAMP": ts,
        "KC-API-PASSPHRASE": pp_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type": "application/json",
    }
    url = f"{base_url}{path}"
    data = body_str.encode()
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    with _proxy_urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _kucoin_get_signed(path: str, api_key: str, api_secret: str,
                       passphrase: str, params: dict | None = None,
                       base_url: str = KUCOIN_BASE) -> dict:
    """Authenticated GET from KuCoin private REST API."""
    import base64, hashlib, hmac as _hmac
    from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
    qs = ("?" + urllib.parse.urlencode(params)) if params else ""
    ts = str(int(time.time() * 1000))
    str_to_sign = f"{ts}GET{path}{qs}"
    sig = base64.b64encode(_hmac.new(
        api_secret.encode(), str_to_sign.encode(), hashlib.sha256
    ).digest()).decode()
    pp_sig = base64.b64encode(_hmac.new(
        api_secret.encode(), passphrase.encode(), hashlib.sha256
    ).digest()).decode()
    headers = {
        "KC-API-KEY":         api_key,
        "KC-API-SIGN":        sig,
        "KC-API-TIMESTAMP":   ts,
        "KC-API-PASSPHRASE":  pp_sig,
        "KC-API-KEY-VERSION": "2",
        "Content-Type":       "application/json",
        "User-Agent":         "AutoTradeHub/2.0",
    }
    url = f"{base_url}{path}{qs}"
    req = urllib.request.Request(url, headers=headers)
    with _proxy_urlopen(req, timeout=20) as resp:
        return json.loads(resp.read().decode())


def _fetch_candles(symbol: str, ktype: str, limit: int = CANDLE_HISTORY) -> list[dict]:
    """Fetch the last `limit` closed candles from KuCoin."""
    now = int(time.time())
    tf_secs = TF_SECONDS.get(ktype, 900)
    start = now - tf_secs * (limit + 2)
    data = _kucoin_get("/api/v1/market/candles", {
        "type": TF_KUCOIN.get(ktype, "15min"),
        "symbol": symbol,
        "startAt": start,
        "endAt": now,
    })
    if str(data.get("code")) != "200000":
        return []
    rows = []
    for r in sorted(data.get("data", []), key=lambda x: int(x[0])):
        ts = int(r[0])
        if ts >= now - tf_secs:
            continue  # skip the currently open candle
        rows.append({
            "ts": ts,
            "open":  float(r[1]),
            "close": float(r[2]),
            "high":  float(r[3]),
            "low":   float(r[4]),
            "vol":   float(r[5]),
        })
    return rows[-limit:]


def _build_df(candles: list[dict]) -> pd.DataFrame:
    if not candles:
        return pd.DataFrame()
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["ts"], unit="s", utc=True)
    # Indicators
    df["ema5"]  = df["close"].ewm(span=5,  adjust=False).mean()
    df["ema9"]  = df["close"].ewm(span=9,  adjust=False).mean()
    df["ema20"] = df["close"].ewm(span=20, adjust=False).mean()   # ← used by SimpleTarget
    df["ema21"] = df["close"].ewm(span=21, adjust=False).mean()
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"]   = ema12 - ema26
    df["macd_s"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_h"] = df["macd"] - df["macd_s"]
    delta = df["close"].diff()
    gain  = delta.clip(lower=0).ewm(com=13, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(com=13, adjust=False).mean()
    df["rsi"] = 100 - 100 / (1 + gain / loss.replace(0, 1e-9))
    df["bb_mid"]   = df["close"].rolling(20).mean()
    bb_std         = df["close"].rolling(20).std()
    df["bb_upper"] = df["bb_mid"] + 2 * bb_std
    df["bb_lower"] = df["bb_mid"] - 2 * bb_std
    df["vol_sma"] = df["vol"].rolling(20).mean()
    return df.reset_index(drop=True)


# ─────────────────────────── Strategy-runner integration ──────────────────
#
# This is the "Phase 2" plumbing — make the LIVE/PAPER engine execute the
# user's own strategy code, the same way the futures backtester does.
#
# Before this: the live engine called _get_signal_fn(strategy_name) which
# fuzzy-matched the name to one of 8 hardcoded signal functions, or fell
# back to _sig_simple_target. BestPracticesV1 / SMCStrategyTV / any
# AI-uploaded strategy never actually ran their populate_* methods in
# paper or live — only the backtester did.
#
# After this: we compile the strategy class via strategy_runner.evaluate_strategy
# (the SAME pipeline the backtester uses) and build a signal_fn from the
# resulting dataframe. If compilation fails (e.g. strategy has no generated_code,
# or the code raises), we gracefully fall back to the legacy name-matched
# function so existing built-in strategies keep working.

class StrategyCompileError(RuntimeError):
    """Raised when the engine can't run the user's strategy code.

    The previous version (pre spot-purge) silently fell back to a fuzzy
    name-matched legacy signal function when the user's `generated_code`
    failed to compile. That meant bots could run live with a completely
    different strategy than the user authored — exactly the "silent
    fallback" bug the PDF §1 was written to eliminate.

    After the spot purge there's only one truth: strategy_runner must be
    able to evaluate the code. If it can't, the bot refuses to start and
    surfaces the compile error in the API response + action_log."""


def build_strategy_signal_fn(
    *,
    strategy_id:   int | None,
    strategy_name: str,
    df,                                # pd.DataFrame (avoid top-level import)
    leverage:      int,
    stoploss_pct:  float,              # positive % e.g. 3.0 for 3%
    take_profit_pct: float,
    pair:          str = "BTC/USDT",   # MTF analyzer: which pair to fetch HTF data for
    execution_tf:  str = "15m",        # MTF analyzer: engine TF (drops bias TFs <= this)
    overrides:     dict | None = None, # Per-bot strategy overrides (session/threshold/...)
):
    """Compile the user's strategy code into a per-bar signal_fn(df, i).

    Returns a callable with shape signal_fn(df, i) -> None | tuple, where the
    tuple is (entry, sl, tp, direction) or (entry, sl, tp, tp2, direction).

    Fail-fast: raises StrategyCompileError when:
      • strategy_id is None or no DB row found, OR
      • the row's generated_code is empty / null, OR
      • strategy_runner.evaluate_strategy raises any exception.

    There is NO legacy fallback. The fuzzy name-matched signal dict
    (_STRATEGY_SIGNALS) and _get_signal_fn helper were removed alongside
    the spot trading stack — every bot must run its own strategy code,
    period.

    Diagnostics (helpful for debugging "why isn't my strategy firing"):
      • df.attrs["strategy_class"] — class name that ran
      • df.attrs["signal_columns"] — which columns the strategy populated
      • df.attrs["class_stoploss_pct"] — class-declared SL (if any)
      • df.attrs["class_take_profit_pct"] — class-declared TP (if any)
    Attached to the returned signal_fn as `.diagnostics` dict.
    """
    if strategy_id is None:
        raise StrategyCompileError(
            f"Strategy '{strategy_name}' has no strategy_id — engine refuses to "
            "start without a DB-resolvable strategy (no more legacy name-matched "
            "fallback)."
        )

    from backend.models import SessionLocal
    from backend.models.strategy import Strategy
    from backend.services.strategy_runner import (
        evaluate_strategy, make_signal_fn_from_df,
    )
    from sqlalchemy import select as _select

    with SessionLocal() as db:
        strat = db.execute(
            _select(Strategy).where(Strategy.id == strategy_id)
        ).scalar_one_or_none()

    if strat is None:
        raise StrategyCompileError(
            f"Strategy id={strategy_id} not found in DB — refusing to start engine."
        )
    if not strat.generated_code or not strat.generated_code.strip():
        raise StrategyCompileError(
            f"Strategy '{strat.name}' (id={strategy_id}) has no generated_code. "
            "Run /api/strategy/{id}/regenerate or re-upload."
        )

    try:
        df_with_signals = evaluate_strategy(
            strat.generated_code, df,
            pair=pair, execution_tf=execution_tf,
            overrides=overrides,
        )
    except Exception as e:
        # Re-raise as StrategyCompileError so the caller can show a clean
        # message and refuse to open trades.
        raise StrategyCompileError(
            f"Strategy '{strat.name}' (id={strategy_id}) compile failed: {e}"
        ) from e

    sig_fn = make_signal_fn_from_df(
        df_with_signals, leverage, stoploss_pct, take_profit_pct,
    )
    sig_fn.diagnostics = {  # type: ignore[attr-defined]
        "path":           "strategy_runner",
        "strategy_id":    strategy_id,
        "strategy_name":  strategy_name,
        "strategy_class": df_with_signals.attrs.get("strategy_class"),
        "signal_columns": df_with_signals.attrs.get("signal_columns", []),
        "class_sl_pct":   df_with_signals.attrs.get("class_stoploss_pct"),
        "class_tp_pct":   df_with_signals.attrs.get("class_take_profit_pct"),
    }
    return sig_fn


# ─────────────────────────── position ─────────────────────────────────────

@dataclass
class Position:
    pair:         str
    direction:    str        # 'long' or 'short'
    entry:        float
    sl:           float
    tp:           float
    size:         float      # USDT stake
    opened_at:    datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    trail_lock:   Optional[float] = None
    # Unique key used in engine.positions dict (allows multiple per pair)
    trade_id:     str = field(default_factory=lambda: "")
    # DB row id — set after _persist_open_trade so close can UPDATE the same row
    db_id:        Optional[int] = None
    # result fields (filled on close)
    closed_at:    Optional[datetime] = None
    exit_price:   Optional[float]    = None
    pnl_pct:      float = 0.0
    pnl_abs:      float = 0.0
    exit_reason:  str   = ""

    def update_trail(self, current_price: float):
        """Move trailing stop when 1.5R is reached."""
        if self.direction == "long":
            excursion = current_price - self.entry
        else:
            excursion = self.entry - current_price
        half_r = abs(self.tp - self.entry) / 3 * 1.5
        if excursion >= half_r and self.trail_lock is None:
            if self.direction == "long":
                self.trail_lock = self.entry + excursion * 0.5
            else:
                self.trail_lock = self.entry - excursion * 0.5

    @property
    def effective_sl(self) -> float:
        if self.trail_lock is None:
            return self.sl
        if self.direction == "long":
            return max(self.sl, self.trail_lock)
        else:
            return min(self.sl, self.trail_lock)

    def check_exit(self, high: float, low: float) -> Optional[tuple[float, str]]:
        """Return (exit_price, reason) if this candle triggers an exit."""
        if self.direction == "long":
            if low <= self.effective_sl:
                return self.effective_sl, "stop_loss"
            if high >= self.tp:
                return self.tp, "take_profit"
        else:
            if high >= self.effective_sl:
                return self.effective_sl, "stop_loss"
            if low <= self.tp:
                return self.tp, "take_profit"
        return None

    def close(self, price: float, reason: str, ts: datetime):
        self.closed_at   = ts
        self.exit_price  = price
        self.exit_reason = reason
        if self.direction == "long":
            self.pnl_pct = (price - self.entry) / self.entry * 100
        else:
            self.pnl_pct = (self.entry - price) / self.entry * 100
        self.pnl_abs = self.size * (self.pnl_pct / 100)


# ─────────────────────────── engine ───────────────────────────────────────

class NativeTradingEngine:
    """One trading session per user (paper or live)."""

    def __init__(self, user_id: str):
        self.user_id    = user_id
        self._lock      = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_evt  = threading.Event()

        # config (set by start())
        self._strategy     = ""
        self._strategy_id: int | None = None   # DB id of the strategy record
        self._pairs: list[str] = []
        self._timeframe    = "15m"
        self._mode         = "paper"    # "paper" | "live"
        self._stoploss     = -0.03
        self._take_profit  = 0.015      # default 1.5% TP (overridden per strategy)
        self._wallet       = 1000.0
        self._risk_pct     = 0.05       # 5% of wallet per trade
        self._max_open     = 3

        # KuCoin credentials (live only)
        self._api_key   = ""
        self._api_sec   = ""
        self._api_pass  = ""

        # state
        self.balance:   float              = 1000.0
        self.positions: dict[str, Position]= {}   # trade_key → open position
        self.closed_trades: list[Position] = []
        self.ticks:     int                = 0
        self.errors:    int                = 0
        self.last_action: str              = ""
        self.started_at: Optional[datetime] = None
        self._last_prices: dict[str, float] = {}  # pair → last known live price
        # Companion dict: epoch-seconds when each _last_prices entry was
        # set. Used by FuturesEngine._get_live_price for a 15s TTL —
        # without this, paper-mode bots return a stuck price forever
        # (no WS feed to refresh it) → entry=exit=cached_value → PNL=0
        # stop_and_reverse loop visible to users in Trade History.
        self._last_prices_ts: dict[str, float] = {}

    # ── public API ──────────────────────────────────────────────────────

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def status(self) -> dict:
        with self._lock:
            return {
                "running":      self.is_running,
                "mode":         self._mode,
                "strategy":     self._strategy,
                "pairs":        self._pairs,
                "timeframe":    self._timeframe,
                "balance":      round(self.balance, 2),
                "open_trades":  len(self.positions),
                "total_trades": len(self.closed_trades),
                "ticks":        self.ticks,
                "errors":       self.errors,
                "last_action":  self.last_action,
                "started_at":   str(self.started_at) if self.started_at else None,
                "user_id":      self.user_id,
                "realized_pnl": round(sum(t.pnl_abs for t in self.closed_trades), 4),
                "unrealized_pnl": round(
                    sum(
                        p.size * (
                            (self._last_prices.get(p.pair, p.entry) - p.entry) / p.entry
                            if p.direction == "long" else
                            (p.entry - self._last_prices.get(p.pair, p.entry)) / p.entry
                        ) * getattr(p, "leverage", 1)
                        for p in self.positions.values()
                    ), 4
                ),
                "win_rate": round(
                    sum(1 for t in self.closed_trades if t.pnl_abs > 0)
                    / max(1, len(self.closed_trades)) * 100, 1
                ),
                "engine": "native_python",
                "pid": None,   # no subprocess
            }

    # ── Spot-trading methods deleted in the spot purge ──────────────
    #
    # All spot start/run/tick methods (start_paper, start_live,
    # _run_loop, _tick_continuous, _tick, _process_pair,
    # _place_live_entry, _place_live_exit, manual_entry) were removed.
    # FuturesEngine subclass implements the futures-only equivalents.

    def stop(self) -> dict:
        if not self.is_running:
            return {"stopped": True, "message": "Engine was not running"}
        self._stop_evt.set()
        self._thread.join(timeout=10)
        mode = self._mode
        return {"stopped": True, "mode": mode, "user_id": self.user_id}


class NativeTradingRegistry:
    """Process-wide registry of per-user NativeTradingEngine instances.

    Acts as a thread-safe factory. Two simultaneous requests for the
    same brand-new user cannot race to create two engines.
    """

    def __init__(self):
        self._engines: dict[str, NativeTradingEngine] = {}
        self._lock    = threading.Lock()

    def for_user(self, user_id: str) -> NativeTradingEngine:
        with self._lock:
            eng = self._engines.get(user_id)
            if eng is None:
                eng = NativeTradingEngine(user_id)
                self._engines[user_id] = eng
            return eng

    def active_users(self) -> list[str]:
        with self._lock:
            return [uid for uid, e in self._engines.items() if e.is_running]

    def stop_all(self):
        with self._lock:
            for e in self._engines.values():
                if e.is_running:
                    try:
                        e.stop()
                    except Exception:
                        pass


native_engine_registry = NativeTradingRegistry()
