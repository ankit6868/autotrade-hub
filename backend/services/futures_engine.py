"""
FuturesEngine — Paper + Live futures trading with leverage.

Extends NativeTradingEngine with:
  • Leverage-amplified P&L calculation
  • Liquidation price tracking and forced-liquidation logic
  • Paper mode: fully simulated (no real orders, realistic leverage math)
  • Live mode: KuCoin Futures REST API (api-futures.kucoin.com)
  • Short positions supported (direction='short')
  • Volume-based signal filtering

Liquidation model (simplified, conservative):
  Long:  entry × (1 − 1/leverage + 0.005)
  Short: entry × (1 + 1/leverage − 0.005)
  The 0.5% maintenance margin prevents liquidation exactly at bankruptcy price.
"""
from __future__ import annotations

import logging
import time
import urllib.parse
import urllib.request
import json
from datetime import datetime, timezone
from typing import Optional

from .native_trading_engine import (
    NativeTradingEngine, Position, _STRATEGY_SIGNALS,
    _persist_open_trade, _persist_closed_trade, _get_signal_fn,
    build_strategy_signal_fn,
    _fetch_candles, _build_df, KUCOIN_BASE, TF_KUCOIN,
)
from . import risk_engine

log = logging.getLogger("futures_engine")

KUCOIN_FUTURES_BASE = "https://api-futures.kucoin.com"
MAINTENANCE_MARGIN  = 0.005   # 0.5% — standard KuCoin simplified


def _calc_liquidation_price(entry: float, direction: str, leverage: int) -> float:
    """Return the price at which this leveraged position gets liquidated."""
    if direction == "long":
        return round(entry * (1 - 1 / leverage + MAINTENANCE_MARGIN), 4)
    else:
        return round(entry * (1 + 1 / leverage - MAINTENANCE_MARGIN), 4)


