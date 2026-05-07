"""
MultiStrategyManager — run multiple strategies simultaneously per user.

Each StrategyInstance DB row maps to one engine thread (spot or futures).
Engines are keyed by `engine_key` = "user_id:instance_id".

On container startup: call MultiStrategyManager.resume_all(db) to
re-launch all instances that were running before the restart.
"""
from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("multi_strategy")


class MultiStrategyManager:
    """Process-wide registry of all running strategy instances."""

    def __init__(self):
        self._engines: dict[str, object] = {}   # engine_key → engine instance
        self._lock = threading.Lock()

    def _make_engine(self, row) -> object:
        """Create the correct engine type for a StrategyInstance row."""
        if getattr(row, "market_type", "spot") == "futures":
            from .futures_engine import FuturesEngine
            return FuturesEngine(row.user_id)
        else:
            from .native_trading_engine import NativeTradingEngine
            return NativeTradingEngine(row.user_id)

    def start_instance(self, row, kucoin_key: str = "", kucoin_secret: str = "",
                       kucoin_passphrase: str = "") -> dict:
        """Start a strategy instance from its DB row."""
        key = row.engine_key or f"{row.user_id}:{row.id}"
        with self._lock:
            existing = self._engines.get(key)
            if existing and existing.is_running:
                return {"error": f"Instance {key} already running"}
            eng = self._make_engine(row)
            self._engines[key] = eng

        pairs = [p.strip() for p in (row.pairs or "BTC/USDT").split(",") if p.strip()]

        if getattr(row, "market_type", "spot") == "futures":
            result = eng.start_futures(
                strategy_name  = row.strategy_name,
                pairs          = pairs,
                leverage       = getattr(row, "leverage", 10),
                mode           = row.mode,
                timeframe      = row.timeframe or "15m",
                stoploss       = row.stoploss or -0.03,
                wallet         = row.wallet or 1000.0,
                max_position_pct = row.risk_pct or 5.0,
                take_profit_pct  = getattr(row, "takeprofit", 0) or 1.5,
                kucoin_key     = kucoin_key,
                kucoin_secret  = kucoin_secret,
                kucoin_passphrase = kucoin_passphrase,
                strategy_id    = row.strategy_id,
            )
        else:
            if row.mode == "live" and kucoin_key:
                result = eng.start_live(
                    strategy_name  = row.strategy_name,
                    pairs          = pairs,
                    timeframe      = row.timeframe or "15m",
                    stoploss       = row.stoploss or -0.03,
                    kucoin_key     = kucoin_key,
                    kucoin_secret  = kucoin_secret,
                    kucoin_passphrase = kucoin_passphrase,
                    wallet         = row.wallet or 1000.0,
                    strategy_id    = row.strategy_id,
                )
            else:
                result = eng.start_paper(
                    strategy_name  = row.strategy_name,
                    pairs          = pairs,
                    timeframe      = row.timeframe or "15m",
                    stoploss       = row.stoploss or -0.03,
                    wallet         = row.wallet or 1000.0,
                    max_position_pct = row.risk_pct or 5.0,
                    take_profit_pct  = getattr(row, "takeprofit", 0) or 1.5,
                    strategy_id    = row.strategy_id,
                )
        return result

    def stop_instance(self, engine_key: str) -> dict:
        with self._lock:
            eng = self._engines.get(engine_key)
        if eng is None:
            return {"error": "Instance not found"}
        return eng.stop()

    def get_engine(self, engine_key: str) -> Optional[object]:
        with self._lock:
            return self._engines.get(engine_key)

    def all_status(self) -> list[dict]:
        with self._lock:
            engines = list(self._engines.items())
        result = []
        for key, eng in engines:
            s = eng.status()
            s["engine_key"] = key
            result.append(s)
        return result

    def resume_all(self, db) -> int:
        """Re-launch all StrategyInstance rows that were running. Returns count."""
        try:
            from sqlalchemy import select
            from backend.models.trade import StrategyInstance
            from backend.models.config import Config
            from backend.utils.encryption import decrypt, DecryptError

            rows = db.execute(
                select(StrategyInstance).where(StrategyInstance.is_running == True)  # noqa: E712
            ).scalars().all()

            count = 0
            for row in rows:
                # Get credentials for this user if live mode
                kk = ks = kp = ""
                if row.mode == "live":
                    cfg = db.execute(
                        select(Config).where(Config.user_id == row.user_id).limit(1)
                    ).scalar_one_or_none()
                    if cfg:
                        try:
                            kk = decrypt(cfg.kucoin_key_enc or "", row.user_id)
                            ks = decrypt(cfg.kucoin_secret_enc or "", row.user_id)
                            kp = decrypt(cfg.kucoin_passphrase_enc or "", row.user_id)
                        except DecryptError:
                            log.warning("Skipping instance %s — decrypt error", row.engine_key)
                            continue
                try:
                    self.start_instance(row, kk, ks, kp)
                    count += 1
                except Exception as e:
                    log.error("Failed to resume instance %s: %s", row.engine_key, e)
            return count
        except Exception as e:
            log.error("resume_all error: %s", e)
            return 0


multi_strategy_manager = MultiStrategyManager()
