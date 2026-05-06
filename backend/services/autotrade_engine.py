"""
Per-user Auto-Trade Engine
==========================
A background worker that:
  1. Periodically runs the opportunity scanner over the top-volume KuCoin
     universe,
  2. Picks the best (pair, strategy) combo above a configured min_score,
  3. Auto-spawns a Freqtrade paper (or live) bot with that strategy + pair(s),
  4. Watches the bot, auto-restarts on crash, and rotates pairs when a better
     opportunity appears while no positions are open.

Every engine instance is owned by a single user_id, reads only that user's
Config row, and drives that user's `FreqtradeManager`. Two users with
auto-trade enabled run two parallel engines — they cannot interfere with
each other's positions, keys, or strategy choices.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import select

from backend.models import SessionLocal, Config, Strategy
from backend.models.trade import Trade
from backend.services.freqtrade_manager import freqtrade_mgr
from backend.services.opportunity_scanner import scan
from backend.services.kucoin_volume import top_usdt_pairs
from backend.utils.encryption import decrypt, DecryptError

log = logging.getLogger("autotrade")


def _resolve_strategy_row(db, user_id: str, class_name: str) -> Strategy | None:
    """Pick the most-recent strategy row owned by `user_id` whose generated
    code declares `class <class_name>(`. Strategy ownership is enforced so
    one user's auto-trade engine can never deploy another user's code."""
    rows = db.execute(
        select(Strategy)
        .where(
            Strategy.user_id == user_id,
            Strategy.generated_code.like(f"%class {class_name}(%"),
        )
        .order_by(Strategy.created_at.desc())
    ).scalars().all()
    return rows[0] if rows else None


def _extract_class_name(code: str) -> str | None:
    """Return the IStrategy class name from generated strategy code."""
    for line in code.splitlines():
        stripped = line.strip()
        if stripped.startswith("class ") and "IStrategy" in stripped:
            return stripped.split("(")[0].replace("class ", "").strip()
    return None


@dataclass
class EngineState:
    running: bool = False
    last_tick: str = ""
    last_action: str = ""
    last_opportunity: dict[str, Any] | None = None
    error: str = ""
    ticks: int = 0
    deploys: int = 0
    history: list[dict[str, Any]] = field(default_factory=list)


class AutoTradeEngine:
    """Single-user auto-trade loop. One thread per user."""

    def __init__(self, user_id: str):
        self.user_id = user_id
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.state = EngineState()

    # ---- Lifecycle ---------------------------------------------------------
    def start(self) -> dict:
        if self._thread and self._thread.is_alive():
            return {"started": False, "reason": "already running", "user_id": self.user_id}
        self._stop.clear()
        self.state = EngineState(running=True)
        self._thread = threading.Thread(
            target=self._loop,
            name=f"AutoTradeEngine[{self.user_id}]",
            daemon=True,
        )
        self._thread.start()
        return {"started": True, "user_id": self.user_id}

    def stop(self) -> dict:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=10)
        self.state.running = False
        return {"stopped": True, "user_id": self.user_id}

    def status(self) -> dict:
        s = self.state
        return {
            "user_id": self.user_id,
            "running": s.running,
            "last_tick": s.last_tick,
            "last_action": s.last_action,
            "last_opportunity": s.last_opportunity,
            "error": s.error,
            "ticks": s.ticks,
            "deploys": s.deploys,
            "history": s.history[-10:],
            "bot": freqtrade_mgr.for_user(self.user_id).status,
        }

    # ---- Main loop --------------------------------------------------------
    def _loop(self):
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as e:  # noqa: BLE001
                self.state.error = f"{type(e).__name__}: {e}"
                log.exception("autotrade tick failed for %s: %s", self.user_id, e)

            interval = self._read_interval()
            for _ in range(max(1, interval)):
                if self._stop.is_set():
                    return
                time.sleep(1)

    def _read_interval(self) -> int:
        with SessionLocal() as db:
            cfg = db.execute(
                select(Config).where(Config.user_id == self.user_id).limit(1)
            ).scalar_one_or_none()
            if not cfg:
                return 600
            return int(getattr(cfg, "auto_trade_scan_interval_s", 600) or 600)

    def _append_history(self, event: str, detail: dict | None = None):
        self.state.history.append({
            "ts": datetime.utcnow().isoformat(),
            "event": event,
            "detail": detail or {},
        })

    def _deploy(self, bot, cfg, strategy_name: str, pairs: list[str], timeframe: str, mode: str, score: float):
        """Start the Freqtrade bot for this user in paper or live mode."""
        # Use configured dry_run_wallet if available, else fall back to 1000
        wallet = float(getattr(cfg, "dry_run_wallet", None) or 1000.0)
        common = dict(
            strategy_name=strategy_name,
            pairs=pairs,
            timeframe=timeframe,
            stoploss=-(cfg.default_stoploss_pct or 3.0) / 100.0,
            wallet=wallet,
            max_open_trades=cfg.max_open_trades or 3,
            max_position_pct=cfg.max_position_pct or 5.0,
            trailing_stop_pct=getattr(cfg, "trailing_stop_pct", 0.0) or 0.0,
            take_profit_pct=getattr(cfg, "take_profit_pct", 0.0) or 0.0,
            position_adjustment=bool(getattr(cfg, "position_adjustment", False)),
        )
        if mode == "live":
            if not (cfg.kucoin_key_enc and cfg.kucoin_secret_enc and cfg.kucoin_passphrase_enc):
                self.state.last_action = "live requested but no KuCoin keys"
                return None
            try:
                kucoin_key = decrypt(cfg.kucoin_key_enc, self.user_id)
                kucoin_secret = decrypt(cfg.kucoin_secret_enc, self.user_id)
                kucoin_passphrase = decrypt(cfg.kucoin_passphrase_enc, self.user_id)
            except DecryptError:
                self.state.last_action = "live deploy failed: credentials could not be decrypted (re-enter in Setup)"
                return None
            res = bot.start_live(
                **common,
                kucoin_key=kucoin_key,
                kucoin_secret=kucoin_secret,
                kucoin_passphrase=kucoin_passphrase,
            )
        else:
            res = bot.start_paper(**common)

        if res.get("started"):
            self.state.deploys += 1
            self.state.last_action = f"deployed {strategy_name} on {pairs[0]} (score {score:.1f}, {mode})"
            self._append_history("deployed", {
                "pair": pairs[0], "strategy": strategy_name,
                "score": score, "mode": mode, "pid": res.get("pid"),
            })
        else:
            self.state.last_action = f"deploy failed: {res.get('error') or res}"
            self._append_history("deploy_failed", {"error": str(res)})
        return res

    def _tick(self):
        self.state.ticks += 1
        self.state.last_tick = datetime.utcnow().isoformat()

        bot = freqtrade_mgr.for_user(self.user_id)

        with SessionLocal() as db:
            cfg = db.execute(
                select(Config).where(Config.user_id == self.user_id).limit(1)
            ).scalar_one_or_none()
            if not cfg:
                self.state.last_action = "no config"
                return
            if not getattr(cfg, "auto_trade_enabled", False):
                self.state.last_action = "disabled"
                return

            mode = getattr(cfg, "auto_trade_mode", "paper") or "paper"
            timeframe = getattr(cfg, "auto_trade_timeframe", "15m") or "15m"

            # Don't rotate while this user has open positions.
            if bot.is_running:
                open_count = db.execute(
                    select(Trade).where(
                        Trade.user_id == self.user_id,
                        Trade.status == "open",
                        Trade.mode == mode,
                    )
                ).scalars().all()
                if open_count:
                    self.state.last_action = (
                        f"bot running, {len(open_count)} open trade(s) — no rotation"
                    )
                    return

            pinned_id = getattr(cfg, "auto_trade_strategy_id", None)
            pinned_pairs_raw = getattr(cfg, "auto_trade_pairs", None)

            # ── PINNED STRATEGY MODE ────────────────────────────────────────
            if pinned_id:
                strat_row = db.execute(
                    select(Strategy).where(
                        Strategy.id == pinned_id,
                        Strategy.user_id == self.user_id,
                    )
                ).scalar_one_or_none()
                if not strat_row:
                    self.state.last_action = f"pinned strategy #{pinned_id} not found"
                    return

                strategy_name = _extract_class_name(strat_row.generated_code) or f"strategy_{strat_row.id}"

                if pinned_pairs_raw:
                    pairs = [p.strip() for p in pinned_pairs_raw.split(",") if p.strip()]
                else:
                    universe = top_usdt_pairs(n=10) or ["BTC/USDT"]
                    pairs = universe[:3]

                self.state.last_opportunity = {
                    "pair": pairs[0],
                    "strategy": strategy_name,
                    "overall_score": 100.0,
                    "action": "pinned",
                    "timeframe": timeframe,
                }

                if bot.is_running and bot._strategy == strategy_name:
                    self.state.last_action = f"pinned {strategy_name} already running on {pairs[0]}"
                    return

                if bot.is_running:
                    bot.stop()
                    self._append_history("stopped_prev", {"strategy": bot._strategy})

                self._deploy(bot, cfg, strategy_name, pairs, timeframe, mode, 100.0)
                return

            # ── AUTO-SELECT MODE ────────────────────────────────────────────
            min_score = float(getattr(cfg, "auto_trade_min_score", 70.0) or 70.0)

            if pinned_pairs_raw:
                universe = [p.strip() for p in pinned_pairs_raw.split(",") if p.strip()]
            else:
                universe = top_usdt_pairs(n=50)

            if not universe:
                self.state.last_action = "no volume data"
                return

            result = scan(db, pairs=universe, timeframe=timeframe, top_n=5, min_score=min_score)
            opps = result.get("opportunities", [])
            if not opps:
                self.state.last_action = f"no opps >= {min_score}"
                return

            best = opps[0]
            self.state.last_opportunity = {
                "pair": best["pair"],
                "strategy": best["strategy"],
                "overall_score": best["overall_score"],
                "action": best.get("recommendation", ""),
                "timeframe": timeframe,
            }

            strat_row = _resolve_strategy_row(db, self.user_id, best["strategy"])
            if not strat_row:
                self.state.last_action = f"no DB strategy for {best['strategy']}"
                return

            if bot.is_running and bot._strategy == best["strategy"]:
                self.state.last_action = "already on best setup"
                return

            if bot.is_running:
                bot.stop()
                self._append_history("stopped_prev", {"strategy": bot._strategy})

            self._deploy(bot, cfg, best["strategy"], [best["pair"]], timeframe, mode, best["overall_score"])


class AutoTradeRegistry:
    """Process-wide map of `user_id -> AutoTradeEngine`. Mirrors the freqtrade
    manager registry: one engine thread per user, started on demand or at
    application startup for every user with `auto_trade_enabled=True`."""

    def __init__(self) -> None:
        self._engines: dict[str, AutoTradeEngine] = {}
        self._lock = threading.Lock()

    def for_user(self, user_id: str) -> AutoTradeEngine:
        with self._lock:
            eng = self._engines.get(user_id)
            if eng is None:
                eng = AutoTradeEngine(user_id)
                self._engines[user_id] = eng
            return eng

    def active_users(self) -> list[str]:
        with self._lock:
            return [uid for uid, e in self._engines.items() if e.state.running]

    def stop_all(self) -> None:
        with self._lock:
            for e in self._engines.values():
                if e.state.running:
                    try:
                        e.stop()
                    except Exception:
                        pass


autotrade_engine = AutoTradeRegistry()