class FuturesPosition(Position):
    """Position with leverage and liquidation tracking."""

    def __init__(self, *args, leverage: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.leverage           = leverage
        self.liquidation_price  = _calc_liquidation_price(self.entry, self.direction, leverage)
        self._market_type       = "futures"

    def check_liquidation(self, price: float) -> bool:
        """Return True if current price has crossed the liquidation level."""
        if self.direction == "long":
            return price <= self.liquidation_price
        else:
            return price >= self.liquidation_price

    def close(self, price: float, reason: str, ts: datetime):
        """Override: multiply raw pnl_pct by leverage."""
        super().close(price, reason, ts)
        self.pnl_pct *= self.leverage
        self.pnl_abs  = self.size * (self.pnl_pct / 100)


class PendingOrder:
    """A pending limit/stop order waiting to be filled."""
    __slots__ = (
        "order_id", "symbol", "side", "order_type", "size", "price",
        "stop_price", "leverage", "margin_mode", "tp_price", "sl_price",
        "hidden", "post_only", "reduce_only", "time_in_force",
        "created_at", "db_id", "cost_usdt",
    )

    def __init__(self, **kwargs):
        for k in self.__slots__:
            setattr(self, k, kwargs.get(k))
        if self.created_at is None:
            self.created_at = datetime.now(timezone.utc)

    def should_fill(self, current_price: float) -> bool:
        if self.order_type == "limit":
            if self.side == "buy" and current_price <= self.price:
                return True
            if self.side == "sell" and current_price >= self.price:
                return True
        elif self.order_type in ("stop", "stop_limit"):
            if self.stop_price is None:
                return False
            if self.side == "buy" and current_price >= self.stop_price:
                return True
            if self.side == "sell" and current_price <= self.stop_price:
                return True
        return False


class FuturesEngine(NativeTradingEngine):
    """
    Futures trading engine — paper or live, with leverage.

    Usage:
        eng = FuturesEngine(user_id)
        eng.start_futures(strategy_name, pairs, leverage=10, mode='paper', ...)
    """

    MAX_ACTION_LOG = 50

    def __init__(self, user_id: str):
        super().__init__(user_id)
        self._leverage      = 1
        self._market_type   = "futures"
        self._margin_mode   = "cross"
        self._pending_orders: dict[str, PendingOrder] = {}
        self._per_symbol_leverage: dict[str, int] = {}
        self._per_symbol_margin: dict[str, str] = {}
        self._order_counter = 0
        self.action_log: list[dict] = []
        self.signal_count: int = 0
        self._winding_down: bool = False

    def wind_down(self):
        """Stop opening new positions but keep managing existing ones until all are closed."""
        self._winding_down = True
        self._log_action("wind_down", "Bot entering wind-down mode — managing open positions to exit, no new entries")
        log.info("[%s] Engine entering wind-down mode with %d open positions",
                 self.user_id, len(self.positions))

    @property
    def is_winding_down(self) -> bool:
        return self._winding_down

    def _log_action(self, action_type: str, detail: str, **extra):
        entry = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "type": action_type,
            "detail": detail,
            **extra,
        }
        self.action_log.append(entry)
        if len(self.action_log) > self.MAX_ACTION_LOG:
            self.action_log = self.action_log[-self.MAX_ACTION_LOG:]

    # ── Start ───────────────────────────────────────────────────────────

    def start_futures(
        self,
        strategy_name: str,
        pairs: list[str],
        leverage: int = 10,
        mode: str = "paper",
        timeframe: str = "15m",
        stoploss: float = -0.03,
        wallet: float = 1000.0,
        max_open_trades: int = 3,
        max_position_pct: float = 5.0,
        take_profit_pct: float = 1.5,
        kucoin_key: str = "",
        kucoin_secret: str = "",
        kucoin_passphrase: str = "",
        strategy_id: int | None = None,
        **_kwargs,
    ) -> dict:
        # Always do a clean stop before (re)starting.
        # This prevents the "already running" deadlock when the UI shows Stopped
        # but an old thread is still alive from a previous session.
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None
        self._stop_evt.clear()
        self._strategy     = strategy_name
        self._strategy_id  = strategy_id
        self._pairs        = pairs
        self._leverage     = max(1, min(20, int(leverage)))
        self._timeframe    = timeframe
        self._stoploss     = stoploss
        self._take_profit  = take_profit_pct / 100.0
        self._wallet       = wallet
        self._mode         = mode
        self._market_type  = "futures"
        self._max_open     = max_open_trades
        self._max_per_pair = 1   # ← STRICT: only 1 open position per pair at a time
        self._risk_pct     = max_position_pct / 100.0
        self._api_key      = kucoin_key
        self._api_sec      = kucoin_secret
        self._api_pass     = kucoin_passphrase
        self.balance       = wallet
        self.positions     = {}
        self.closed_trades = []
        self.ticks = self.errors = 0
        self._stop_evt.clear()
        self.started_at = datetime.now(timezone.utc)

        # ── Phase 2 sanity check: verify the strategy code compiles before
        # we start the engine thread, so the user gets immediate feedback in
        # the action log if their strategy is broken. Without this, a broken
        # strategy silently falls back to a name-matched built-in and the
        # bot looks like it's working — exactly the hidden bug we're fixing.
        if strategy_id is not None:
            try:
                from backend.models import SessionLocal
                from backend.models.strategy import Strategy
                from backend.services.strategy_runner import evaluate_strategy
                from sqlalchemy import select as _select
                import pandas as _pd
                with SessionLocal() as _db:
                    _strat = _db.execute(
                        _select(Strategy).where(Strategy.id == strategy_id)
                    ).scalar_one_or_none()
                if _strat and _strat.generated_code:
                    # Compile against a tiny dummy dataframe to surface
                    # syntax/import errors WITHOUT needing real candles.
                    _dummy = _pd.DataFrame({
                        "date":  _pd.date_range("2024-01-01", periods=300, freq="15min", tz="UTC"),
                        "open":  [100.0] * 300, "high": [101.0] * 300,
                        "low":   [99.0]  * 300, "close": [100.5] * 300,
                        "vol":   [1000.0] * 300,
                    })
                    _result = evaluate_strategy(_strat.generated_code, _dummy)
                    self._log_action(
                        "strategy_compiled",
                        f"Strategy '{_result.attrs.get('strategy_class', strategy_name)}' compiled OK on engine start",
                        strategy_class=_result.attrs.get("strategy_class"),
                        signal_columns=_result.attrs.get("signal_columns", []),
                    )
                    log.info(
                        "[%s] strategy %s compiled OK (class=%s, signals=%s)",
                        self.user_id, strategy_name,
                        _result.attrs.get("strategy_class"),
                        _result.attrs.get("signal_columns", []),
                    )
            except Exception as _compile_exc:
                self._log_action(
                    "strategy_compile_failed",
                    f"Strategy '{strategy_name}' failed to compile: {_compile_exc} — bot will use the legacy name-matched fallback. Edit the strategy code in Strategy Editor to fix.",
                    error=str(_compile_exc),
                )
                log.warning(
                    "[%s] strategy %s failed to compile on start: %s — will fall back at signal time",
                    self.user_id, strategy_name, _compile_exc,
                )

        import threading
        self._thread = threading.Thread(
            target=self._run_loop, daemon=True,
            name=f"futures-{self.user_id}-{mode}"
        )
        # NOTE: is_running is a @property in the base class — derived from
        # self._thread.is_alive(), so we must start the thread first.
        self._thread.start()
        log.info("[%s] Futures engine started lev=%sx mode=%s strategy=%s",
                 self.user_id, self._leverage, mode, strategy_name)
        return {"started": True, "mode": mode, "market_type": "futures",
                "leverage": self._leverage, "strategy": strategy_name}

    # ── Tick override — adds liquidation check ──────────────────────────

    def _tick_continuous(self, signal_fn, seen_signal: dict,
                         last_signal_ts: dict | None = None,
                         signal_interval: float = 60.0):
        """Futures tick: liquidation + TP/SL every 5s, signals every 60s.

        Phase 2 changes:
          • The `signal_fn` argument passed in by the base class is IGNORED —
            we build a per-pair signal_fn from the user's strategy code via
            build_strategy_signal_fn (same path the backtester uses). This
            replaces the old fuzzy-name-matched fallback chain that quietly
            ran the wrong strategy code for anything outside the 8 hardcoded
            built-ins.
          • Signal-edge detection: stored in seen_signal[pair] = direction
            of last fired signal. A new signal only fires when the bar's
            edge changes (None → long, None → short, long → short, short → long).
          • Stop-and-reverse: when an opposite-direction signal fires while
            a position is open on the same pair, we close the old position
            and open the new one in the next tick. Matches the user's
            preference picked in the earlier architecture review.

        Phase 4 change:
          • After we have (entry, strategy_sl, strategy_tp, direction) from
            the strategy, we pass it through risk_engine.compute_tp_sl which
            applies timeframe-aware ATR multipliers if the strategy didn't
            provide structural levels, and enforces a min-RR check. Rejected
            signals are logged with reason but do NOT open a position.
        """
        import time as _time
        now_epoch = _time.time()

        for pair in self._pairs:
            if self._stop_evt.is_set():
                return

            live_price = self._get_live_price(pair)
            if live_price is None:
                continue

            # Check pending limit/stop orders for this pair
            if self._pending_orders:
                self._check_pending_orders(pair, live_price)

            now = datetime.now(timezone.utc)

            with self._lock:
                self.ticks += 1

                # ── Manage ALL open futures positions ───────────────────
                pair_keys = [k for k, p in self.positions.items() if p.pair == pair]
                for trade_key in pair_keys:
                    pos = self.positions.get(trade_key)
                    if pos is None:
                        continue

                    # Liquidation check (futures only) — instant, every tick
                    if isinstance(pos, FuturesPosition):
                        if pos.check_liquidation(live_price):
                            pos.close(live_price, "liquidated", now)
                            self.balance += pos.pnl_abs
                            self.closed_trades.append(pos)
                            del self.positions[trade_key]
                            # Reset to None (Phase 2 edge detection stores direction).
                            seen_signal[pair] = None
                            self.last_action = (
                                f"LIQUIDATED {pair} @ {live_price:.4f} "
                                f"liq={pos.liquidation_price:.4f} P&L={pos.pnl_abs:+.2f}"
                            )
                            self._log_action("liquidated", self.last_action,
                                pair=pair, price=live_price, pnl=pos.pnl_abs,
                                direction=pos.direction)
                            log.warning("[%s] %s", self.user_id, self.last_action)
                            _persist_closed_trade(self.user_id, pos, self._mode,
                                                  self._strategy_id, pos.db_id)
                            continue

                    # TP/SL exit — checked every tick (5 s when positions open)
                    pos.update_trail(live_price)
                    exit_info = pos.check_exit(live_price, live_price)
                    if exit_info:
                        exit_price, reason = exit_info
                        pos.close(exit_price, reason, now)
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        del self.positions[trade_key]
                        # Reset to None (Phase 2 edge detection stores direction).
                        seen_signal[pair] = None
                        self.last_action = (
                            f"CLOSED {pair} @ {exit_price:.4f} ({reason}) "
                            f"P&L={pos.pnl_abs:+.2f} lev={getattr(pos,'leverage',1)}x"
                        )
                        self._log_action("closed", self.last_action,
                            pair=pair, price=exit_price, pnl=pos.pnl_abs,
                            direction=pos.direction, reason=reason,
                            entry_price=pos.entry)
                        log.info("[%s] %s", self.user_id, self.last_action)
                        _persist_closed_trade(self.user_id, pos, self._mode,
                                              self._strategy_id, pos.db_id)
                        if self._mode == "live":
                            self._place_live_exit(pair, pos, exit_price)
                        # ── Notify copy followers of close ────────────────
                        try:
                            from backend.services.copy_trading import copy_trading_service
                            copy_trading_service.update_signal_result(
                                master_id=self.user_id,
                                pair=pair,
                                exit_price=exit_price,
                                pnl_pct=pos.pnl_pct,
                                reason=reason,
                            )
                        except Exception:
                            pass

                # Wind-down: no new entries, auto-stop when all positions closed
                if self._winding_down:
                    if len(self.positions) == 0:
                        self._log_action("wind_down_complete", "All positions closed — stopping engine")
                        log.info("[%s] Wind-down complete, all positions closed", self.user_id)
                        self._stop_evt.set()
                    continue

                # Position limit guards.
                #
                # NOTE on stop-and-reverse: we deliberately DON'T skip the
                # signal scan just because a position exists on this pair.
                # If we did, an opposite-side signal could never be detected
                # → the bot would be stuck long forever once it opened a
                # long, even when the strategy flipped bearish. The new
                # logic only blocks the per-pair guard for SAME-direction
                # repeats; opposite signals fall through to stop-and-reverse
                # in the signal-scan block below.
                if len(self.positions) >= self._max_open:
                    # Global max-open across all pairs still applies — if
                    # we're at the cap and the pair has no existing position,
                    # we genuinely can't add a new one even on a flip.
                    existing_for_pair = sum(
                        1 for p in self.positions.values() if p.pair == pair
                    )
                    if existing_for_pair == 0:
                        continue
                    # else: there's already a position on this pair, fall
                    # through so a flip can stop-and-reverse it in place.

            # ── Signal scan — only when interval has elapsed ────────────
            if last_signal_ts is not None:
                elapsed = now_epoch - last_signal_ts.get(pair, 0.0)
                if elapsed < signal_interval:
                    continue   # wait for next 60 s window

            try:
                candles = _fetch_candles(pair.replace("/", "-"), self._timeframe)
            except Exception as e:
                log.warning("[%s] candle fetch %s: %s", self.user_id, pair, e)
                continue

            if not candles:
                continue
            df = _build_df(candles)
            if df.empty:
                continue

            # ── Phase 2: build signal_fn from user's strategy code ──────────
            # We rebuild every signal scan because the dataframe changes
            # (new closed candle). evaluate_strategy is fast (<200ms for
            # 200 candles), so doing this per-scan is fine.
            user_signal_fn = build_strategy_signal_fn(
                strategy_id     = self._strategy_id,
                strategy_name   = self._strategy,
                df              = df,
                leverage        = self._leverage,
                stoploss_pct    = abs(self._stoploss) * 100.0,
                take_profit_pct = self._take_profit * 100.0,
            )

            # Latest bar index — strategy_runner's signal_fn uses edge
            # detection (compares enter_long[i] vs enter_long[i-1]) so
            # we get TradingView-parity "fire on the bar where the
            # condition transitions from False → True" behaviour.
            last_idx = len(df) - 1
            try:
                sig = user_signal_fn(df, last_idx)
            except TypeError:
                # Legacy signal_fn that takes only df, no i (defensive).
                try:
                    sig = user_signal_fn(df)
                except Exception:
                    sig = None
            except Exception as e:
                log.warning("[%s] signal_fn raised for %s: %s",
                            self.user_id, pair, e)
                sig = None

            if sig is None:
                # No signal this bar — clear the edge-tracking flag so the
                # NEXT distinct signal (after a flat bar) fires cleanly.
                seen_signal[pair] = None
                continue

            # Unpack 4-tuple or 5-tuple (5-tuple = ARM with TP2).
            tp2_s: float | None = None
            if len(sig) == 5:
                entry_s, sl_s, tp_s, tp2_s, direction = sig
            else:
                entry_s, sl_s, tp_s, direction = sig

            # ── Edge detection + stop-and-reverse ─────────────────────────
            # seen_signal[pair] now holds the DIRECTION of the last fired
            # signal (None | 'long' | 'short'). Skip if the same direction
            # already fired and we still hold the position.
            prev_dir = seen_signal.get(pair)
            if prev_dir == direction:
                # Same-direction repeat — don't fire again until the signal
                # flips or the position closes (the close path resets to None).
                continue

            self.signal_count += 1

            # If we hold an opposite-direction position on this pair AND
            # the user picked "1 per pair + stop-and-reverse", close the
            # old position now so the new one can open below.
            with self._lock:
                opposite_keys = [
                    k for k, p in self.positions.items()
                    if p.pair == pair and p.direction != direction
                ]
            for trade_key in opposite_keys:
                with self._lock:
                    old_pos = self.positions.get(trade_key)
                    if old_pos is None:
                        continue
                    exit_price = live_price
                    old_pos.close(exit_price, "stop_and_reverse", now)
                    self.balance += old_pos.pnl_abs
                    self.closed_trades.append(old_pos)
                    del self.positions[trade_key]
                    self.last_action = (
                        f"REVERSED {pair} {old_pos.direction} @ {exit_price:.4f} "
                        f"(opposite signal) P&L={old_pos.pnl_abs:+.2f}"
                    )
                    self._log_action("stop_and_reverse", self.last_action,
                        pair=pair, price=exit_price, pnl=old_pos.pnl_abs,
                        old_direction=old_pos.direction, new_direction=direction)
                    log.info("[%s] %s", self.user_id, self.last_action)
                    _persist_closed_trade(self.user_id, old_pos, self._mode,
                                          self._strategy_id, old_pos.db_id)
                    if self._mode == "live":
                        self._place_live_exit(pair, old_pos, exit_price)

            entry = live_price

            # ── Phase 4: timeframe-aware risk plan ──────────────────────────
            # Pass the strategy's structural SL/TP through risk_engine,
            # which (a) honours them if they validate, (b) falls back to
            # ATR×per-TF-multiplier defaults if not, and (c) enforces a
            # min-RR gate per the spec.
            plan = risk_engine.compute_tp_sl(
                entry        = entry,
                direction    = direction,
                df           = df,
                timeframe    = self._timeframe,
                strategy_sl  = sl_s,
                strategy_tp  = tp_s,
                strategy_tp2 = tp2_s,
            )

            if not plan.valid:
                # Log loudly so the UI's "last_action" surfaces it. Don't
                # update seen_signal so the next bar gets a fresh chance.
                rejection = risk_engine.format_plan_for_log(plan, pair=pair)
                self.last_action = rejection
                self._log_action("signal_rejected", rejection,
                    pair=pair, direction=direction, reason=plan.rejected_reason,
                    timeframe=self._timeframe, atr=plan.atr)
                log.info("[%s] %s", self.user_id, rejection)
                continue

            sl, tp = plan.sl, plan.tp

            with self._lock:
                stake = self.balance * self._risk_pct
                if stake < 1.0 or stake > self.balance:
                    continue

                trade_key = f"{pair}#{int(now.timestamp())}"
                pos = FuturesPosition(
                    pair=pair, direction=direction,
                    entry=entry, sl=sl, tp=tp, size=stake,
                    opened_at=now,
                    trade_id=trade_key,
                    leverage=self._leverage,
                )
                pos.db_id = _persist_open_trade(
                    self.user_id, pos, self._mode, self._strategy_id,
                    leverage=self._leverage, market_type="futures",
                )
                self.positions[trade_key] = pos
                self.balance -= stake
                # Track the DIRECTION (not just bool) so edge detection on
                # the next signal scan knows whether this is a same-side
                # repeat or a genuine flip.
                seen_signal[pair] = direction
                self.last_action = (
                    f"OPENED futures {direction} {pair} @ {entry:.4f} "
                    f"{self._leverage}x liq={pos.liquidation_price:.4f} "
                    f"SL={sl:.4f} TP={tp:.4f} RR={plan.rr:.2f} [{plan.source}/{self._timeframe}]"
                )
                self._log_action("opened", self.last_action,
                    pair=pair, price=entry, direction=direction,
                    leverage=self._leverage, sl=sl, tp=tp,
                    liquidation=pos.liquidation_price, stake=stake,
                    rr=plan.rr, atr=plan.atr, risk_source=plan.source,
                    timeframe=self._timeframe,
                    diagnostics=getattr(user_signal_fn, "diagnostics", None))
                log.info("[%s] %s", self.user_id, self.last_action)
                if self._mode == "live":
                    self._place_live_entry(pair, pos)

                # ── Broadcast to copy-trading followers ─────────────────
                try:
                    from backend.services.copy_trading import copy_trading_service
                    copy_trading_service.broadcast(
                        master_id=self.user_id,
                        signal_type="entry",
                        pair=pair,
                        direction=direction,
                        entry_price=entry,
                        sl_price=sl,
                        tp_price=tp,
                        leverage=self._leverage,
                        market_type="futures",
                        stake_pct=self._risk_pct * 100,
                        db_signal_id=pos.db_id,
                    )
                except Exception as _cte:
                    log.debug("[%s] copy-broadcast skipped: %s", self.user_id, _cte)

    # ── Live order placement via KuCoin Futures API ─────────────────────

    def _place_live_entry(self, pair: str, pos) -> None:
        """Place a real futures order via KuCoin Lead Trading API."""
        if self._mode != "live" or not self._api_key:
            return
        try:
            from .native_trading_engine import _kucoin_post_signed
            from .kucoin_futures_client import normalize_futures_symbol
            symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
            side   = "buy" if pos.direction == "long" else "sell"
            position_side = "LONG" if pos.direction == "long" else "SHORT"
            contract_size  = pos.size * self._leverage
            contracts      = max(1, int(contract_size / pos.entry * 1000))
            margin_mode = self.get_symbol_margin(symbol).upper() or "ISOLATED"
            body = {
                "clientOid":    f"atf-{int(time.time()*1000)}",
                "side":          side,
                "symbol":        symbol,
                "type":          "market",
                "size":          contracts,
                "leverage":      self._leverage,
                "marginMode":    margin_mode,
                "positionSide":  position_side,
            }
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            log.info("[%s] Lead Trading ENTRY order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] Lead Trading entry order failed: %s", self.user_id, e)

    def _place_live_exit(self, pair: str, pos, price: float) -> None:
        """Close a futures position via KuCoin Lead Trading API."""
        if self._mode != "live" or not self._api_key:
            return
        try:
            from .native_trading_engine import _kucoin_post_signed
            from .kucoin_futures_client import normalize_futures_symbol
            symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
            side   = "sell" if pos.direction == "long" else "buy"
            position_side = "LONG" if pos.direction == "long" else "SHORT"
            contract_size = pos.size * self._leverage
            contracts     = max(1, int(contract_size / pos.entry * 1000))
            margin_mode = self.get_symbol_margin(symbol).upper() or "ISOLATED"
            body = {
                "clientOid":    f"atf-exit-{int(time.time()*1000)}",
                "side":          side,
                "symbol":        symbol,
                "type":          "market",
                "size":          contracts,
                "leverage":      self._leverage,
                "marginMode":    margin_mode,
                "positionSide":  position_side,
                "reduceOnly":    True,
            }
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            log.info("[%s] Lead Trading EXIT order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] Lead Trading exit order failed: %s", self.user_id, e)

    # ── Manual order management ──────────────────────────────────────────

    def place_pending_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        price: float | None = None,
        stop_price: float | None = None,
        leverage: int | None = None,
        tp_price: float | None = None,
        sl_price: float | None = None,
        hidden: bool = False,
        post_only: bool = False,
        reduce_only: bool = False,
        time_in_force: str = "GTC",
        cost_usdt: float = 0,
    ) -> dict:
        with self._lock:
            self._order_counter += 1
            oid = f"pord-{self.user_id}-{self._order_counter}-{int(time.time())}"
            lev = leverage or self.get_symbol_leverage(symbol)
            order = PendingOrder(
                order_id=oid, symbol=symbol, side=side,
                order_type=order_type, size=size, price=price,
                stop_price=stop_price, leverage=lev,
                margin_mode=self.get_symbol_margin(symbol),
                tp_price=tp_price, sl_price=sl_price,
                hidden=hidden, post_only=post_only,
                reduce_only=reduce_only, time_in_force=time_in_force,
                cost_usdt=cost_usdt,
            )
            self._pending_orders[oid] = order
        return {"order_id": oid, "status": "pending", "symbol": symbol, "side": side, "type": order_type}

    def cancel_pending_order(self, order_id: str) -> dict:
        with self._lock:
            order = self._pending_orders.pop(order_id, None)
        if order is None:
            return {"error": f"Order {order_id} not found"}
        return {"cancelled": True, "order_id": order_id}

    def get_pending_orders(self, symbol: str | None = None) -> list[dict]:
        with self._lock:
            orders = list(self._pending_orders.values())
        if symbol:
            orders = [o for o in orders if o.symbol == symbol]
        return [
            {
                "order_id": o.order_id, "symbol": o.symbol, "side": o.side,
                "order_type": o.order_type, "size": o.size, "price": o.price,
                "stop_price": o.stop_price, "leverage": o.leverage,
                "margin_mode": o.margin_mode, "tp_price": o.tp_price,
                "sl_price": o.sl_price, "status": "pending",
                "created_at": str(o.created_at),
            }
            for o in orders
        ]

    def set_symbol_leverage(self, symbol: str, leverage: int) -> dict:
        lev = max(1, min(20, leverage))
        with self._lock:
            self._per_symbol_leverage[symbol] = lev
            self._leverage = lev
        return {"symbol": symbol, "leverage": lev}

    def get_symbol_leverage(self, symbol: str) -> int:
        return self._per_symbol_leverage.get(symbol, self._leverage)

    def set_symbol_margin(self, symbol: str, mode: str) -> dict:
        mode = mode.lower()
        if mode not in ("cross", "isolated"):
            return {"error": "mode must be 'cross' or 'isolated'"}
        with self._lock:
            self._per_symbol_margin[symbol] = mode
            self._margin_mode = mode
        return {"symbol": symbol, "margin_mode": mode}

    def get_symbol_margin(self, symbol: str) -> str:
        return self._per_symbol_margin.get(symbol, self._margin_mode)

    def _check_pending_orders(self, pair: str, current_price: float):
        """Check and fill pending orders that match the current price (paper mode)."""
        symbol_variants = [
            pair.replace("/", "").replace("USDT", "USDTM"),
            pair.replace("/", "-"),
            pair,
        ]
        orders_to_fill = []
        with self._lock:
            for oid, order in list(self._pending_orders.items()):
                if order.symbol not in symbol_variants:
                    continue
                if order.should_fill(current_price):
                    orders_to_fill.append((oid, order))

        for oid, order in orders_to_fill:
            with self._lock:
                self._pending_orders.pop(oid, None)
            direction = "long" if order.side == "buy" else "short"
            fill_price = order.price if order.price else current_price
            now = datetime.now(timezone.utc)
            sl = order.sl_price or (fill_price * (1 - abs(self._stoploss)) if direction == "long" else fill_price * (1 + abs(self._stoploss)))
            tp = order.tp_price or (fill_price * (1 + self._take_profit) if direction == "long" else fill_price * (1 - self._take_profit))
            lev = order.leverage or self._leverage
            with self._lock:
                pos = FuturesPosition(
                    pair=pair, direction=direction,
                    entry=fill_price, sl=sl, tp=tp,
                    size=order.size, leverage=lev,
                    opened_at=now, trade_id=f"{pair}#filled#{oid}",
                )
                pos.db_id = _persist_open_trade(
                    self.user_id, pos, self._mode, self._strategy_id,
                    leverage=lev, market_type="futures",
                )
                trade_key = f"{pair}#filled#{oid}"
                self.positions[trade_key] = pos
                # Deduct USDT margin from balance, not raw size (which may be BTC)
                usdt_cost = getattr(order, "cost_usdt", 0) or 0
                if usdt_cost > 0:
                    margin = usdt_cost  # cost_usdt is already the USDT stake
                else:
                    margin = order.size  # legacy: size is in USDT for paper
                self.balance -= margin
                self.last_action = f"FILLED order {oid} → {direction} {pair} @ {fill_price:.4f} {lev}x"
                log.info("[%s] %s", self.user_id, self.last_action)

    # ── Status override — adds leverage + liquidation info ───────────────

    @property
    def status(self) -> dict:
        # super().status is a @property on NativeTradingEngine — access without ()
        base = dict(super().status)
        base["market_type"]   = "futures"
        base["leverage"]      = self._leverage
        base["margin_mode"]   = self._margin_mode
        base["pending_orders"] = len(self._pending_orders)
        base["action_log"]    = list(self.action_log)
        base["signal_count"]  = self.signal_count
        base["winding_down"]  = self._winding_down
        for pos_info in base.get("positions", []):
            for k, p in self.positions.items():
                if p.pair == pos_info["pair"]:
                    pos_info["liquidation_price"] = getattr(p, "liquidation_price", None)
                    pos_info["leverage"]          = getattr(p, "leverage", self._leverage)
                    break
        base["open_positions_detail"] = [
            {
                "pair": p.pair, "direction": p.direction, "entry": p.entry,
                "sl": p.sl, "tp": p.tp, "size": p.size,
                "leverage": getattr(p, "leverage", self._leverage),
                "liquidation_price": getattr(p, "liquidation_price", None),
                "opened_at": str(p.opened_at) if p.opened_at else None,
                "unrealized_pnl": round(
                    p.size * ((self._last_prices.get(p.pair, p.entry) - p.entry) / p.entry
                    if p.direction == "long" else
                    (p.entry - self._last_prices.get(p.pair, p.entry)) / p.entry)
                    * getattr(p, "leverage", 1), 4
                ),
                "current_price": self._last_prices.get(p.pair, p.entry),
            }
            for p in self.positions.values()
        ]
        base["closed_trades_detail"] = [
            {
                "pair": t.pair, "direction": t.direction,
                "entry": t.entry, "exit": t.exit_price,
                "pnl": round(t.pnl_abs, 4), "pnl_pct": round(t.pnl_pct, 2),
                "reason": t.reason,
                "opened_at": str(t.opened_at) if t.opened_at else None,
                "closed_at": str(t.closed_at) if t.closed_at else None,
                "leverage": getattr(t, "leverage", self._leverage),
            }
            for t in self.closed_trades[-20:]
        ]
        return base


