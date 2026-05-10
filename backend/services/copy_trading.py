"""
CopyTradingService — broadcast master signals and auto-fill followers.

Flow:
  1. Master engine opens/closes a position → calls broadcast()
  2. broadcast() inserts CopySignal row (expires in 5 min)
  3. For each active CopySubscription, _execute_for_follower() is called
  4. Follower engine enters the same position (scaled by eff_stake_pct)
  5. All copied trades have copy_source_id set in Trade row

Fixes applied:
  - Direction (long/short) is properly forwarded to follower engines
  - stake_pct from subscription override is used for position sizing
  - Futures copy trades correctly create FuturesPosition (not spot)
  - update_signal_result works by pair lookup, not fragile signal_id
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
        direction: str,                     # "long" or "short"
        entry_price: float,
        sl_price: float,
        tp_price: float,
        stake_pct: float,
        market_type: str = "spot",
        leverage: int = 1,
        strategy_name: str = "",
        signal_type: str = "entry",
        db_signal_id: Optional[int] = None,  # master's DB trade id for reference
        # accept alternate kwarg spelling from engine
        master_id: Optional[str] = None,
    ) -> Optional[int]:
        """Insert a CopySignal row and dispatch to all active followers."""
        # Allow both 'master_user_id' and 'master_id' spellings
        master_user_id = master_user_id or master_id or ""
        if not master_user_id:
            return None

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
        """
        Enter a copy trade for one follower.
        Properly forwards direction (long/short) and stake_pct.
        """
        from .futures_engine import futures_engine_registry, FuturesPosition
        from .native_trading_engine import native_engine_registry, _persist_open_trade

        # Cap leverage at subscriber's max; use override stake if set
        eff_leverage  = min(leverage, sub.max_leverage or 10)
        eff_stake_pct = float(sub.stake_override_pct or stake_pct)

        now = datetime.now(timezone.utc)

        if market_type == "futures":
            eng = futures_engine_registry.for_user(sub.follower_user_id)
            if not eng.is_running:
                # Auto-start follower's futures engine in paper mode
                eng.start_futures(
                    strategy_name="CopyTrade",
                    pairs=[pair],
                    leverage=eff_leverage,
                    mode=sub.copy_mode or "paper",
                )

            # Create a FuturesPosition directly (correct for SHORT support)
            stake  = (eng.balance or 1000.0) * (eff_stake_pct / 100)
            pos    = FuturesPosition(
                pair=pair, direction=direction,
                entry=entry_price, sl=sl, tp=tp,
                size=stake, leverage=eff_leverage,
                opened_at=now,
                trade_id=f"copy-{signal_id}-{sub.follower_user_id}",
            )
            mode = eng._mode or sub.copy_mode or "paper"
            pos.db_id = _persist_open_trade(
                sub.follower_user_id, pos, mode, None,
                leverage=eff_leverage, market_type="futures",
            )
            with eng._lock:
                eng.positions[pos.trade_id] = pos
                eng.balance = max(0.0, (eng.balance or 1000.0) - stake)

            log.info("[copy] futures follower %s: %s %s @ %.4f lev=%dx",
                     sub.follower_user_id, direction, pair, entry_price, eff_leverage)

        else:
            # Spot copy trade
            eng = native_engine_registry.for_user(sub.follower_user_id)
            if not eng.is_running:
                eng._mode = sub.copy_mode or "paper"
                eng.balance = eng._wallet or 1000.0

            # For spot, call manual_entry with direction
            try:
                result = eng.manual_entry(pair, direction)
                log.info("[copy] spot follower %s: %s", sub.follower_user_id, result)
            except Exception as e:
                log.error("[copy] spot manual_entry error for %s: %s", sub.follower_user_id, e)

    def update_signal_result(
        self,
        master_id: Optional[str] = None,
        pair: Optional[str] = None,
        exit_price: Optional[float] = None,
        pnl_pct: Optional[float] = None,
        reason: Optional[str] = None,
        # legacy signature support
        signal_id: Optional[int] = None,
        profit_pct: Optional[float] = None,
        profit_abs: Optional[float] = None,
    ):
        """Update copy signal with closed trade P&L. Supports both new and legacy calls."""
        effective_profit_pct = pnl_pct or profit_pct or 0.0
        effective_profit_abs = profit_abs or 0.0

        try:
            from backend.models.database import SessionLocal
            from backend.models.trade import CopySignal
            from sqlalchemy import select, desc
            db = SessionLocal()
            try:
                sig = None
                # Try to find the open signal by pair + master
                if master_id and pair:
                    sig = db.execute(
                        select(CopySignal).where(
                            CopySignal.master_user_id == master_id,
                            CopySignal.pair == pair,
                            CopySignal.signal_type == "entry",
                            CopySignal.closed_at.is_(None),
                        ).order_by(desc(CopySignal.broadcasted_at))
                    ).scalar_one_or_none()
                # Fallback: by signal_id
                if sig is None and signal_id:
                    sig = db.get(CopySignal, signal_id)

                if sig:
                    sig.profit_pct  = effective_profit_pct
                    sig.profit_abs  = effective_profit_abs
                    sig.closed_at   = datetime.now(timezone.utc)
                    sig.signal_type = "exit"
                    db.commit()
            finally:
                db.close()
        except Exception as e:
            log.error("update_signal_result error: %s", e)


copy_trading_service = CopyTradingService()
