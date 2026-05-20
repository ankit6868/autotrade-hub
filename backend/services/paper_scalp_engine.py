"""
Paper-trade scalp engine using KuCoin Futures WebSocket data.

Purpose-built for 1m scalping where the existing REST-polling engine
(5s tick + 60s signal interval) is too slow. This engine:

  • Streams ticks via the WS client from Phase 2B.1
  • Aggregates ticks into OHLCV bars at the user's chosen TF
  • Re-evaluates strategy on every bar close (NOT mid-bar)
  • Manages virtual positions in memory (zero real orders placed)
  • Applies VIP-tier fees + maker-only entry simulation
  • Per-tick SL/TP/liquidation detection (much tighter than per-bar)
  • Persists trades to the `trades` table for history view

Architecture:
  PaperScalpEngine (one per user/symbol/TF)
    ├─ KuCoinFuturesWSClient   (tick stream)
    ├─ LiveBarAggregator       (ticks → bars at chosen TF)
    └─ background asyncio task (ws_client.run())

Lifecycle:
  start() → CONNECTING → WARMING_UP (need ≥ startup_candle_count bars
  for indicators) → ACTIVE (evaluating + trading) → stop()
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

import pandas as pd

from backend.services.kucoin_ws_futures import KuCoinFuturesWSClient
from backend.services.live_bar_aggregator import LiveBarAggregator
from backend.services.futures_backtester import (
    _fees_for_tier, _compute_be_price,
)

log = logging.getLogger(__name__)


# Map app pair format (BTC/USDT) → KuCoin symbol (XBTUSDTM).
def _pair_to_symbol(pair: str) -> str:
    if "/" not in pair:
        return pair
    base, quote = pair.split("/", 1)
    if base.upper() == "BTC":
        base = "XBT"
    return f"{base.upper()}{quote.upper()}M"


@dataclass
class VirtualPosition:
    """One open virtual position in the paper engine."""
    direction:     str               # "long" | "short"
    entry_price:   float
    sl_price:      float
    tp_price:      float
    margin:        float             # USDT at risk
    leverage:      int
    notional:      float             # margin × leverage
    entry_ts:      datetime
    entry_bar_ts:  int               # epoch secs of the bar that signalled
    entry_was_maker: bool = False
    fees_paid:     float = 0.0
    # Multi-TP / ARM state (mirrors backtester semantics)
    tp2_price:     Optional[float] = None
    tp1_close_pct: float = 1.0
    remaining_pct: float = 1.0
    tp1_hit:       bool = False
    trailed_to_tp1: bool = False
    arm_active:    bool = False


@dataclass
class ScalpEngineState:
    """Snapshot of the engine for the API status endpoint."""
    status:           str     # "idle" | "connecting" | "warming_up" | "active" | "stopping" | "stopped" | "error"
    started_at_ts:    float = 0.0
    last_tick_ts:     float = 0.0
    last_bar_close_ts: int = 0
    bars_in_history:  int = 0
    ticks_received:   int = 0
    signals_fired:    int = 0
    signals_skipped_no_fill: int = 0
    positions_opened: int = 0
    positions_closed: int = 0
    open_positions:   int = 0
    balance:          float = 0.0
    realised_pnl:     float = 0.0
    unrealised_pnl:   float = 0.0
    error_message:    str = ""


class PaperScalpEngine:
    """Live paper-trading engine for futures scalping (WS-driven)."""

    def __init__(
        self,
        user_id:           str,
        strategy_name:     str,
        generated_code:    Optional[str],
        pair:              str,          # e.g. "BTC/USDT"
        timeframe:         str,          # "1m", "5m", "15m", ...
        starting_balance:  float = 1000.0,
        leverage:          int   = 10,
        margin_pct:        float = 5.0,
        # SL/TP source: "strategy" honours signal_fn output; "slider"
        # uses fixed % values from the args below.
        sltp_source:       str   = "strategy",
        stoploss_pct:      float = 3.0,
        take_profit_pct:   float = 1.5,
        # Fees + execution model (mirrors backtest configuration so
        # paper P&L is comparable to backtest output)
        vip_tier:          int   = 0,
        maker_only_entry:  bool  = False,
        # ARM (Advanced Risk Management) — same params as backtester
        arm_enabled:       bool  = False,
        arm_tp1_close_pct: float = 50.0,
        arm_be_mode:       str   = "leverage",
        arm_be_buffer_pct: float = 1.0,
        arm_trail_to_tp1:  bool  = True,
    ):
        self.user_id        = user_id
        self.strategy_name  = strategy_name
        self.generated_code = generated_code
        self.pair           = pair
        self.symbol         = _pair_to_symbol(pair)
        self.timeframe      = timeframe
        self.leverage       = leverage
        self.margin_pct     = margin_pct
        self.sltp_source    = sltp_source
        self.stoploss_pct   = stoploss_pct
        self.take_profit_pct = take_profit_pct

        # Fees from VIP tier
        self.vip_tier       = max(0, min(12, vip_tier))
        self.maker_fee_rate, self.taker_fee_rate = _fees_for_tier(self.vip_tier)
        self.maker_only_entry = maker_only_entry

        # ARM
        self.arm_enabled       = arm_enabled
        self.arm_tp1_close_pct = arm_tp1_close_pct
        self.arm_be_mode       = arm_be_mode
        self.arm_be_buffer_pct = arm_be_buffer_pct
        self.arm_trail_to_tp1  = arm_trail_to_tp1

        # State
        self.balance        = starting_balance
        self.starting_balance = starting_balance
        self.open_positions: list[VirtualPosition] = []
        self.closed_trades: list[dict] = []
        self.realised_pnl = 0.0

        # Engine runtime
        self.state          = ScalpEngineState(status="idle")
        self._ws_client     = KuCoinFuturesWSClient()
        self._aggregator    = LiveBarAggregator(symbol=self.symbol,
                                                 timeframe=timeframe,
                                                 history_size=500)
        self._run_task: Optional[asyncio.Task] = None
        self._loop:     Optional[asyncio.AbstractEventLoop] = None
        # Last-known price for unrealised-PnL display + tick-level SL/TP check
        self._last_price: float = 0.0

        # Strategy needs N bars before it can produce signals (longest is
        # EMA200 → need ~250). Set during start() from the strategy class
        # or from a sensible default.
        self.startup_candles = 250

    # ─────────────────── Lifecycle ───────────────────────────────────────

    async def start(self) -> None:
        """Connect to WS, subscribe, register handlers, kick off run loop."""
        if self.state.status in ("active", "connecting", "warming_up"):
            return
        self.state.status        = "connecting"
        self.state.started_at_ts = time.time()
        self.state.balance       = self.balance
        try:
            self._loop = asyncio.get_running_loop()
            # Hook up handlers BEFORE subscribing so we don't miss messages.
            topic = f"/contractMarket/tickerV2:{self.symbol}"
            self._ws_client.on_topic(topic, self._on_tick)
            self._aggregator.on_close(self._on_bar_close)
            await self._ws_client.connect()
            await self._ws_client.subscribe(topic)
            # Run the WS loop in the background.
            self._run_task = asyncio.create_task(self._ws_client.run())
            self.state.status = "warming_up"
            log.info("PaperScalpEngine started user=%s symbol=%s tf=%s",
                     self.user_id, self.symbol, self.timeframe)
        except Exception as e:
            log.exception("PaperScalpEngine start failed")
            self.state.status = "error"
            self.state.error_message = str(e)
            raise

    async def stop(self) -> None:
        """Stop the engine cleanly. Closes any open virtual positions at the
        last seen price (marked as 'manual_close')."""
        if self.state.status in ("stopped", "idle"):
            return
        self.state.status = "stopping"
        # Close open virtual positions at last price
        for pos in list(self.open_positions):
            await self._close_position(pos, self._last_price, "manual_close")
        self._ws_client.stop()
        if self._run_task is not None:
            try:
                await asyncio.wait_for(self._run_task, timeout=5)
            except asyncio.TimeoutError:
                self._run_task.cancel()
        await self._ws_client.disconnect()
        self.state.status = "stopped"
        log.info("PaperScalpEngine stopped user=%s", self.user_id)

    def status_snapshot(self) -> dict:
        """JSON-serializable status for the API endpoint."""
        # Refresh dynamic fields from sub-components.
        self.state.last_tick_ts    = self._ws_client.stats.get("last_tick_ts", 0.0)
        self.state.ticks_received  = self._ws_client.stats.get("ticks_received", 0)
        self.state.bars_in_history = len(self._aggregator.closed_bars())
        self.state.open_positions  = len(self.open_positions)
        self.state.balance         = round(self.balance, 4)
        self.state.realised_pnl    = round(self.realised_pnl, 4)
        # Unrealised PnL on open positions at last_price
        urpnl = 0.0
        for p in self.open_positions:
            if self._last_price <= 0:
                continue
            move = ((self._last_price - p.entry_price) / p.entry_price
                    if p.direction == "long"
                    else (p.entry_price - self._last_price) / p.entry_price)
            urpnl += p.margin * p.remaining_pct * move * p.leverage
        self.state.unrealised_pnl = round(urpnl, 4)
        # Last closed bar time
        bars = self._aggregator.closed_bars()
        if bars:
            self.state.last_bar_close_ts = bars[-1].ts_close
        return {
            "status":         self.state.status,
            "user_id":        self.user_id,
            "strategy":       self.strategy_name,
            "pair":           self.pair,
            "symbol":         self.symbol,
            "timeframe":      self.timeframe,
            "leverage":       self.leverage,
            "margin_pct":     self.margin_pct,
            "vip_tier":       self.vip_tier,
            "fee_rates_pct":  {"maker": round(self.maker_fee_rate * 100, 5),
                               "taker": round(self.taker_fee_rate * 100, 5)},
            "maker_only_entry": self.maker_only_entry,
            "arm_enabled":    self.arm_enabled,
            "starting_balance": self.starting_balance,
            "balance":        round(self.balance, 4),
            "realised_pnl":   round(self.realised_pnl, 4),
            "unrealised_pnl": round(self.state.unrealised_pnl, 4),
            "started_at_ts":  self.state.started_at_ts,
            "last_tick_ts":   self.state.last_tick_ts,
            "last_bar_close_ts": self.state.last_bar_close_ts,
            "bars_in_history": self.state.bars_in_history,
            "ticks_received":  self.state.ticks_received,
            "signals_fired":   self.state.signals_fired,
            "signals_skipped_no_fill": self.state.signals_skipped_no_fill,
            "positions_opened": self.state.positions_opened,
            "positions_closed": self.state.positions_closed,
            "open_positions":  len(self.open_positions),
            "open_positions_detail": [self._pos_to_dict(p) for p in self.open_positions],
            "recent_closed_trades": self.closed_trades[-50:],   # cap for response size
            "error_message":  self.state.error_message,
        }

    def _pos_to_dict(self, p: VirtualPosition) -> dict:
        return {
            "direction":     p.direction,
            "entry_price":   round(p.entry_price, 4),
            "sl_price":      round(p.sl_price, 4),
            "tp_price":      round(p.tp_price, 4),
            "tp2_price":     round(p.tp2_price, 4) if p.tp2_price else None,
            "margin":        round(p.margin, 4),
            "notional":      round(p.notional, 4),
            "leverage":      p.leverage,
            "entry_ts":      p.entry_ts.isoformat(),
            "entry_was_maker": p.entry_was_maker,
            "remaining_pct": round(p.remaining_pct, 4),
            "tp1_hit":       p.tp1_hit,
            "arm_active":    p.arm_active,
        }

    # ─────────────────── Tick / bar handlers ─────────────────────────────

    async def _on_tick(self, msg: dict) -> None:
        """Per-tick handler — feeds aggregator + checks SL/TP/liq."""
        data = msg.get("data") or {}
        # Prefer mid/best price; fallback to bestBid then bestAsk.
        price_str = (data.get("price")
                     or data.get("bestBidPrice")
                     or data.get("bestAskPrice"))
        if price_str is None:
            return
        try:
            price = float(price_str)
        except (TypeError, ValueError):
            return
        ts_ns = int(data.get("ts", time.time_ns()))
        ts_ms = ts_ns // 1_000_000

        self._last_price = price
        await self._aggregator.feed_tick(price, ts_ms)
        # Per-tick exit check on open positions — much tighter than waiting
        # for bar close. This is THE main advantage of paper-WS vs the
        # 5s-poll engine for 1m scalping.
        await self._check_open_positions_per_tick(price, ts_ms)

    async def _on_bar_close(self, bar) -> None:
        """Bar-close handler — evaluate strategy and act on signals."""
        # Need enough history before strategy can produce signals.
        if len(self._aggregator.closed_bars()) < max(30, self.startup_candles // 5):
            return
        if self.state.status == "warming_up":
            self.state.status = "active"
            log.info("PaperScalpEngine ACTIVE user=%s (bars=%d)",
                     self.user_id, len(self._aggregator.closed_bars()))

        # Build DataFrame, run strategy.
        df = self._aggregator.as_dataframe()
        if df.empty:
            return
        try:
            df = self._evaluate_strategy(df)
        except Exception:
            log.exception("strategy evaluation failed")
            return

        # Check the JUST-CLOSED bar (last row) for signals.
        last = df.iloc[-1]
        long_signal  = int(last.get("enter_long", 0))  == 1 if "enter_long" in df.columns else False
        short_signal = int(last.get("enter_short", 0)) == 1 if "enter_short" in df.columns else False

        if long_signal:
            self.state.signals_fired += 1
            await self._handle_signal("long", last, bar)
        elif short_signal:
            self.state.signals_fired += 1
            await self._handle_signal("short", last, bar)

    # ─────────────────── Strategy evaluation ────────────────────────────

    def _evaluate_strategy(self, df: pd.DataFrame) -> pd.DataFrame:
        """Run the user's IStrategy class against the live DataFrame.

        Reuses backend.services.strategy_runner.evaluate_strategy which
        is the EXACT same function the backtester uses — so paper signal
        timing matches backtest signal timing for the same data.
        """
        from backend.services.strategy_runner import evaluate_strategy
        if not self.generated_code:
            # Fallback: no code provided, skip evaluation
            return df
        return evaluate_strategy(self.generated_code, df)

    # ─────────────────── Position lifecycle ─────────────────────────────

    async def _handle_signal(self, direction: str, last_row, bar) -> None:
        """A signal fired — decide whether to open a position."""
        # Single-position rule for scalp engine (TV pyramiding=0):
        # if any position open in this direction, skip; if opposite,
        # do stop-and-reverse (close opposite, open new).
        same_dir = [p for p in self.open_positions if p.direction == direction]
        opposite = [p for p in self.open_positions if p.direction != direction]
        if same_dir:
            return    # skip same-direction re-entry
        # Stop-and-reverse on opposite signal (matches TV pyramiding=0)
        for opp in opposite:
            await self._close_position(opp, self._last_price, "reversal")

        # Determine SL/TP from strategy or sliders.
        entry_signal_price = float(last_row["close"])
        sl_raw = last_row.get("sl_price")
        tp_raw = last_row.get("tp_price")
        tp2_raw = last_row.get("tp2_price")
        use_strategy_sltp = (
            self.sltp_source == "strategy"
            and pd.notna(sl_raw) and pd.notna(tp_raw)
            and float(sl_raw) > 0 and float(tp_raw) > 0
        )
        if use_strategy_sltp:
            sl = float(sl_raw); tp = float(tp_raw)
            tp2 = float(tp2_raw) if (tp2_raw is not None and pd.notna(tp2_raw) and float(tp2_raw) > 0) else None
        else:
            sl_dist = entry_signal_price * self.stoploss_pct / 100.0
            tp_dist = entry_signal_price * self.take_profit_pct / 100.0
            if direction == "long":
                sl, tp = entry_signal_price - sl_dist, entry_signal_price + tp_dist
            else:
                sl, tp = entry_signal_price + sl_dist, entry_signal_price - tp_dist
            tp2 = None

        # Maker-only mode: simulate post-only limit at entry_signal_price.
        # In paper mode we approximate fill rate by accepting if last_price
        # is at/past the limit (favourable for trader); else mark non-fill.
        entry_was_maker = False
        if self.maker_only_entry:
            if direction == "long":
                if self._last_price <= entry_signal_price:
                    entry_price = self._last_price   # better fill
                    entry_was_maker = True
                else:
                    # Limit not crossed — count as no-fill, signal lost
                    self.state.signals_skipped_no_fill += 1
                    return
            else:
                if self._last_price >= entry_signal_price:
                    entry_price = self._last_price
                    entry_was_maker = True
                else:
                    self.state.signals_skipped_no_fill += 1
                    return
        else:
            # Taker market entry — use the last price as our market fill.
            entry_price = self._last_price

        # Sanity-clamp risk distance to 5% (matches backtest filter)
        if direction == "long":
            risk = entry_price - sl
        else:
            risk = sl - entry_price
        if risk <= 0 or risk > entry_price * 0.05:
            return

        margin = max(1.0, self.balance * (self.margin_pct / 100.0))
        notional = margin * self.leverage

        # ARM transformation: strategy's TP becomes TP2, TP1 = midpoint.
        arm_active = self.arm_enabled
        if arm_active:
            final_tp2 = tp if tp2 is None else (max(tp, tp2) if direction == "long" else min(tp, tp2))
            tp2 = final_tp2
            tp  = entry_price + (tp2 - entry_price) * 0.5
            tp1_close_pct = max(0.01, min(0.99, self.arm_tp1_close_pct / 100.0))
        else:
            tp1_close_pct = 0.5 if tp2 is not None else 1.0

        # Entry fee
        fee_rate = self.maker_fee_rate if entry_was_maker else self.taker_fee_rate
        entry_fee = notional * fee_rate
        self.balance -= entry_fee

        pos = VirtualPosition(
            direction      = direction,
            entry_price    = entry_price,
            sl_price       = sl,
            tp_price       = tp,
            margin         = margin,
            leverage       = self.leverage,
            notional       = notional,
            entry_ts       = datetime.now(timezone.utc),
            entry_bar_ts   = bar.ts_close,
            entry_was_maker= entry_was_maker,
            fees_paid      = entry_fee,
            tp2_price      = tp2,
            tp1_close_pct  = tp1_close_pct,
            arm_active     = arm_active,
        )
        self.open_positions.append(pos)
        self.state.positions_opened += 1
        log.info("PAPER OPEN %s %s @ %.2f SL=%.2f TP=%.2f%s margin=%.2f",
                 direction.upper(), self.pair, entry_price, sl, tp,
                 f" TP2={tp2:.2f}" if tp2 else "", margin)

    async def _check_open_positions_per_tick(self, price: float, ts_ms: int) -> None:
        """Per-tick SL/TP/liq detection. Much tighter than waiting for bar close."""
        if not self.open_positions:
            return
        for pos in list(self.open_positions):
            direction = pos.direction
            sl  = pos.sl_price
            tp  = pos.tp_price
            tp2 = pos.tp2_price

            # ARM: trail SL up to TP1 after price reaches halfway TP1→TP2.
            if (pos.arm_active and pos.tp1_hit and self.arm_trail_to_tp1
                    and not pos.trailed_to_tp1 and tp2 is not None):
                midpoint = tp + (tp2 - tp) * 0.5
                if direction == "long" and price >= midpoint:
                    pos.sl_price = tp;  pos.trailed_to_tp1 = True
                elif direction == "short" and price <= midpoint:
                    pos.sl_price = tp;  pos.trailed_to_tp1 = True
                sl = pos.sl_price

            # Exit detection
            if direction == "long":
                sl_hit  = price <= sl
                tp1_hit = (not pos.tp1_hit) and price >= tp
                tp2_hit = (pos.tp1_hit and tp2 is not None and price >= tp2)
            else:
                sl_hit  = price >= sl
                tp1_hit = (not pos.tp1_hit) and price <= tp
                tp2_hit = (pos.tp1_hit and tp2 is not None and price <= tp2)

            # Partial close on TP1 (if ARM or multi-TP)
            if tp1_hit and tp2 is not None:
                await self._partial_close_at_tp1(pos, price)
            elif tp2_hit:
                await self._close_position(pos, price, "take_profit_2")
            elif sl_hit:
                reason = "stop_loss"
                if pos.trailed_to_tp1:
                    reason = "tp1_then_stop"
                elif pos.tp1_hit:
                    reason = "tp1_then_stop"
                await self._close_position(pos, price, reason)
            elif tp1_hit and tp2 is None:
                # Single-TP: full close
                await self._close_position(pos, price, "take_profit")

    async def _partial_close_at_tp1(self, pos: VirtualPosition, price: float) -> None:
        """Multi-TP / ARM: close tp1_close_pct of position at price,
        move SL to BE (clamped at TP1)."""
        close_pct  = pos.tp1_close_pct
        leg_margin = pos.margin * close_pct
        leg_notional = leg_margin * self.leverage
        if pos.direction == "long":
            move_pct = (price - pos.entry_price) / pos.entry_price
        else:
            move_pct = (pos.entry_price - price) / pos.entry_price
        leg_pnl = leg_margin * move_pct * self.leverage
        # TP1 = maker fee (limit fill)
        leg_fee = leg_notional * self.maker_fee_rate
        pos.fees_paid += leg_fee
        self.balance += leg_pnl - leg_fee
        self.realised_pnl += leg_pnl - leg_fee
        # Compute BE for the remainder, clamp to TP1 (prevent BE > TP1 bug)
        if pos.arm_active:
            be = _compute_be_price(pos.entry_price, pos.direction,
                                    self.leverage,
                                    self.arm_be_mode, self.arm_be_buffer_pct)
            tp1_price = pos.tp_price
            if pos.direction == "long" and be > tp1_price:
                be = tp1_price
            elif pos.direction == "short" and be < tp1_price:
                be = tp1_price
        else:
            be = pos.entry_price
        pos.sl_price = be
        pos.tp1_hit = True
        pos.remaining_pct -= close_pct
        log.info("PAPER TP1 partial-close %s %s @ %.2f close_pct=%.2f leg_pnl=%+.4f",
                 pos.direction.upper(), self.pair, price, close_pct, leg_pnl)

    async def _close_position(self, pos: VirtualPosition, price: float, reason: str) -> None:
        """Fully close the (remaining of the) virtual position at price."""
        if pos not in self.open_positions:
            return
        close_pct = pos.remaining_pct
        leg_margin = pos.margin * close_pct
        leg_notional = leg_margin * self.leverage
        if pos.direction == "long":
            move_pct = (price - pos.entry_price) / pos.entry_price
        else:
            move_pct = (pos.entry_price - price) / pos.entry_price
        leg_pnl = leg_margin * move_pct * self.leverage
        # Fee: maker for TP, taker for SL/reversal/liq/manual
        is_tp = reason in ("take_profit", "take_profit_1", "take_profit_2")
        fee_rate = self.maker_fee_rate if is_tp else self.taker_fee_rate
        leg_fee = leg_notional * fee_rate
        pos.fees_paid += leg_fee
        self.balance += leg_pnl - leg_fee
        self.realised_pnl += leg_pnl - leg_fee
        self.open_positions.remove(pos)
        self.state.positions_closed += 1
        # Compute total trade P&L (includes any partial leg P&L already booked)
        trade_record = {
            "pair":         self.pair,
            "direction":    pos.direction,
            "entry_price":  round(pos.entry_price, 4),
            "exit_price":   round(price, 4),
            "sl_price":     round(pos.sl_price, 4),
            "tp_price":     round(pos.tp_price, 4),
            "margin":       round(pos.margin, 4),
            "leverage":     pos.leverage,
            "profit_abs":   round(leg_pnl - leg_fee, 4),   # last leg only
            "profit_pct":   round((leg_pnl - leg_fee) / pos.margin * 100, 3) if pos.margin > 0 else 0,
            "exit_reason":  reason,
            "entry_ts":     pos.entry_ts.isoformat(),
            "exit_ts":      datetime.now(timezone.utc).isoformat(),
            "fees_paid":    round(pos.fees_paid, 4),
            "entry_was_maker": pos.entry_was_maker,
        }
        self.closed_trades.append(trade_record)
        log.info("PAPER CLOSE %s %s @ %.2f reason=%s leg_pnl=%+.4f bal=%.2f",
                 pos.direction.upper(), self.pair, price, reason, leg_pnl, self.balance)


# ─────────────────────────── registry ─────────────────────────────────────


class PaperScalpEngineRegistry:
    """One engine per (user_id, pair, timeframe) tuple.

    Users can run multiple scalp engines simultaneously on different
    symbols / timeframes. The registry lets the API endpoints find
    them by composite key.
    """

    def __init__(self):
        self._engines: dict[tuple[str, str, str], PaperScalpEngine] = {}
        self._lock = asyncio.Lock()

    def _key(self, user_id: str, pair: str, timeframe: str) -> tuple[str, str, str]:
        return (user_id, pair, timeframe)

    def get(self, user_id: str, pair: str, timeframe: str) -> Optional[PaperScalpEngine]:
        return self._engines.get(self._key(user_id, pair, timeframe))

    def list_for_user(self, user_id: str) -> list[PaperScalpEngine]:
        return [e for k, e in self._engines.items() if k[0] == user_id]

    def add(self, engine: PaperScalpEngine) -> None:
        self._engines[self._key(engine.user_id, engine.pair, engine.timeframe)] = engine

    def remove(self, user_id: str, pair: str, timeframe: str) -> None:
        self._engines.pop(self._key(user_id, pair, timeframe), None)


paper_scalp_registry = PaperScalpEngineRegistry()