# ── Registry — supports multiple concurrent bots per user ──────────────

class FuturesEngineRegistry:
    """Process-wide futures engine registry supporting multiple bots per user."""

    def __init__(self):
        import threading
        self._engines: dict[str, FuturesEngine] = {}
        self._bot_engines: dict[str, FuturesEngine] = {}
        self._lock = threading.Lock()

    def for_user(self, user_id: str) -> FuturesEngine:
        """Get the default (manual trading) engine for a user."""
        with self._lock:
            if user_id not in self._engines:
                self._engines[user_id] = FuturesEngine(user_id)
            return self._engines[user_id]

    def for_bot(self, user_id: str, bot_key: str) -> FuturesEngine:
        """Get or create an isolated engine for a specific bot instance."""
        full_key = f"{user_id}:{bot_key}"
        with self._lock:
            if full_key not in self._bot_engines:
                self._bot_engines[full_key] = FuturesEngine(user_id)
            return self._bot_engines[full_key]

    def stop_bot(self, user_id: str, bot_key: str):
        """Stop and remove a bot engine."""
        full_key = f"{user_id}:{bot_key}"
        with self._lock:
            eng = self._bot_engines.pop(full_key, None)
        if eng:
            eng.stop()

    def user_bot_engines(self, user_id: str) -> list[tuple[str, FuturesEngine]]:
        """List all bot engines for a user."""
        prefix = f"{user_id}:"
        with self._lock:
            return [
                (k.split(":", 1)[1], e)
                for k, e in self._bot_engines.items()
                if k.startswith(prefix)
            ]

    def all_running(self) -> list[tuple[str, FuturesEngine]]:
        with self._lock:
            running = [(uid, e) for uid, e in self._engines.items() if e.is_running]
            running += [(k, e) for k, e in self._bot_engines.items() if e.is_running]
            return running


futures_engine_registry = FuturesEngineRegistry()
