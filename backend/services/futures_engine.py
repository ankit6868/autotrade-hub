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
    _fetch_candles, _build_df, KUCOIN_BASE, TF_KUCOIN,
)

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
        "created_at", "db_id",
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

    def __init__(self, user_id: str):
        super().__init__(user_id)
        self._leverage      = 1
        self._market_type   = "futures"
        self._margin_mode   = "cross"
        self._pending_orders: dict[str, PendingOrder] = {}
        self._per_symbol_leverage: dict[str, int] = {}
        self._per_symbol_margin: dict[str, str] = {}
        self._order_counter = 0

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
        self._leverage     = max(1, min(125, int(leverage)))
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
        """Futures tick: liquidation + TP/SL every 5s, signals every 60s."""
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
                            seen_signal[pair] = False
                            self.last_action = (
                                f"LIQUIDATED {pair} @ {live_price:.4f} "
                                f"liq={pos.liquidation_price:.4f} P&L={pos.pnl_abs:+.2f}"
                            )
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
                        seen_signal[pair] = False
                        self.last_action = (
                            f"CLOSED {pair} @ {exit_price:.4f} ({reason}) "
                            f"P&L={pos.pnl_abs:+.2f} lev={getattr(pos,'leverage',1)}x"
                        )
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

                # Position limit guards
                if len(self.positions) >= self._max_open:
                    continue
                existing_for_pair = sum(
                    1 for p in self.positions.values() if p.pair == pair
                )
                if existing_for_pair >= getattr(self, '_max_per_pair', 2):
                    continue

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

            sig = signal_fn(df)
            if sig is None:
                continue

            entry_s, sl_s, tp_s, direction = sig
            entry = live_price
            sl_dist = abs(entry_s - sl_s)
            tp_dist = abs(tp_s - entry_s)
            if direction == "long":
                sl = entry - sl_dist
                tp = entry + tp_dist
            else:
                sl = entry + sl_dist
                tp = entry - tp_dist

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
                seen_signal[pair] = True
                self.last_action = (
                    f"OPENED futures {direction} {pair} @ {entry:.4f} "
                    f"{self._leverage}x liq={pos.liquidation_price:.4f}"
                )
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
        """Place a real futures market order on KuCoin Futures."""
        if self._mode != "live" or not self._api_key:
            return
        try:
            from .native_trading_engine import _kucoin_post_signed
            # KuCoin Futures uses contract symbol e.g. XBTUSDTM
            symbol = pair.replace("/", "").replace("USDT", "USDTM")
            side   = "buy" if pos.direction == "long" else "sell"
            # Calculate number of contracts: 1 contract = 0.001 BTC for XBTUSDTM
            contract_size  = pos.size * self._leverage  # total position value in USDT
            contracts      = max(1, int(contract_size / pos.entry * 1000))  # approx
            body = {
                "clientOid":  f"atf-{int(time.time()*1000)}",
                "side":        side,
                "symbol":      symbol,
                "type":        "market",
                "size":        contracts,
                "leverage":    str(self._leverage),
            }
            resp = _kucoin_post_signed(
                "/api/v1/orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            log.info("[%s] futures ENTRY order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] futures entry order failed: %s", self.user_id, e)

    def _place_live_exit(self, pair: str, pos, price: float) -> None:
        """Close a futures position on KuCoin Futures."""
        if self._mode != "live" or not self._api_key:
            return
        try:
            from .native_trading_engine import _kucoin_post_signed
            symbol = pair.replace("/", "").replace("USDT", "USDTM")
            side   = "sell" if pos.direction == "long" else "buy"
            contract_size = pos.size * self._leverage
            contracts     = max(1, int(contract_size / pos.entry * 1000))
            body = {
                "clientOid": f"atf-exit-{int(time.time()*1000)}",
                "side":       side,
                "symbol":     symbol,
                "type":       "market",
                "size":       contracts,
                "closeOrder": True,
            }
            resp = _kucoin_post_signed(
                "/api/v1/orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            log.info("[%s] futures EXIT order: %s", self.user_id, resp)
        except Exception as e:
            log.error("[%s] futures exit order failed: %s", self.user_id, e)

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
        lev = max(1, min(125, leverage))
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
                self.balance -= order.size
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
        for pos_info in base.get("positions", []):
            for k, p in self.positions.items():
                if p.pair == pos_info["pair"]:
                    pos_info["liquidation_price"] = getattr(p, "liquidation_price", None)
                    pos_info["leverage"]          = getattr(p, "leverage", self._leverage)
                    break
        return base


# ── Registry — one FuturesEngine per user (separate from spot registry) ──

class FuturesEngineRegistry:
    """Process-wide futures engine registry, isolated from spot NativeTradingRegistry."""

    def __init__(self):
        import threading
        self._engines: dict[str, FuturesEngine] = {}
        self._lock = threading.Lock()

    def for_user(self, user_id: str) -> FuturesEngine:
        with self._lock:
            if user_id not in self._engines:
                self._engines[user_id] = FuturesEngine(user_id)
            return self._engines[user_id]

    def all_running(self) -> list[tuple[str, FuturesEngine]]:
        with self._lock:
            return [(uid, e) for uid, e in self._engines.items() if e.is_running]


futures_engine_registry = FuturesEngineRegistry()
