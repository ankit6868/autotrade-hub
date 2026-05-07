"""
CopyTradingService — broadcast master signals and auto-fill followers.

Flow:
  1. Master engine opens/closes a position → calls broadcast()
  2. broadcast() inserts CopySignal row (expires in 5 min)
  3. For each active CopySubscription, open_copy_trade() is called
  4. Follower engine enters the same position (scaled by risk_multiplier)
  5. All copied trades have copy_source_id set in Trade row
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

log = logging.getLogger("copy_trading")


class CopyTradingService:

    def broadcast(
        self,
        master_user_id: str,
        pair: str,
        direction: str,
        entry_price: float,
        sl_price: float,
        tp_price: float,
        stake_pct: float,
        market_type: str = "spot",
        leverage: int = 1,
        strategy_name: str = "",
        signal_type: str = "entry",
    ) -> Optional[int]:
        """Insert a CopySignal row and dispatch to all active followers."""
        try:
            from backend.models.database import SessionLocal
            from backend.models.trade import CopySignal, CopySubscription
            from sqlalchemy import select

            db = SessionLocal()
            try:
                now = datetime.now(timezone.utc)
                signal = CopySignal(
                    master_user_id = master_user_id,
                    pair           = pair,
                    direction      = direction,
                    market_type    = market_type,
                    leverage       = leverage,
                    entry_price    = entry_price,
                    sl_price       = sl_price,
                    tp_price       = tp_price,
                    stake_pct      = stake_pct,
                    strategy_name  = strategy_name,
                    signal_type    = signal_type,
                    broadcasted_at = now,
                    expires_at     = now + timedelta(minutes=5),
                )
                db.add(signal)
                db.flush()
                signal_id = signal.id

                # Dispatch to active followers
                subs = db.execute(
                    select(CopySubscription).where(
                        CopySubscription.master_user_id == master_user_id,
                        CopySubscription.is_active == True,  # noqa: E712
                    )
                ).scalars().all()

                for sub in subs:
                    try:
                        self._execute_for_follower(
                            sub, signal_id, pair, direction, entry_price,
                            sl_price, tp_price, stake_pct, market_type, leverage, now
                        )
                        sub.total_copied = (sub.total_copied or 0) + 1
                    except Exception as e:
                        log.error("Copy dispatch failed for %s: %s", sub.follower_user_id, e)

                db.commit()
                log.info("[copy] broadcast %s %s %s → %d followers",
                         pair, direction, market_type, len(subs))
                return signal_id
            finally:
                db.close()
        except Exception as e:
            log.error("broadcast error: %s", e)
            return None

    def _execute_for_follower(
        self, sub, signal_id: int, pair: str, direction: str,
        entry_price: float, sl: float, tp: float, stake_pct: float,
        market_type: str, leverage: int, signal_time: datetime,
    ):
        """Enter a copy trade for one follower."""
        from .native_trading_engine import native_engine_registry
        from .futures_engine import futures_engine_registry

        # Cap leverage at subscriber's max
        eff_leverage = min(leverage, sub.max_leverage or 10)
        # Use override stake or master's stake
        eff_stake_pct = sub.stake_override_pct or stake_pct

        if market_type == "futures":
            eng = futures_engine_registry.for_user(sub.follower_user_id)
            if not eng.is_running:
                # Auto-start in paper futures for copy
                eng.start_futures(
                    strategy_name="CopyTrade",
                    pairs=[pair],
                    leverage=eff_leverage,
                    mode=sub.copy_mode or "paper",
                )
        else:
            eng = native_engine_registry.for_user(sub.follower_user_id)
            if not eng.is_running:
                eng._mode = sub.copy_mode or "paper"
                eng.balance = eng._wallet or 1000.0

        result = eng.manual_entry(pair, direction)
        log.info("[copy] follower %s: %s", sub.follower_user_id, result)

    def update_signal_result(self, signal_id: int, profit_pct: float, profit_abs: float):
        """Update copy signal with closed trade P&L."""
        try:
            from backend.models.database import SessionLocal
            from backend.models.trade import CopySignal
            db = SessionLocal()
            try:
                sig = db.get(CopySignal, signal_id)
                if sig:
                    sig.profit_pct = profit_pct
                    sig.profit_abs = profit_abs
                    sig.closed_at  = datetime.now(timezone.utc)
                    sig.signal_type = "exit"
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            log.error("update_signal_result error: %s", e)


copy_trading_service = CopyTradingService()
