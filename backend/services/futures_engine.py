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
    NativeTradingEngine, Position,
    _persist_open_trade, _persist_closed_trade,
    build_strategy_signal_fn, StrategyCompileError,
    _fetch_candles, _build_df, KUCOIN_BASE, TF_KUCOIN,
)
from . import risk_engine
from . import mtf_candles
from . import timeframe_adapter
from . import notifier as _notifier      # NICE-7 — Discord webhooks

log = logging.getLogger("futures_engine")

KUCOIN_FUTURES_BASE = "https://api-futures.kucoin.com"
MAINTENANCE_MARGIN  = 0.005   # 0.5% — standard KuCoin simplified

# KuCoin Futures kline granularity is in MINUTES (not seconds — fixed in
# commit f6003ff). Map from our TF strings to KuCoin's minute count.
_FUTURES_TF_MIN = {
    "1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30,
    "1h": 60, "2h": 120, "4h": 240, "8h": 480, "12h": 720,
    "1d": 1440, "1w": 10080,
}


def _fetch_futures_candles(pair: str, tf: str, limit: int = 200) -> list[dict]:
    """Bug-fix: the legacy `_fetch_candles` hits api.kucoin.com (SPOT). The
    futures engine MUST use futures candles — basis between spot and perp
    can be 0.1-0.5% in normal markets and several % during stress events,
    so strategies firing on spot data on the futures engine were trading
    a different market than the chart implied.

    This fetcher hits api-futures.kucoin.com/api/v1/kline/query — same
    endpoint /api/market/ohlcv uses. Returns the same dict shape as
    _fetch_candles so callers don't need to change downstream code.
    """
    from ._kucoin_proxy import urlopen as _proxy_urlopen
    import urllib.request as _ureq
    from .kucoin_futures_client import normalize_futures_symbol
    # Pair like "BTC/USDT" → "XBTUSDTM"
    sym = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
    gran = _FUTURES_TF_MIN.get(tf, 15)
    now_ms  = int(time.time() * 1000)
    # +5 bars buffer so dropping the partial bar still leaves `limit` closed bars.
    from_ms = now_ms - (limit + 5) * gran * 60_000
    url = (f"{KUCOIN_FUTURES_BASE}/api/v1/kline/query"
           f"?symbol={sym}&granularity={gran}&from={from_ms}&to={now_ms}")
    try:
        req = _ureq.Request(url, headers={"User-Agent": "AutoTradeHub/2.0"})
        with _proxy_urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read().decode())
    except Exception as e:
        log.warning("_fetch_futures_candles %s/%s fetch failed: %s", sym, tf, e)
        return []
    if str(payload.get("code")) != "200000":
        return []
    rows: list[dict] = []
    tf_secs = gran * 60
    now_sec = int(time.time())
    for r in payload.get("data") or []:
        if len(r) < 6:
            continue
        ts = int(r[0]) // 1000   # KuCoin returns ms; our convention is seconds
        if ts >= now_sec - tf_secs:
            continue              # drop the currently-forming partial bar
        rows.append({
            "ts":    ts,
            "open":  float(r[1]),
            "high":  float(r[2]),
            "low":   float(r[3]),
            "close": float(r[4]),
            "vol":   float(r[5]),
        })
    rows.sort(key=lambda r: r["ts"])
    return rows[-limit:]


# ── Contract multiplier cache (per-symbol lot size lookup) ──────────────
#
# Bug-fix: the legacy live order placement used `contracts = stake × leverage
# / entry × 1000` which assumes BTC's 0.001-BTC multiplier. For ETH (0.01),
# SOL (0.1), DOGE (1000-token contracts) this is wildly wrong — would
# trade either way too little or way too much. This cache hits
# /api/v1/contracts/{symbol} once per symbol, then memoises.

_CONTRACT_MULTIPLIERS: dict[str, float] = {}
_CONTRACT_LOT_SIZES:   dict[str, float] = {}    # min order step (e.g. 1 contract)
_CONTRACT_CACHE_LOCK = None
def _get_contract_multiplier(symbol: str) -> tuple[float, float]:
    """Return (multiplier, lot_size) for `symbol` (e.g. 'XBTUSDTM').

      multiplier — base units per contract (BTC=0.001, ETH=0.01, etc.)
      lot_size   — minimum contract increment (almost always 1)

    Defaults to (0.001, 1) if the lookup fails — same as BTC. Conservative
    fallback: BTC's tiny lot size means a misclassified pair will trade
    1000× smaller, not larger.
    """
    global _CONTRACT_CACHE_LOCK
    import threading as _t
    if _CONTRACT_CACHE_LOCK is None:
        _CONTRACT_CACHE_LOCK = _t.Lock()
    with _CONTRACT_CACHE_LOCK:
        cached = _CONTRACT_MULTIPLIERS.get(symbol)
        if cached is not None:
            return cached, _CONTRACT_LOT_SIZES.get(symbol, 1.0)
    # Cold lookup. The contracts endpoint is unauthenticated and cheap.
    try:
        from ._kucoin_proxy import urlopen as _proxy_urlopen
        import urllib.request as _ureq
        url = f"{KUCOIN_FUTURES_BASE}/api/v1/contracts/{symbol}"
        req = _ureq.Request(url, headers={"User-Agent": "AutoTradeHub/2.0"})
        with _proxy_urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode())
        data = payload.get("data") or {}
        mult = float(data.get("multiplier", 0.001) or 0.001)
        lot  = float(data.get("lotSize",    1.0)   or 1.0)
    except Exception as e:
        log.warning("contracts/%s lookup failed (using BTC defaults): %s", symbol, e)
        mult, lot = 0.001, 1.0
    with _CONTRACT_CACHE_LOCK:
        _CONTRACT_MULTIPLIERS[symbol] = mult
        _CONTRACT_LOT_SIZES[symbol]   = lot
    return mult, lot


def _stake_to_contracts(stake_usdt: float, leverage: int, entry: float,
                        symbol: str) -> int:
    """Convert (USDT margin, leverage, entry price) → integer contract count
    using the symbol's actual multiplier. Replaces the BTC-only `× 1000`
    calculation in the live order helpers.

      contracts = (stake × leverage) / (entry × multiplier)

    Then snapped to the nearest lotSize integer, floor=1.
    """
    if entry <= 0:
        return 1
    multiplier, lot = _get_contract_multiplier(symbol)
    base_qty = (stake_usdt * leverage) / entry          # how much BASE the position controls
    raw      = base_qty / max(multiplier, 1e-9)          # how many contracts that is
    snapped  = max(1, int(raw // max(lot, 1.0) * lot))
    return snapped


def _calc_liquidation_price(entry: float, direction: str, leverage: int) -> float:
    """Return the price at which this leveraged position gets liquidated."""
    if direction == "long":
        return round(entry * (1 - 1 / leverage + MAINTENANCE_MARGIN), 4)
    else:
        return round(entry * (1 + 1 / leverage - MAINTENANCE_MARGIN), 4)


class FuturesPosition(Position):
    """Position with leverage, liquidation tracking, and Advanced Risk
    Management (TP1/TP2 partial-close + BE trail + trail-to-TP1).

    ARM state machine
    -----------------
      pre_tp1   — TP from base class points at TP1 (midpoint of entry/tp2);
                  SL is the strategy's structural stop. TP1 hit → partial
                  close `tp1_close_pct` of size, move SL to BE-buffered
                  price (clamped to TP1 so the remainder can't instantly
                  stop out), set tp = tp2.
      post_tp1  — TP points at TP2; SL is the BE level. Once price reaches
                  midpoint(TP1, TP2) AND trail-to-TP1 is on, SL ratchets
                  up to TP1 (one-shot). At TP2 (or trailed SL) → close
                  the remainder and the position closes for real.

    When arm_active=False (default), the class behaves exactly like the
    legacy single-TP Position with leverage — no behaviour change.
    """

    def __init__(self, *args, leverage: int = 1, **kwargs):
        super().__init__(*args, **kwargs)
        self.leverage           = leverage
        self.liquidation_price  = _calc_liquidation_price(self.entry, self.direction, leverage)
        self._market_type       = "futures"

        # ── Advanced Risk Management state ──────────────────────────────
        # Defaults are the "ARM-disabled" identity: a single-TP position
        # that closes 100% on tp hit. configure_arm() flips these to enable
        # partial-close / BE-trail / trail-to-TP1 behaviour.
        self.arm_active:        bool  = False
        self.tp1_price:         Optional[float] = None    # midpoint(entry, tp2_price)
        self.tp2_price:         Optional[float] = None    # strategy's furthest TP
        self.tp1_close_pct:     float = 1.0               # 0.0..1.0 — fraction closed at TP1
        self.arm_be_mode:       str   = "leverage"        # "leverage" | "manual_pct" | "entry"
        self.arm_be_buffer_pct: float = 1.0
        self.arm_trail_to_tp1:  bool  = True
        # Runtime state
        self.tp1_hit:           bool  = False
        self.trailed_to_tp1:    bool  = False
        self.remaining_pct:     float = 1.0
        self.partial_pnl_abs:   float = 0.0
        # Audit log of partial fills — read by /api/futures/bots performance.
        self.partial_exits:     list  = []                # list[dict(price, reason, close_pct, pnl_abs, ts)]

    def configure_arm(
        self,
        *,
        tp2_price: float,
        tp1_close_pct: float,
        arm_be_mode: str = "leverage",
        arm_be_buffer_pct: float = 1.0,
        arm_trail_to_tp1: bool = True,
    ) -> None:
        """Enable ARM on this position. Call right after construction when
        the parent engine knows the user enabled ARM for this bot.

        Side effects:
          • self.tp1_price = midpoint(entry, tp2_price)
          • self.tp        = tp1_price (so check_exit fires TP1 first)
          • self.tp2_price = tp2_price (kept for post-TP1 phase)
          • self.tp1_close_pct = sanitised into (0.01, 0.99)
        """
        if tp2_price is None:
            return  # silently no-op — no TP2 to split, ARM can't engage
        if self.direction == "long" and tp2_price <= self.entry:
            return
        if self.direction == "short" and tp2_price >= self.entry:
            return

        self.arm_active        = True
        self.tp2_price         = float(tp2_price)
        # TP1 = midpoint between entry and TP2 — matches the user spec.
        self.tp1_price         = self.entry + (self.tp2_price - self.entry) * 0.5
        # Override the legacy tp with TP1 so check_exit's high>=tp / low<=tp
        # branch detects TP1 first. Once TP1 fires, the tick loop calls
        # _book_partial_at_tp1 which moves self.tp to TP2.
        self.tp                = self.tp1_price
        # Input is a PERCENTAGE (e.g. 50 for 50%), matching the bot create
        # form and the backtester's API. Convert to fraction and clamp into
        # (0.01, 0.99) so a 100% close at TP1 can't sneak through (would
        # make TP2 dead) and 0% can't either (would skip the partial step).
        self.tp1_close_pct     = max(0.01, min(0.99, float(tp1_close_pct) / 100.0))
        self.arm_be_mode       = arm_be_mode or "leverage"
        self.arm_be_buffer_pct = float(arm_be_buffer_pct or 0.0)
        self.arm_trail_to_tp1  = bool(arm_trail_to_tp1)

    def maybe_trail_to_tp1(self, current_price: float) -> bool:
        """One-shot SL ratchet: after TP1 has been booked and price has
        progressed to midpoint(TP1, TP2), move SL up from BE to TP1.

        Returns True if the SL was just moved (caller can log it).
        """
        if not (
            self.arm_active and self.tp1_hit and self.arm_trail_to_tp1
            and not self.trailed_to_tp1
            and self.tp1_price is not None and self.tp2_price is not None
        ):
            return False
        midpoint = self.tp1_price + (self.tp2_price - self.tp1_price) * 0.5
        crossed = (
            (self.direction == "long"  and current_price >= midpoint) or
            (self.direction == "short" and current_price <= midpoint)
        )
        if not crossed:
            return False
        self.sl = self.tp1_price
        self.trailed_to_tp1 = True
        return True

    def _compute_be_price(self) -> float:
        """Break-even SL price per the ARM spec — same formula as the
        backtester's _compute_be_price so live and backtest agree.

        Modes:
          • 'leverage'   — buffer = leverage / 10 (%). 20x → 2% above entry (long).
          • 'manual_pct' — buffer = arm_be_buffer_pct (%).
          • 'entry'      — no buffer (BE = entry).
        """
        if self.arm_be_mode == "leverage":
            buffer_pct = self.leverage / 1000.0
        elif self.arm_be_mode == "manual_pct":
            buffer_pct = max(0.0, self.arm_be_buffer_pct or 0.0) / 100.0
        else:
            buffer_pct = 0.0
        if self.direction == "long":
            be = self.entry * (1.0 + buffer_pct)
            # Safety clamp: BE must never sit on the wrong side of TP1, else
            # the remainder instantly stops out for a loss right after TP1.
            if self.tp1_price is not None and be > self.tp1_price:
                be = self.tp1_price
            return be
        else:
            be = self.entry * (1.0 - buffer_pct)
            if self.tp1_price is not None and be < self.tp1_price:
                be = self.tp1_price
            return be

    def book_partial_at_tp1(self, fill_price: float, ts: datetime) -> float:
        """Realise the TP1 partial close: compute leg P&L on `tp1_close_pct`
        of the original size, append a partial-exit record, move SL to BE
        (clamped), and re-target tp = tp2_price.

        Returns the leg P&L (caller adds to engine balance).

        Idempotent — subsequent calls are no-ops since tp1_hit guards.
        """
        if not self.arm_active or self.tp1_hit or self.tp2_price is None:
            return 0.0

        close_pct = self.tp1_close_pct
        leg_margin = self.size * close_pct
        if self.direction == "long":
            raw_pct = (fill_price - self.entry) / self.entry
        else:
            raw_pct = (self.entry - fill_price) / self.entry
        leveraged_pct = raw_pct * self.leverage
        leg_pnl = leg_margin * leveraged_pct

        # Update state
        self.partial_pnl_abs += leg_pnl
        self.tp1_hit          = True
        self.remaining_pct   -= close_pct
        # Move SL to BE (clamped), re-target TP at TP2.
        self.sl = self._compute_be_price()
        self.tp = self.tp2_price
        self.partial_exits.append({
            "ts":         ts.isoformat() if ts else None,
            "price":      round(float(fill_price), 6),
            "reason":     "take_profit_1",
            "close_pct":  round(float(close_pct), 4),
            "pnl_abs":    round(float(leg_pnl), 4),
        })
        return leg_pnl

    def check_liquidation(self, price: float) -> bool:
        """Return True if current price has crossed the liquidation level."""
        if self.direction == "long":
            return price <= self.liquidation_price
        else:
            return price >= self.liquidation_price

    def close(self, price: float, reason: str, ts: datetime):
        """Close the position. For ARM positions, only the REMAINING fraction
        realises the final-leg P&L; partial_pnl_abs (booked at TP1) is added
        on top so pnl_abs reflects the total round-trip return.

        For non-ARM positions, remaining_pct=1.0 and partial_pnl_abs=0.0 so
        the math reduces to the legacy single-leg path.
        """
        # Compute leg P&L manually (don't call super().close which clobbers
        # pnl_abs with size*pnl_pct/100, ignoring remaining_pct).
        self.closed_at   = ts
        self.exit_price  = price
        self.exit_reason = reason
        if self.direction == "long":
            raw_pct = (price - self.entry) / self.entry * 100
        else:
            raw_pct = (self.entry - price) / self.entry * 100
        leveraged_pct = raw_pct * self.leverage
        remaining_margin = self.size * self.remaining_pct
        final_leg_pnl = remaining_margin * (leveraged_pct / 100.0)
        # Liquidation can't lose more than the leg's margin.
        if reason == "liquidated":
            final_leg_pnl = max(final_leg_pnl, -remaining_margin)
        self.pnl_abs = final_leg_pnl + self.partial_pnl_abs
        # pnl_pct reflects ROI on the FULL initial margin so the UI shows
        # a sensible number even after a partial booking.
        self.pnl_pct = (self.pnl_abs / self.size * 100.0) if self.size > 0 else 0.0


class PendingOrder:
    """A pending limit/stop order waiting to be filled."""
    __slots__ = (
        "order_id", "symbol", "side", "order_type", "size", "price",
        "stop_price", "leverage", "margin_mode", "tp_price", "sl_price",
        "hidden", "post_only", "reduce_only", "time_in_force",
        "created_at", "db_id", "cost_usdt",
        # "paper" | "live". The /order endpoint registers both in the
        # engine for matching tracking purposes, but only paper orders
        # should be filled locally — live orders are filled by KuCoin
        # and surfaced via the order-reconcile path.
        "mode",
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
        # ── Advanced Risk Management config (Phase 3) ───────────────────
        # Set by start_futures from the bot create payload. When ARM is
        # off, these are ignored and positions use single-TP behaviour.
        self._arm_enabled:        bool  = False
        self._arm_tp1_close_pct:  float = 50.0           # %, sliced into (1, 99)
        self._arm_be_mode:        str   = "leverage"
        self._arm_be_buffer_pct:  float = 1.0
        self._arm_trail_to_tp1:   bool  = True
        # ── Phase 8: cooldown + max-trades/day + daily DD trip ──────────
        # Resolves PDF §7 safe-default + §16 edge-case ("max daily loss")
        # requirements that were missing from the live engine.
        #
        # cooldown_until[pair] holds the epoch-seconds before which no new
        # signal may fire on this pair (set on every close).
        # day_counters tracks trades opened today (resets at UTC midnight)
        # and trips the daily-DD breaker.
        self._cooldown_until:    dict[str, float] = {}
        self._max_trades_per_day:int   = 8
        self._cooldown_seconds:  int   = 0
        self._day_key:           str   = ""           # YYYY-MM-DD UTC
        self._day_trades:        int   = 0
        self._day_start_balance: float = 0.0
        self._day_max_dd_pct:    float = 25.0          # 25% daily DD trips bot
        self._day_dd_tripped:    bool  = False

        # ── MUST-2: Compile-failure circuit breaker ──────────────────────
        # If the strategy_runner raises N consecutive times during the
        # signal-scan loop (rare — usually a NaN crash on bad candle data
        # or an upstream KuCoin glitch), auto-stop the bot instead of
        # spamming the action_log indefinitely. The start-time compile
        # check in start_futures catches gross errors; this breaker
        # catches the ones that only manifest on real candle batches.
        self._consec_compile_failures: int = 0
        self._max_compile_failures:    int = 5
        # ── NICE-6: Pause flag ───────────────────────────────────────────
        # `_paused = True` blocks new entries (signal scan exits early)
        # but KEEPS managing open positions — TP / SL / liquidation /
        # ARM all keep firing. Different from wind_down which exits the
        # engine when positions all close. Pause can be toggled freely;
        # the bot resumes immediately when set back to False.
        self._paused: bool = False

    def wind_down(self):
        """Stop opening new positions but keep managing existing ones until all are closed."""
        self._winding_down = True
        self._log_action("wind_down", "Bot entering wind-down mode — managing open positions to exit, no new entries")
        log.info("[%s] Engine entering wind-down mode with %d open positions",
                 self.user_id, len(self.positions))

    @property
    def is_winding_down(self) -> bool:
        return self._winding_down

    # ── NICE-6: pause / resume (no new entries, keep managing positions) ─

    def pause(self) -> None:
        """Block opening new positions on the next signal scan. Existing
        positions keep running through TP/SL/ARM/liq. Idempotent."""
        if not self._paused:
            self._paused = True
            self._log_action("paused",
                "Bot paused — no new entries; existing positions still managed.")
            log.info("[%s] Engine paused", self.user_id)
            _notifier.notify_paused(
                self.user_id, strategy=self._strategy, mode=self._mode,
            )

    def resume(self) -> None:
        """Re-enable new entries. Idempotent."""
        if self._paused:
            self._paused = False
            self._log_action("resumed", "Bot resumed — new entries re-enabled.")
            log.info("[%s] Engine resumed", self.user_id)
            _notifier.notify_resumed(
                self.user_id, strategy=self._strategy, mode=self._mode,
            )

    @property
    def is_paused(self) -> bool:
        return self._paused

    def _get_live_price(self, pair: str) -> Optional[float]:
        """Fetch the most recent market price for a futures pair.

        Lookup order (fastest first):
          1. `self._last_prices[pair]` — populated by KuCoin WS bullet
             feed when available. ONLY trusted if < 15s old, otherwise
             treated as stale (previous bug: cache was returned forever
             without TTL → all paper trades had entry=exit=cached_value
             → PNL=0 stop_and_reverse loop).
          2. `mtf_candles.get_candles(pair, "1m")` — the cached 1m bar
             feed. Last bar's close is the freshest <1 min price we have.
          3. KuCoin REST `/api/v1/ticker` (futures public endpoint) —
             always works but adds ~150ms latency. Used as final fallback.

        Returns None on total failure so the caller skips this tick.
        """
        import time as _time
        now = _time.time()

        # 1. WS cache (only if fresh — within 15s).
        # In LIVE mode the KuCoin WS feed pushes price updates to
        # `_last_prices`, so values stay <1s fresh. In PAPER mode (no WS),
        # the cache is only populated by THIS method's fallback paths
        # below, so we need to invalidate it ourselves after the TTL.
        cached = self._last_prices.get(pair)
        cached_ts = self._last_prices_ts.get(pair, 0.0)
        if cached is not None and cached > 0 and (now - cached_ts) < 15.0:
            return float(cached)

        # 2. 1m bar feed cache — the freshest minute-bar close.
        try:
            from . import mtf_candles
            df1m = mtf_candles.get_candles(pair, "1m", force_refresh=False)
            if df1m is not None and len(df1m) > 0:
                price = float(df1m["close"].iloc[-1])
                if price > 0:
                    self._last_prices[pair] = price
                    self._last_prices_ts[pair] = now
                    return price
        except Exception as _bar_exc:
            log.debug("[%s] _get_live_price 1m fetch failed for %s: %s",
                      self.user_id, pair, _bar_exc)

        # 3. REST ticker — last resort. Use the public/unauth endpoint
        # so paper-mode bots without API keys still get prices.
        try:
            from .kucoin_futures_client import normalize_futures_symbol
            from ._kucoin_proxy import urlopen as _proxy_urlopen
            import urllib.request, json
            sym = normalize_futures_symbol(pair)
            url = f"https://api-futures.kucoin.com/api/v1/ticker?symbol={sym}"
            req = urllib.request.Request(url, headers={"User-Agent": "autotrade-hub/1.0"})
            with _proxy_urlopen(req, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            data = (payload or {}).get("data") or {}
            price_str = data.get("price") or data.get("lastTradedPrice") or data.get("bestBidPrice")
            if price_str:
                price = float(price_str)
                if price > 0:
                    self._last_prices[pair] = price
                    self._last_prices_ts[pair] = now
                    return price
        except Exception as _rest_exc:
            log.debug("[%s] _get_live_price REST fallback failed for %s: %s",
                      self.user_id, pair, _rest_exc)

        # Final fallback: return the stale cached value if we have one
        # (better than None — engine continues tick, but PNL math will
        # be inaccurate). Logged so debugging shows it.
        if cached is not None and cached > 0:
            log.debug("[%s] _get_live_price all fetches failed, returning STALE cache %.2f (age=%.1fs)",
                      self.user_id, cached, now - cached_ts)
            return float(cached)
        return None

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

    def _persist_auto_stop(self, *, reason: str) -> None:
        """Flip StrategyInstance.is_running=False in the DB so the
        watchdog stops resurrecting this engine. Used when the bot
        decides to permanently exit (e.g. compile-failure circuit breaker
        hit its threshold). Best-effort — if the DB write fails the
        engine still stops in memory; the watchdog will resurrect once,
        the next compile fails immediately, and we'll try the persist
        again. Safe to call multiple times.
        """
        try:
            from backend.models.database import SessionLocal
            from backend.models.trade import StrategyInstance
            from sqlalchemy import update as sql_update
            with SessionLocal() as db:
                # We don't have the StrategyInstance.id on the engine, but
                # the (user_id, strategy_id, is_running=True) tuple uniquely
                # identifies the row — at most one such record per running
                # bot. Falling back to user_id+strategy_name covers the rare
                # case where strategy_id is missing (legacy templates).
                where_clauses = [
                    StrategyInstance.user_id == self.user_id,
                    StrategyInstance.is_running == True,  # noqa: E712
                ]
                if self._strategy_id is not None:
                    where_clauses.append(StrategyInstance.strategy_id == self._strategy_id)
                else:
                    where_clauses.append(StrategyInstance.strategy_name == self._strategy)
                db.execute(
                    sql_update(StrategyInstance)
                    .where(*where_clauses)
                    .values(is_running=False)
                )
                db.commit()
                log.info("[%s] Persisted auto-stop (reason=%s) to DB", self.user_id, reason)
        except Exception as exc:
            log.warning("[%s] Failed to persist auto-stop to DB: %s", self.user_id, exc)

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
        # Phase 3 — Advanced Risk Management
        arm_enabled:        bool  = False,
        arm_tp1_close_pct:  float = 50.0,
        arm_be_mode:        str   = "leverage",
        arm_be_buffer_pct:  float = 1.0,
        arm_trail_to_tp1:   bool  = True,
        # Phase 8 — Cooldown + max-trades-per-day + daily DD trip
        # Defaults come from the StrategyTemplate's trade_limits when
        # available; the bot create endpoint computes appropriate mode-based
        # values per PDF §7.
        max_trades_per_day: int   = 8,
        cooldown_candles:   int   = 3,
        max_daily_dd_pct:   float = 25.0,
        # Per-bot strategy overrides — surfaced into populate_indicators
        # via metadata['overrides'] so SMC-family strategies can read
        # session window + equal-price threshold without editing class
        # code. Strategies that don't look at metadata['overrides'] are
        # unaffected (these become dead params).
        session_start_hr_utc: int   = 12,
        session_end_hr_utc:   int   = 21,
        equal_price_thresh:   float = 0.001,
        # ── Optional per-strategy risk gates ─────────────────────────
        # 0 = disabled (don't enforce). When > 0, the engine adds the
        # corresponding circuit breaker. Strategies that don't set
        # these (most don't) get the legacy unbounded behaviour.
        max_hold_candles:    int   = 0,    # close trade after N bars open
        max_stops_per_day:   int   = 0,    # halt new entries after N stops today
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
        # ── ARM config — copied so every position opened by this engine
        # inherits the same settings (Phase 3 wiring).
        self._arm_enabled        = bool(arm_enabled)
        self._arm_tp1_close_pct  = float(arm_tp1_close_pct)
        self._arm_be_mode        = str(arm_be_mode or "leverage")
        self._arm_be_buffer_pct  = float(arm_be_buffer_pct or 0.0)
        self._arm_trail_to_tp1   = bool(arm_trail_to_tp1)
        # Phase 8 — cooldown / max-trades-per-day / daily DD config.
        # Convert cooldown_candles into seconds using the engine TF.
        tf_seconds = {"1m":60, "5m":300, "15m":900, "30m":1800,
                      "1h":3600, "4h":14400, "1d":86400}.get(timeframe, 900)
        self._cooldown_seconds   = max(0, int(cooldown_candles)) * tf_seconds
        self._max_trades_per_day = max(1, int(max_trades_per_day))
        self._day_max_dd_pct     = max(1.0, float(max_daily_dd_pct))
        self._cooldown_until     = {}
        self._day_key            = ""        # populated lazily by tick
        self._day_trades         = 0
        self._day_start_balance  = float(wallet)
        self._day_dd_tripped     = False
        # ── Per-bot strategy overrides (session / equal-price) ──────
        # Stored here so they survive process restarts via DB persist.
        # Passed to evaluate_strategy via metadata['overrides'].
        self._strategy_overrides = {
            "session_start_hr_utc": max(0, min(23, int(session_start_hr_utc))),
            "session_end_hr_utc":   max(0, min(23, int(session_end_hr_utc))),
            "equal_price_thresh":   max(0.0001, min(0.05, float(equal_price_thresh))),
        }
        # ── Optional per-strategy risk gates ─────────────────────────
        # max_hold_candles: force-close any position open longer than N
        # bars on the engine's execution TF. 0 = disabled. Useful for
        # scalp strategies that want a hard max-hold (e.g. 60 LTF
        # candles = 5h on 5m). Strategy can declare via class attribute
        # `max_hold_candles = 60`; engine reads it through compiled_df
        # attrs (same mechanism as max_trades_per_day).
        self._max_hold_candles  = max(0, int(max_hold_candles))
        # max_stops_per_day: halt new entries after this many stop-loss
        # exits today. 0 = disabled. Stricter than max_trades_per_day
        # because it specifically counts STOPS (signals of strategy
        # failure) — a strategy that's winning 10 trades in a row
        # doesn't trip even with low limit; one that loses 3 in a row
        # does. Resets at the start of a new UTC day, like _day_trades.
        self._max_stops_per_day = max(0, int(max_stops_per_day))
        self._day_stops         = 0
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

        # ── MUST-3: MTF cache warm-up ───────────────────────────────────
        # The Timeframe Adapter wants HTF candles (e.g. 1h + 4h when
        # execution_tf=5m) on EVERY signal scan. Without this warm-up,
        # the first scan after start_futures cold-fetches 3-4 TFs
        # sequentially = ~4 seconds of latency. Fire a background thread
        # that pre-populates mtf_candles._CACHE before the run loop's
        # first tick, so the first signal scan is responsive.
        try:
            from . import timeframe_adapter, mtf_candles
            # Strategy's authored TF (defaults to execution TF if missing).
            strat_tf = timeframe or "15m"
            bundle = timeframe_adapter.adapt(strategy_tf=strat_tf, execution_tf=timeframe)
            tfs_to_warm = bundle.required_timeframes
            def _warm():
                # Cap at the first 4 TFs so we don't burn through KuCoin
                # rate-limit budget on first start. Sequential is fine —
                # this thread doesn't block the engine.
                for p in pairs[:5]:                       # also cap pairs
                    for tf in tfs_to_warm[:4]:
                        try:
                            mtf_candles.get_candles(p, tf, force_refresh=False)
                        except Exception as _e:
                            log.debug("[%s] MTF warm-up %s/%s failed: %s",
                                      self.user_id, p, tf, _e)
            import threading as _t
            _t.Thread(target=_warm, daemon=True,
                      name=f"futures-warm-{self.user_id}").start()
            log.info("[%s] MTF cache warm-up dispatched for %s pair(s) × %s TF(s)",
                     self.user_id, len(pairs[:5]), len(tfs_to_warm[:4]))
        except Exception as _warm_exc:
            log.debug("[%s] MTF warm-up skipped: %s", self.user_id, _warm_exc)

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

    # ── Run loop (lives on FuturesEngine after the spot purge) ──────────
    #
    # Before the spot purge this method lived on NativeTradingEngine as a
    # shared base implementation. After the purge we kept only the futures
    # path, so the run loop now lives here directly. It drives an adaptive
    # tick rate (5s when positions are open, 60s when flat) and calls the
    # overridden _tick_continuous on every iteration.

    def _run_loop(self):
        """Adaptive futures-engine loop.

        Two speeds:
          • FAST (5 s)  — positions open: catches TP / SL / liq instantly.
          • SLOW (60 s) — flat: just scans for entry signals.
        """
        import time as _time

        seen_signal:    dict[str, object] = {}  # pair → direction of last fired signal
        last_signal_ts: dict[str, float]  = {}
        SIGNAL_INTERVAL = 60.0

        log.info("[%s] Futures engine loop started — strategy=%s pairs=%s mode=%s tf=%s",
                 self.user_id, self._strategy, self._pairs, self._mode, self._timeframe)

        last_reconcile_ts = 0.0
        RECONCILE_INTERVAL = 60.0    # check for live-position drift every 60s

        while not self._stop_evt.is_set():
            try:
                now_ts = _time.time()
                # Live-position drift reconciliation — once per minute is
                # plenty (KuCoin doesn't drift continuously, only on user
                # action / liquidation we missed / rejected re-order).
                if (self._mode == "live"
                    and now_ts - last_reconcile_ts >= RECONCILE_INTERVAL):
                    self._reconcile_live_positions()
                    last_reconcile_ts = now_ts
                self._tick_continuous(
                    None,                       # signal_fn ignored — engine builds its own per scan
                    seen_signal,
                    last_signal_ts=last_signal_ts,
                    signal_interval=SIGNAL_INTERVAL,
                )
                # Update the per-pair signal-scan clock so the
                # `if elapsed < signal_interval: continue` guard works.
                for pair in self._pairs:
                    last_signal_ts.setdefault(pair, 0.0)
                    if (now_ts - last_signal_ts[pair]) >= SIGNAL_INTERVAL:
                        last_signal_ts[pair] = now_ts
            except Exception as exc:
                with self._lock:
                    self.errors += 1
                    self.last_action = f"engine error: {exc}"
                log.warning("[%s] engine error: %s", self.user_id, exc)
                # Exponential back-off on repeated errors so we don't
                # hammer KuCoin during an outage.
                self._stop_evt.wait(min(60, 5 * max(1, self.errors)))
                continue

            # Adaptive sleep — 5s when positions open, 60s when flat.
            with self._lock:
                has_open = bool(self.positions)
            self._stop_evt.wait(5.0 if has_open else 60.0)

        log.info("[%s] Futures engine loop exited.", self.user_id)

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
                            # Phase 8: arm cooldown so the bot doesn't immediately
                            # re-enter on the same signal after a forced exit.
                            self._cooldown_until[pair] = time.time() + self._cooldown_seconds
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
                            _notifier.notify_liquidated(
                                self.user_id, pair=pair, direction=pos.direction,
                                liq_price=pos.liquidation_price, pnl=pos.pnl_abs,
                                leverage=self._leverage, mode=self._mode,
                            )
                            continue

                    # ── Phase 3 — ARM trail-to-TP1 ratchet ─────────────
                    # After TP1 has been booked and price reaches the
                    # midpoint of (TP1, TP2), move SL up to TP1 so the
                    # remainder can never exit below TP1.
                    if isinstance(pos, FuturesPosition) and pos.maybe_trail_to_tp1(live_price):
                        self.last_action = (
                            f"ARM TRAIL {pair} {pos.direction} SL→TP1 ({pos.sl:.4f}) "
                            f"price={live_price:.4f}"
                        )
                        self._log_action("arm_trail_tp1", self.last_action,
                            pair=pair, price=live_price, new_sl=pos.sl,
                            direction=pos.direction)
                        log.info("[%s] %s", self.user_id, self.last_action)

                    # ── Phase 3 — ARM pre-TP1 partial-close detection ──
                    # When ARM is active AND TP1 hasn't fired yet, check
                    # whether the current price has crossed TP1. If so,
                    # book the partial profit, move SL to BE (clamped),
                    # update pos.tp → tp2, and KEEP the position open.
                    # The remainder runs on the next tick with TP2 as target.
                    if (isinstance(pos, FuturesPosition) and pos.arm_active
                            and not pos.tp1_hit and pos.tp1_price is not None):
                        long_tp1_hit  = pos.direction == "long"  and live_price >= pos.tp1_price
                        short_tp1_hit = pos.direction == "short" and live_price <= pos.tp1_price
                        # SL hit before TP1? Fall through to standard exit.
                        sl_hit_first  = (
                            (pos.direction == "long"  and live_price <= pos.sl) or
                            (pos.direction == "short" and live_price >= pos.sl)
                        )
                        if (long_tp1_hit or short_tp1_hit) and not sl_hit_first:
                            leg_pnl = pos.book_partial_at_tp1(pos.tp1_price, now)
                            self.balance += leg_pnl
                            self.last_action = (
                                f"PARTIAL CLOSE {pair} {pos.direction} "
                                f"{pos.tp1_close_pct*100:.0f}% @ TP1 {pos.tp1_price:.4f} "
                                f"P&L={leg_pnl:+.2f}  SL→{pos.sl:.4f}  target TP2={pos.tp2_price:.4f}"
                            )
                            self._log_action("partial_close_tp1", self.last_action,
                                pair=pair, price=pos.tp1_price, pnl=leg_pnl,
                                close_pct=pos.tp1_close_pct,
                                new_sl=pos.sl, new_tp=pos.tp,
                                direction=pos.direction)
                            log.info("[%s] %s", self.user_id, self.last_action)
                            # In LIVE mode, send a reduce-only partial close
                            # to KuCoin so the real position is also reduced.
                            if self._mode == "live":
                                self._place_live_partial_close(pair, pos, pos.tp1_close_pct, pos.tp1_price)
                            # Don't fall through to check_exit on this tick —
                            # the position state has just changed; let the
                            # next tick re-evaluate against the new SL/TP.
                            continue

                    # TP/SL exit — checked every tick (5 s when positions open)
                    pos.update_trail(live_price)
                    exit_info = pos.check_exit(live_price, live_price)
                    # ── Max-hold force-close (optional gate) ─────────────
                    # If the strategy / user declared a max-hold limit and
                    # this position has been open longer than that, force
                    # an exit at the live price with reason="max_hold_expired".
                    # PDF / SMC-spec rationale: scalp/intraday strategies
                    # shouldn't sit in a setup forever — bias staleness
                    # eventually invalidates the trade thesis.
                    if exit_info is None and self._max_hold_candles > 0:
                        bars_held = int((now - pos.opened_at).total_seconds() // tf_seconds)
                        if bars_held >= self._max_hold_candles:
                            exit_info = (live_price, "max_hold_expired")
                    if exit_info:
                        exit_price, reason = exit_info
                        pos.close(exit_price, reason, now)
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        del self.positions[trade_key]
                        # Count stop-losses for the daily-stops breaker.
                        # Only "stop_loss" qualifies — TP exits, max-hold,
                        # and liquidations are tracked separately so they
                        # don't trip the bot's "strategy is failing" gate.
                        if reason == "stop_loss":
                            self._day_stops += 1
                        # Reset to None (Phase 2 edge detection stores direction).
                        seen_signal[pair] = None
                        # Phase 8: arm cooldown after every close (TP, SL, manual).
                        self._cooldown_until[pair] = time.time() + self._cooldown_seconds
                        # Include ARM partial-history in the summary when present.
                        arm_summary = ""
                        if isinstance(pos, FuturesPosition) and pos.partial_exits:
                            arm_summary = (
                                f" (ARM: {len(pos.partial_exits)} partial(s), "
                                f"partial P&L={pos.partial_pnl_abs:+.2f})"
                            )
                        self.last_action = (
                            f"CLOSED {pair} @ {exit_price:.4f} ({reason}) "
                            f"P&L={pos.pnl_abs:+.2f} lev={getattr(pos,'leverage',1)}x{arm_summary}"
                        )
                        self._log_action("closed", self.last_action,
                            pair=pair, price=exit_price, pnl=pos.pnl_abs,
                            direction=pos.direction, reason=reason,
                            entry_price=pos.entry,
                            arm_partial_exits=getattr(pos, "partial_exits", None),
                            arm_partial_pnl=getattr(pos, "partial_pnl_abs", 0.0))
                        log.info("[%s] %s", self.user_id, self.last_action)
                        _persist_closed_trade(self.user_id, pos, self._mode,
                                              self._strategy_id, pos.db_id)
                        if self._mode == "live":
                            self._place_live_exit(pair, pos, exit_price)
                        _notifier.notify_position_closed(
                            self.user_id, pair=pair, direction=pos.direction,
                            entry=pos.entry, exit_p=exit_price, pnl=pos.pnl_abs,
                            reason=reason, leverage=self._leverage, mode=self._mode,
                        )
                        # Copy-trading broadcast removed in spot purge —
                        # futures bots no longer publish signals to a
                        # copy-trading service. The futures terminal is
                        # single-user from the trading-pipeline perspective.

                # Wind-down: no new entries, auto-stop when all positions closed
                if self._winding_down:
                    if len(self.positions) == 0:
                        self._log_action("wind_down_complete", "All positions closed — stopping engine")
                        log.info("[%s] Wind-down complete, all positions closed", self.user_id)
                        # Flip DB is_running=False so the watchdog won't
                        # resurrect this bot 60s after the wind-down ends.
                        # Without this, "Stop bot with open positions" →
                        # wind-down → last position TP-hits → engine dies
                        # → 60s later watchdog finds is_running=True in DB,
                        # spins the engine back up → bot is "running" again
                        # even though the user explicitly stopped it.
                        self._persist_auto_stop(reason="wind_down_complete")
                        self._stop_evt.set()
                    continue

                # NICE-6: paused → skip the signal-scan path entirely.
                # Position management (above) already ran, so TP/SL/liq/ARM
                # are still serviced. Only NEW entries are blocked.
                if self._paused:
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
                # Bug-fix: use the FUTURES candle endpoint, not the spot one
                # imported from native_trading_engine. Strategies on the
                # futures engine must trade off futures perp data; spot/perp
                # basis can shift signal timing by several %.
                candles = _fetch_futures_candles(pair, self._timeframe, limit=200)
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
            #
            # No legacy fallback after the spot purge: if the strategy fails
            # to compile we LOG LOUDLY and SKIP this tick — better to halt
            # than silently run the wrong signal. The engine's start-time
            # compile check (in start_futures) already vetted the code, so
            # a runtime compile failure here is exceptional (e.g. a NaN-in-
            # rolling-window crash on a particular candle batch).
            try:
                user_signal_fn = build_strategy_signal_fn(
                    strategy_id     = self._strategy_id,
                    strategy_name   = self._strategy,
                    df              = df,
                    leverage        = self._leverage,
                    stoploss_pct    = abs(self._stoploss) * 100.0,
                    take_profit_pct = self._take_profit * 100.0,
                    pair            = pair,            # MTF analyzer
                    execution_tf    = self._timeframe, # MTF analyzer
                    overrides       = self._strategy_overrides,
                )
                # Successful compile — reset the breaker counter so transient
                # blips don't accumulate forever.
                self._consec_compile_failures = 0
            except StrategyCompileError as e:
                self._consec_compile_failures += 1
                self.last_action = (
                    f"STRATEGY COMPILE FAILED ({self._consec_compile_failures}/"
                    f"{self._max_compile_failures}): {e}"
                )
                self._log_action("strategy_compile_failed_tick",
                    self.last_action, pair=pair, error=str(e),
                    consecutive=self._consec_compile_failures,
                    threshold=self._max_compile_failures)
                log.warning("[%s] %s", self.user_id, self.last_action)
                # MUST-2: circuit breaker. After N consecutive failures the
                # strategy is almost certainly broken in a way that recovers
                # never — auto-stop so the action_log isn't endlessly spammed
                # and so no half-state position management leaks resources.
                if self._consec_compile_failures >= self._max_compile_failures:
                    self.last_action = (
                        f"BOT AUTO-STOPPED: {self._consec_compile_failures} consecutive "
                        f"strategy_runner failures — last error: {e}. "
                        "Edit the strategy code or re-upload, then restart this bot."
                    )
                    self._log_action("auto_stop_compile_failures",
                        self.last_action, error=str(e),
                        consecutive=self._consec_compile_failures)
                    log.error("[%s] %s", self.user_id, self.last_action)
                    _notifier.notify_compile_failed(
                        self.user_id, strategy=self._strategy, error=str(e),
                    )
                    # Persist the auto-stop to DB. Without this, the
                    # watchdog (in main.py) would keep resurrecting a
                    # bot whose strategy can never compile — infinite
                    # crash/restart loop with notification spam.
                    self._persist_auto_stop(reason="compile_failures")
                    # Tell the run loop to exit — bot transitions to !is_running
                    # which the UI surfaces in the active-bots list.
                    self._stop_evt.set()
                continue

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

            # ── Phase 7 — HTF bias filter (closed-candle policy) ─────────
            # When the strategy was authored for a different TF than the
            # engine's execution TF, the Timeframe Adapter asks us to gate
            # entries by HTF bias (PDF §5/§6). We do this AFTER signal
            # detection so we don't waste a candle fetch on every tick,
            # only when there's a candidate signal.
            strategy_class_tf = getattr(user_signal_fn, "diagnostics", {}).get("class_tf") if hasattr(user_signal_fn, "diagnostics") else None
            # Fall back to the strategy's declared timeframe via dataframe attrs.
            if not strategy_class_tf:
                strategy_class_tf = df.attrs.get("class_timeframe") if hasattr(df, "attrs") else None
            tf_bundle = timeframe_adapter.adapt(
                strategy_tf  = strategy_class_tf or self._timeframe,
                execution_tf = self._timeframe,
            )
            if tf_bundle.adapter_active and tf_bundle.bias_tfs:
                # Pick the smallest HTF that's > execution TF as the primary bias.
                primary_bias_tf = tf_bundle.bias_tfs[0]
                bias_long = mtf_candles.htf_bias_long(pair, primary_bias_tf, ema_period=200)
                if bias_long is not None:
                    # Block counter-trend trades (the #1 SMC loser pattern).
                    if direction == "long" and not bias_long:
                        self.last_action = (
                            f"BLOCKED long {pair} @ {live_price:.4f} — "
                            f"{primary_bias_tf} bias is bearish (price below EMA200)"
                        )
                        self._log_action("htf_bias_block", self.last_action,
                            pair=pair, direction=direction,
                            bias_tf=primary_bias_tf, bias_long=False)
                        log.info("[%s] %s", self.user_id, self.last_action)
                        continue
                    if direction == "short" and bias_long:
                        self.last_action = (
                            f"BLOCKED short {pair} @ {live_price:.4f} — "
                            f"{primary_bias_tf} bias is bullish (price above EMA200)"
                        )
                        self._log_action("htf_bias_block", self.last_action,
                            pair=pair, direction=direction,
                            bias_tf=primary_bias_tf, bias_long=True)
                        log.info("[%s] %s", self.user_id, self.last_action)
                        continue

            # ── Edge detection + stop-and-reverse ─────────────────────────
            # seen_signal[pair] now holds the DIRECTION of the last fired
            # signal (None | 'long' | 'short'). Skip if the same direction
            # already fired and we still hold the position.
            prev_dir = seen_signal.get(pair)
            if prev_dir == direction:
                # Same-direction repeat — don't fire again until the signal
                # flips or the position closes (the close path resets to None).
                continue

            # ── Phase 8: cooldown + max-trades-per-day + daily DD trip ────
            # Cooldown: skip if we're still inside the post-close window.
            cooldown_end = self._cooldown_until.get(pair, 0.0)
            if now_epoch < cooldown_end:
                remain = int(cooldown_end - now_epoch)
                self.last_action = (
                    f"COOLDOWN {pair} {direction} — {remain}s remaining of "
                    f"{self._cooldown_seconds // max(60,1)}m post-close cooldown"
                )
                self._log_action("cooldown_skip", self.last_action,
                    pair=pair, direction=direction, remaining_seconds=remain)
                continue

            # Rotate day-counter window at UTC midnight, capture start-of-day
            # balance for the DD calculation.
            day_now = now.strftime("%Y-%m-%d")
            if day_now != self._day_key:
                self._day_key            = day_now
                self._day_trades         = 0
                self._day_stops          = 0   # ← new: per-day stop-loss counter
                self._day_start_balance  = self.balance
                self._day_dd_tripped     = False
                self._log_action("day_rollover", f"New trading day {day_now} — counters reset",
                                 starting_balance=self._day_start_balance)

            # Max-trades-per-day cap.
            if self._day_trades >= self._max_trades_per_day:
                self.last_action = (
                    f"DAILY CAP {pair} {direction} — {self._day_trades}/"
                    f"{self._max_trades_per_day} trades already today"
                )
                self._log_action("daily_cap_skip", self.last_action,
                    pair=pair, direction=direction,
                    day_trades=self._day_trades,
                    max_trades_per_day=self._max_trades_per_day)
                continue

            # Max-stops-per-day breaker (optional; 0 = disabled).
            # When the strategy or user explicitly declares this gate, halt
            # all new entries once the day's stop-loss count reaches the
            # limit. Stricter than max_trades_per_day because it counts
            # ONLY stop-losses (signals of strategy failure) — a strategy
            # that's winning doesn't trip; one that's bleeding does.
            # Resets at the same UTC midnight rollover as _day_trades.
            if self._max_stops_per_day > 0 and self._day_stops >= self._max_stops_per_day:
                self.last_action = (
                    f"DAILY STOPS CAP {pair} {direction} — {self._day_stops}/"
                    f"{self._max_stops_per_day} stops already today, halting new entries"
                )
                self._log_action("daily_stops_cap_skip", self.last_action,
                    pair=pair, direction=direction,
                    day_stops=self._day_stops,
                    max_stops_per_day=self._max_stops_per_day)
                continue

            # Daily-DD breaker: trips the bot if today's loss exceeds the
            # threshold. Once tripped, no new entries are taken for the rest
            # of the UTC day (resets at midnight).
            if self._day_start_balance > 0:
                today_pnl_pct = (self.balance - self._day_start_balance) / self._day_start_balance * 100.0
                if today_pnl_pct <= -self._day_max_dd_pct:
                    if not self._day_dd_tripped:
                        self._day_dd_tripped = True
                        self.last_action = (
                            f"DAILY DD TRIPPED {today_pnl_pct:.2f}% (limit -{self._day_max_dd_pct}%) — "
                            f"no new entries until {day_now} UTC midnight"
                        )
                        self._log_action("daily_dd_trip", self.last_action,
                            day_pnl_pct=today_pnl_pct, limit=-self._day_max_dd_pct,
                            day_trades=self._day_trades)
                        log.warning("[%s] %s", self.user_id, self.last_action)
                        _notifier.notify_daily_dd_trip(
                            self.user_id, pnl_pct=today_pnl_pct,
                            limit_pct=self._day_max_dd_pct,
                            starting_balance=self._day_start_balance,
                            balance_now=self.balance,
                        )
                    continue
                if self._day_dd_tripped:
                    # Already tripped today — keep skipping new entries.
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
                    # Phase 8: don't trigger cooldown for stop-and-reverse —
                    # the new position opens RIGHT AFTER this close in the
                    # same tick. Cooldown only kicks in when a position
                    # closes WITHOUT an immediate replacement.

            entry = live_price

            # ── Phase 4 + NICE-4: timeframe-aware risk plan w/ overrides ─
            # Pass the strategy's structural SL/TP through risk_engine,
            # which (a) honours them if they validate, (b) falls back to
            # ATR×per-TF-multiplier defaults (with per-user overrides), and
            # (c) enforces a min-RR gate per the spec.
            user_risk_overrides = risk_engine.load_user_risk_overrides(self.user_id)
            plan = risk_engine.compute_tp_sl(
                entry          = entry,
                direction      = direction,
                df             = df,
                timeframe      = self._timeframe,
                strategy_sl    = sl_s,
                strategy_tp    = tp_s,
                strategy_tp2   = tp2_s,
                user_overrides = user_risk_overrides,
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

            # ── Op#10: live-only spread check ────────────────────────────
            # Reject entries when KuCoin's bid-ask spread eats > 25% of the
            # expected reward — common on altcoins during Asia session.
            expected_reward_pct = abs(tp - entry) / max(entry, 1e-9) * 100.0
            too_wide, spread_info = self._live_spread_too_wide(pair, expected_reward_pct)
            if too_wide:
                self.last_action = f"SPREAD BLOCK {pair} {direction} — {spread_info.get('reason')}"
                self._log_action("spread_block", self.last_action,
                    pair=pair, direction=direction, **spread_info)
                log.info("[%s] %s", self.user_id, self.last_action)
                continue

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

                # ── Phase 3 — configure ARM on the new position ─────────
                # The strategy's TP (plan.tp) becomes TP2; configure_arm
                # computes TP1 = midpoint(entry, tp2) and overrides pos.tp
                # so the position-management loop checks TP1 first.
                if self._arm_enabled and plan.tp is not None:
                    pos.configure_arm(
                        tp2_price         = plan.tp,
                        tp1_close_pct     = self._arm_tp1_close_pct,
                        arm_be_mode       = self._arm_be_mode,
                        arm_be_buffer_pct = self._arm_be_buffer_pct,
                        arm_trail_to_tp1  = self._arm_trail_to_tp1,
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
                # Phase 8: count this opening toward the daily cap.
                self._day_trades += 1
                # If ARM kicked in, surface TP1/TP2/closepct in the log
                # so the bot panel shows what the engine will actually do.
                arm_tag = ""
                if pos.arm_active:
                    arm_tag = (
                        f" ARM[TP1={pos.tp1_price:.4f} TP2={pos.tp2_price:.4f} "
                        f"close@TP1={pos.tp1_close_pct*100:.0f}% be={pos.arm_be_mode}]"
                    )
                self.last_action = (
                    f"OPENED futures {direction} {pair} @ {entry:.4f} "
                    f"{self._leverage}x liq={pos.liquidation_price:.4f} "
                    f"SL={pos.sl:.4f} TP={pos.tp:.4f} RR={plan.rr:.2f} "
                    f"[{plan.source}/{self._timeframe}]{arm_tag}"
                )
                self._log_action("opened", self.last_action,
                    pair=pair, price=entry, direction=direction,
                    leverage=self._leverage, sl=pos.sl, tp=pos.tp,
                    tp1=pos.tp1_price, tp2=pos.tp2_price,
                    arm_active=pos.arm_active,
                    arm_tp1_close_pct=pos.tp1_close_pct if pos.arm_active else None,
                    liquidation=pos.liquidation_price, stake=stake,
                    rr=plan.rr, atr=plan.atr, risk_source=plan.source,
                    timeframe=self._timeframe,
                    diagnostics=getattr(user_signal_fn, "diagnostics", None))
                log.info("[%s] %s", self.user_id, self.last_action)
                if self._mode == "live":
                    # Bug-fix: place the KuCoin order FIRST, then commit local
                    # state. On any rejection (insufficient margin, lot-size
                    # mismatch, leverage cap, transport error) we must
                    # remove the position from self.positions, refund the
                    # stake, and delete the open-trade DB row — otherwise
                    # the engine manages a phantom position that doesn't
                    # exist on KuCoin and ARM/TP/SL fire reduce-only orders
                    # which all fail.
                    ok, err = self._place_live_entry(pair, pos)
                    if not ok:
                        self.last_action = (
                            f"LIVE ENTRY REJECTED {pair} {direction} @ {entry:.4f}: {err}"
                        )
                        self._log_action("live_entry_rejected", self.last_action,
                            pair=pair, direction=direction, error=err,
                            stake=stake, leverage=self._leverage)
                        log.error("[%s] %s", self.user_id, self.last_action)
                        # Roll back: remove from positions, refund balance,
                        # delete the DB row (best-effort), reset signal flag
                        # so the next bar can re-attempt cleanly.
                        self.positions.pop(trade_key, None)
                        self.balance += stake
                        self._day_trades = max(0, self._day_trades - 1)
                        seen_signal[pair] = None
                        try:
                            from backend.models import SessionLocal as _SL
                            from backend.models.trade import Trade as _Trade
                            from sqlalchemy import select as _select
                            if getattr(pos, "db_id", None):
                                with _SL() as _db:
                                    row = _db.execute(
                                        _select(_Trade).where(_Trade.id == pos.db_id)
                                    ).scalar_one_or_none()
                                    if row:
                                        _db.delete(row)
                                        _db.commit()
                        except Exception:
                            pass
                        # No notification — the order didn't happen.
                        # Continue to next pair instead of falling through.
                        continue
                    # NICE-5: rewrite on-exchange TP/SL for the new position.
                    try:
                        from .kucoin_futures_client import normalize_futures_symbol
                        sym = normalize_futures_symbol(
                            pair.replace("/", "").replace("USDT", "USDTM"),
                        )
                        tp_to_push = pos.tp2_price if (pos.arm_active and pos.tp2_price) else pos.tp
                        self._push_live_tp_sl(sym, pos.sl, tp_to_push, label="open", pos=pos)
                    except Exception as _push_exc:
                        log.debug("[%s] TP/SL push on open skipped: %s",
                                  self.user_id, _push_exc)

                # NICE-7 — Discord notification (best-effort, async).
                _notifier.notify_position_opened(
                    self.user_id, pair=pair, direction=direction,
                    entry=entry, sl=pos.sl, tp=pos.tp, leverage=self._leverage,
                    strategy=self._strategy, mode=self._mode,
                    arm=pos.arm_active,
                    tp1=pos.tp1_price if pos.arm_active else None,
                    tp2=pos.tp2_price if pos.arm_active else None,
                )

                # Copy-trading broadcast removed in spot purge.

    # ── Live order placement via KuCoin Futures API ─────────────────────

    # ── KuCoin Lead Trading helpers — all order placements now route
    #     through _kucoin_lead_post() so response codes are checked
    #     uniformly and the order id (when present) is captured.

    def _kucoin_lead_post(self, body: dict, *, label: str) -> tuple[bool, dict, str]:
        """Send a signed POST to /api/v1/copy-trade/futures/orders and
        verify the response is a real success (HTTP 2xx **and** business
        code 200000).

        Returns (ok, data_dict, error_msg). On failure, error_msg carries
        either KuCoin's `msg` field or a transport-level exception string.

        Replaces the four ad-hoc try/except blocks in entry / exit /
        partial-close / push_tp_sl that silently swallowed HTTP and
        business-logic errors. Single source of truth so a phantom
        position (local-yes / KuCoin-no) can't happen on any path.
        """
        if self._mode != "live" or not self._api_key:
            return False, {}, "not in live mode"
        try:
            from .native_trading_engine import _kucoin_post_signed
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
        except Exception as e:
            log.error("[%s] Lead Trading %s transport error: %s",
                      self.user_id, label, e)
            return False, {}, f"transport: {e}"
        code = str((resp or {}).get("code", ""))
        if code != "200000":
            err = (resp or {}).get("msg") or f"KuCoin code {code}"
            log.error("[%s] Lead Trading %s REJECTED: %s (resp=%s)",
                      self.user_id, label, err, resp)
            return False, resp or {}, err
        log.info("[%s] Lead Trading %s ok: %s", self.user_id, label, resp.get("data"))
        return True, resp.get("data") or {}, ""

    def _place_live_entry(self, pair: str, pos) -> tuple[bool, str]:
        """Place a real futures market order for the entry. Returns
        (ok, error_msg). On failure the caller MUST roll back local state
        (remove pos from self.positions, refund stake to balance, delete
        the DB row) so the engine doesn't manage a phantom position."""
        if self._mode != "live" or not self._api_key:
            return True, ""   # paper mode — pretend success
        from .kucoin_futures_client import normalize_futures_symbol
        symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        side   = "buy" if pos.direction == "long" else "sell"
        position_side = "LONG" if pos.direction == "long" else "SHORT"
        # Bug-fix: per-symbol lot-size-aware contract count (was BTC-only ×1000).
        contracts   = _stake_to_contracts(pos.size, self._leverage, pos.entry, symbol)
        margin_mode = self.get_symbol_margin(symbol).upper() or "ISOLATED"
        client_oid  = f"atf-{int(time.time()*1000)}"
        body = {
            "clientOid":    client_oid,
            "side":          side,
            "symbol":        symbol,
            "type":          "market",
            "size":          contracts,
            "leverage":      self._leverage,
            "marginMode":    margin_mode,
            "positionSide":  position_side,
        }
        ok, data, err = self._kucoin_lead_post(body, label=f"ENTRY {pair} {side}")
        if ok:
            # Bug-fix: capture the exchange order id so cancel-all,
            # reconciliation and the partial-close path can find the order.
            order_id = data.get("orderId") or client_oid
            try:
                pos.exchange_order_id = str(order_id)
                pos.client_oid        = client_oid
            except Exception:
                pass
        return ok, err

    def tick_pending_orders_paper(self) -> int:
        """Fill any pending paper limit/stop orders whose trigger price has
        been reached. The main user engine never runs _run_loop, so without
        this paper limit orders would sit in Open Orders forever even after
        price crossed the limit — making the paper trading flow incomplete.

        LIVE pending orders are handled by KuCoin itself + the existing
        order-reconcile in /api/futures/orders, so we skip them here.

        Returns number of orders that filled this tick.
        """
        if not self._pending_orders:
            return 0
        # Snapshot the pairs we need prices for.
        pairs = list({o.symbol for o in self._pending_orders.values()})
        # _check_pending_orders takes a normalised "BTC/USDT" pair.
        # Engine pending orders store symbol in whatever format the
        # caller used (e.g. "BTCUSDTM"); normalise back.
        def _to_pair(sym: str) -> str:
            if "/" in sym:
                return sym
            if sym.endswith("USDTM"):
                base = sym[:-5].replace("XBT", "BTC")
                return f"{base}/USDT"
            if sym.endswith("USDT"):
                return f"{sym[:-4]}/USDT"
            return sym
        filled = 0
        seen_pairs = set()
        for sym in pairs:
            pair = _to_pair(sym)
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            live_price = self._get_live_price(pair)
            if live_price is None:
                continue
            before = len(self._pending_orders)
            try:
                self._check_pending_orders(pair, live_price)
            except Exception as exc:
                log.warning("[%s] tick_pending_orders_paper(%s) failed: %s",
                            self.user_id, pair, exc)
                continue
            after = len(self._pending_orders)
            if after < before:
                filled += (before - after)
        return filled

    def tick_manual_position_management(self) -> int:
        """Process liq + TP/SL exits for PAPER positions on this engine
        WITHOUT running the full signal-scan loop.

        Why this exists
        ---------------
        The MAIN user engine (futures_engine_registry.for_user) has no
        strategy configured and `_run_loop` is never started for it. So a
        position opened via /api/futures/manual-entry sits with sl/tp
        values on the FuturesPosition object but NO thread checking them.
        Liquidation, take-profit and stop-loss would never auto-trigger.

        This method does the minimal subset of _tick_continuous needed for
        manual positions: liquidation check + check_exit(). Skips ARM
        partial-close and stop-and-reverse — those are bot-only flows.

        Safety
        ------
        LIVE positions are deliberately SKIPPED — those must only close
        via a real KuCoin order, otherwise our local state would diverge
        from KuCoin (we'd think it's closed, KuCoin would still have
        margin locked). Live manual positions are protected by the TP/SL
        we pushed to KuCoin in /manual-entry (Bug B fix) plus the 30s
        reconcile from maybe_reconcile_live_positions.

        Returns the number of positions that were closed this tick.
        Safe to call when self.positions is empty (cheap no-op).
        """
        if not self.positions:
            return 0
        closed = 0
        now = datetime.now(timezone.utc)
        # Snapshot pairs so we don't iterate a dict that mutates under us.
        pairs = list({p.pair for p in self.positions.values()})
        for pair in pairs:
            live_price = self._get_live_price(pair)
            if live_price is None:
                continue
            with self._lock:
                # Re-fetch keys inside the lock — another thread (or this
                # method's own previous iteration) might have removed them.
                trade_keys = [k for k, p in self.positions.items() if p.pair == pair]
                for trade_key in trade_keys:
                    pos = self.positions.get(trade_key)
                    if pos is None:
                        continue
                    # Skip LIVE positions — only KuCoin should close those.
                    # The per-position _mode tag is authoritative; engine
                    # _mode is a misleading default for the main user engine.
                    pos_mode = getattr(pos, "_mode", self._mode)
                    if pos_mode != "paper":
                        continue
                    # 1. Liquidation
                    if isinstance(pos, FuturesPosition) and pos.check_liquidation(live_price):
                        pos.close(live_price, "liquidated", now)
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        del self.positions[trade_key]
                        self.last_action = (
                            f"LIQUIDATED (manual) {pair} @ {live_price:.4f} "
                            f"liq={pos.liquidation_price:.4f} P&L={pos.pnl_abs:+.2f}"
                        )
                        self._log_action("liquidated", self.last_action,
                            pair=pair, price=live_price, pnl=pos.pnl_abs,
                            direction=pos.direction)
                        log.warning("[%s] %s", self.user_id, self.last_action)
                        # Pass the per-position mode tag, not self._mode —
                        # the main user engine defaults to "paper" but the
                        # position was tagged at entry time with the user's
                        # actual choice.
                        _persist_closed_trade(self.user_id, pos, pos_mode,
                                              self._strategy_id, getattr(pos, "db_id", None))
                        closed += 1
                        continue
                    # 2. Standard TP/SL exit. Trail update is harmless on
                    # manual positions (they don't use trail config).
                    pos.update_trail(live_price)
                    exit_info = pos.check_exit(live_price, live_price)
                    if exit_info:
                        exit_price, reason = exit_info
                        pos.close(exit_price, reason, now)
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        del self.positions[trade_key]
                        self.last_action = (
                            f"EXIT (manual) {pair} {pos.direction.upper()} "
                            f"@ {exit_price:.4f} reason={reason} P&L={pos.pnl_abs:+.2f}"
                        )
                        self._log_action("manual_exit", self.last_action,
                            pair=pair, price=exit_price, reason=reason,
                            pnl=pos.pnl_abs, direction=pos.direction)
                        log.info("[%s] %s", self.user_id, self.last_action)
                        _persist_closed_trade(self.user_id, pos, pos_mode,
                                              self._strategy_id, getattr(pos, "db_id", None))
                        closed += 1
        return closed

    def maybe_reconcile_live_positions(self, *, throttle_secs: int = 30) -> bool:
        """Throttled wrapper around _reconcile_live_positions, safe to call
        from HTTP endpoints that don't have a run loop (e.g. the user-
        shared manual-trade engine where _run_loop isn't running).

        Returns True if a reconcile actually ran. Throttle protects against
        the /open endpoint's 8-second poll from hammering KuCoin's
        /positions endpoint."""
        if self._mode != "live" or not self._api_key:
            return False
        now = time.time()
        last = getattr(self, "_last_manual_reconcile_ts", 0.0)
        if now - last < throttle_secs:
            return False
        self._last_manual_reconcile_ts = now
        self._reconcile_live_positions()
        return True

    def _reconcile_live_positions(self) -> None:
        """Periodic check: drop local positions that no longer exist on
        KuCoin (manually closed via the exchange UI, or auto-closed due
        to liquidation we missed, or never opened because the entry order
        was rejected after we already optimistically wrote local state).

        Called from the run loop on the slow tick (~ once per minute).
        Live mode only; paper mode keeps the engine as source of truth.

        On a discovered mismatch:
          • Local position not on KuCoin → mark closed_reason='reconciled_drift'
            and remove from self.positions + close DB row.
          • Don't touch KuCoin-side positions that exist locally; the
            engine continues managing them normally.
        """
        if self._mode != "live" or not self._api_key or not self.positions:
            return
        try:
            from .native_trading_engine import _kucoin_get_signed
            from .kucoin_futures_client import normalize_futures_symbol
            resp = _kucoin_get_signed(
                "/api/v1/positions",
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            if str(resp.get("code")) != "200000":
                return
            # Build a set of (symbol, direction) tuples that KuCoin says
            # are open. Direction = sign of currentQty (positive = long).
            exchange_open: set[tuple[str, str]] = set()
            for p in resp.get("data") or []:
                qty = float(p.get("currentQty", 0) or 0)
                if qty == 0:
                    continue
                sym = p.get("symbol", "")
                exchange_open.add((sym, "long" if qty > 0 else "short"))
            # Walk local positions; drop any that KuCoin doesn't see.
            drift: list[str] = []
            with self._lock:
                for trade_key, pos in list(self.positions.items()):
                    sym = normalize_futures_symbol(
                        pos.pair.replace("/", "").replace("USDT", "USDTM"),
                    )
                    if (sym, pos.direction) not in exchange_open:
                        drift.append(trade_key)
                for trade_key in drift:
                    pos = self.positions.pop(trade_key, None)
                    if pos is None:
                        continue
                    # Use last-known price as the close fill; net P&L is
                    # whatever the bot saw before the drift. Persist so the
                    # trade history shows the reconciliation.
                    fill = self._last_prices.get(pos.pair, pos.entry)
                    try:
                        pos.close(fill, "reconciled_drift", datetime.now(timezone.utc))
                        self.balance += pos.pnl_abs
                        self.closed_trades.append(pos)
                        _persist_closed_trade(
                            self.user_id, pos, self._mode,
                            self._strategy_id, pos.db_id,
                        )
                    except Exception:
                        pass
                    self.last_action = (
                        f"RECONCILED-DRIFT {pos.pair} {pos.direction} — "
                        f"no on-exchange position; cleared local state."
                    )
                    self._log_action("reconciled_drift", self.last_action,
                        pair=pos.pair, direction=pos.direction)
                    log.warning("[%s] %s", self.user_id, self.last_action)
        except Exception as e:
            log.debug("[%s] reconcile_live_positions: %s", self.user_id, e)

    def _live_spread_too_wide(self, pair: str, expected_reward_pct: float) -> tuple[bool, dict]:
        """Op#10 — Spread / slippage gate.

        Read the current orderbook top-of-book from KuCoin Futures, compute
        the % spread, and reject the entry if it eats more than 25% of the
        expected reward. Returns (too_wide, info_dict).

        Backtester models slippage via SLIPPAGE_BPS_*; live needs this
        runtime check because actual spreads vary (especially on altcoins
        during low-liquidity hours).
        """
        if self._mode != "live":
            return False, {"reason": "spread check skipped — paper mode"}
        try:
            from .kucoin_futures_client import normalize_futures_symbol, KUCOIN_FUTURES_BASE
            from ._kucoin_proxy import urlopen as _proxy_urlopen
            import urllib.request as _ureq, json as _json
            symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
            url = f"{KUCOIN_FUTURES_BASE}/api/v1/level1/orderbook?symbol={symbol}"
            req = _ureq.Request(url)
            with _proxy_urlopen(req, timeout=5) as r:
                payload = _json.loads(r.read().decode())
            data = payload.get("data") or {}
            bid = float(data.get("bestBidPrice") or 0)
            ask = float(data.get("bestAskPrice") or 0)
            if bid <= 0 or ask <= 0 or ask <= bid:
                return False, {"reason": "spread check inconclusive — bad orderbook"}
            mid = (bid + ask) / 2.0
            spread_pct = (ask - bid) / mid * 100.0
            # Reject when half-spread (cost to cross) > 25% of expected reward.
            # half-spread × 2 (in + out) gives the round-trip cost.
            roundtrip_cost_pct = spread_pct
            if roundtrip_cost_pct > 0.25 * expected_reward_pct:
                return True, {
                    "spread_pct":            round(spread_pct, 4),
                    "expected_reward_pct":   round(expected_reward_pct, 4),
                    "roundtrip_cost_pct":    round(roundtrip_cost_pct, 4),
                    "reason": (
                        f"Spread {spread_pct:.3f}% eats >25% of expected "
                        f"reward {expected_reward_pct:.2f}% (round-trip)."
                    ),
                }
            return False, {"spread_pct": round(spread_pct, 4)}
        except Exception as e:
            log.debug("[%s] spread check failed (%s) — allowing entry",
                      self.user_id, e)
            return False, {"reason": f"spread check error: {e}"}

    def _place_live_partial_close(self, pair: str, pos, close_pct: float, fill_price: float) -> tuple[bool, str]:
        """LIVE-mode: send a reduce-only market order to close `close_pct`
        of the position via KuCoin Lead Trading. Returns (ok, error_msg).
        Mirrors the partial-close booked in the local position state."""
        if self._mode != "live" or not self._api_key or close_pct <= 0:
            return True, ""
        from .kucoin_futures_client import normalize_futures_symbol
        symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        side   = "sell" if pos.direction == "long" else "buy"
        position_side = "LONG" if pos.direction == "long" else "SHORT"
        # Bug-fix: per-symbol lot-size-aware contracts.
        full_contracts = _stake_to_contracts(pos.size, self._leverage, pos.entry, symbol)
        leg_contracts  = max(1, int(full_contracts * close_pct))
        margin_mode    = self.get_symbol_margin(symbol).upper() or "ISOLATED"
        body = {
            "clientOid":    f"atf-tp1-{int(time.time()*1000)}",
            "side":          side,
            "symbol":        symbol,
            "type":          "market",
            "size":          leg_contracts,
            "leverage":      self._leverage,
            "marginMode":    margin_mode,
            "positionSide":  position_side,
            "reduceOnly":    True,
        }
        ok, _, err = self._kucoin_lead_post(
            body, label=f"TP1 PARTIAL {pair} ({close_pct*100:.0f}%)",
        )
        if not ok:
            return False, err

        # ── Op#9 — rewrite on-exchange TP/SL after the partial ────────────
        if isinstance(pos, FuturesPosition) and pos.tp2_price is not None:
            self._push_live_tp_sl(symbol, pos.sl, pos.tp2_price, label="rewrite-after-tp1", pos=pos)
        return True, ""

    def _push_live_tp_sl(self, symbol: str, sl: float, tp: float, *, label: str = "rewrite",
                          pos=None) -> bool:
        """Push fresh SL + TP to KuCoin Lead Trading.

        Verified against KuCoin docs (2026-05-23 check):
          • Primary endpoint: POST /api/v1/copy-trade/futures/position/risk-limit/sl-tp
            with {symbol, stopLossPrice, takeProfitPrice} — modifies the
            position's built-in stops. Some lead-trading sub-accounts
            don't expose this; the response then is non-200000.
          • Documented fallback: POST /api/v1/copy-trade/futures/st-orders
            places TWO separate conditional stop orders (one with
            stopPriceType=TP, one with stopPriceType=SL) that fire when
            triggered. Slightly slower but works on every lead-trading
            account.

        We try the primary first; if KuCoin returns a non-200000 code,
        fall through to the st-orders path. Success on either is success
        overall. The `pos` arg gives access to direction + leverage for
        the st-orders body when fallback fires.
        """
        if self._mode != "live" or not self._api_key:
            return False
        # ── 1. PRIMARY — modify position SL/TP in place ──────────────────
        primary_ok = False
        try:
            from backend.services.kucoin_futures_client import _sign_request, KUCOIN_FUTURES_BASE as _base
            from backend.services._kucoin_proxy import urlopen as _proxy_urlopen
            import urllib.request as _ureq, json as _json
            body = {
                "symbol":          symbol,
                "stopLossPrice":   str(round(sl, 4)),
                "takeProfitPrice": str(round(tp, 4)),
            }
            body_str = _json.dumps(body)
            ts2  = str(int(time.time() * 1000))
            endpoint = "/api/v1/copy-trade/futures/position/risk-limit/sl-tp"
            headers = _sign_request(
                self._api_sec, self._api_pass, self._api_key,
                ts2, "POST", endpoint, body=body_str,
            )
            headers["Content-Type"] = "application/json"
            req2 = _ureq.Request(
                f"{_base}{endpoint}", data=body_str.encode(),
                headers=headers, method="POST",
            )
            with _proxy_urlopen(req2, timeout=15) as r2:
                resp = _json.loads(r2.read().decode())
            code = str(resp.get("code", ""))
            if code == "200000":
                log.info("[%s] Lead Trading TP/SL %s ok (primary)", self.user_id, label)
                return True
            log.info("[%s] TP/SL %s primary rejected (%s) — trying st-orders fallback",
                     self.user_id, label, resp.get("msg") or f"code {code}")
        except Exception as e:
            log.info("[%s] TP/SL %s primary transport error (%s) — trying st-orders fallback",
                     self.user_id, label, e)

        # ── 2. FALLBACK — place two conditional stop orders ──────────────
        # Documented endpoint per KuCoin's "Add Take Profit And Stop Loss
        # Order" copy-trading doc. Requires direction + leverage; if pos
        # wasn't passed in we can't run this fallback safely.
        if pos is None:
            log.warning("[%s] TP/SL %s — no pos passed, skipping st-orders fallback",
                        self.user_id, label)
            return False
        try:
            from .kucoin_futures_client import normalize_futures_symbol
            sym = normalize_futures_symbol(symbol)
            # For a LONG position: exit side is "sell"; SL trigger is BELOW entry,
            # TP trigger is ABOVE entry. For a SHORT position: exit side is "buy";
            # SL trigger is ABOVE entry, TP trigger is BELOW entry.
            exit_side    = "sell" if pos.direction == "long" else "buy"
            position_side = "LONG" if pos.direction == "long" else "SHORT"
            remaining_pct = getattr(pos, "remaining_pct", 1.0) or 1.0
            full_contracts = _stake_to_contracts(pos.size, self._leverage, pos.entry, sym)
            contracts      = max(1, int(full_contracts * remaining_pct))
            margin_mode    = self.get_symbol_margin(sym).upper() or "ISOLATED"
            common = {
                "symbol":        sym,
                "side":          exit_side,
                "type":          "market",
                "size":          contracts,
                "leverage":      self._leverage,
                "marginMode":    margin_mode,
                "positionSide":  position_side,
                "reduceOnly":    True,
            }
            sl_ok = self._post_st_order({
                **common,
                "clientOid":         f"atf-sl-{int(time.time()*1000)}",
                "stopPriceType":     "SL",
                "triggerStopDownPrice": str(round(sl, 4)) if pos.direction == "long" else "",
                "triggerStopUpPrice":   str(round(sl, 4)) if pos.direction == "short" else "",
            }, label=f"st-order SL {label}")
            tp_ok = self._post_st_order({
                **common,
                "clientOid":         f"atf-tp-{int(time.time()*1000)+1}",
                "stopPriceType":     "TP",
                "triggerStopUpPrice":   str(round(tp, 4)) if pos.direction == "long" else "",
                "triggerStopDownPrice": str(round(tp, 4)) if pos.direction == "short" else "",
            }, label=f"st-order TP {label}")
            return sl_ok and tp_ok
        except Exception as e2:
            log.warning("[%s] TP/SL %s st-orders fallback failed: %s",
                        self.user_id, label, e2)
            return False

    def _post_st_order(self, body: dict, *, label: str) -> bool:
        """Post a conditional stop order via the documented Copy Trading
        "Add Take Profit And Stop Loss Order" endpoint."""
        try:
            from .native_trading_engine import _kucoin_post_signed
            resp = _kucoin_post_signed(
                "/api/v1/copy-trade/futures/st-orders", body,
                self._api_key, self._api_sec, self._api_pass,
                base_url=KUCOIN_FUTURES_BASE,
            )
            code = str((resp or {}).get("code", ""))
            if code == "200000":
                log.info("[%s] st-order %s ok", self.user_id, label)
                return True
            log.warning("[%s] st-order %s rejected: %s",
                        self.user_id, label, (resp or {}).get("msg") or f"code {code}")
            return False
        except Exception as e:
            log.warning("[%s] st-order %s transport error: %s", self.user_id, label, e)
            return False

    def _place_live_exit(self, pair: str, pos, price: float) -> tuple[bool, str]:
        """Close a futures position via KuCoin Lead Trading API. Returns
        (ok, error_msg). ARM-aware sizing: only the REMAINING fraction is
        sent (closing the full original size after a TP1 partial would
        oversell and KuCoin would reject with insufficient-position)."""
        if self._mode != "live" or not self._api_key:
            return True, ""
        from .kucoin_futures_client import normalize_futures_symbol
        symbol = normalize_futures_symbol(pair.replace("/", "").replace("USDT", "USDTM"))
        side   = "sell" if pos.direction == "long" else "buy"
        position_side = "LONG" if pos.direction == "long" else "SHORT"
        remaining_pct = getattr(pos, "remaining_pct", 1.0) or 1.0
        # Bug-fix: per-symbol multiplier (was BTC-only ×1000).
        full_contracts = _stake_to_contracts(pos.size, self._leverage, pos.entry, symbol)
        contracts      = max(1, int(full_contracts * remaining_pct))
        margin_mode    = self.get_symbol_margin(symbol).upper() or "ISOLATED"
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
        ok, _, err = self._kucoin_lead_post(body, label=f"EXIT {pair} {side}")
        return ok, err

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
        mode: str = "paper",
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
                mode=mode,
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
        """Check and fill pending PAPER orders that match the current price.

        LIVE orders are deliberately skipped here — they're filled by
        KuCoin and surfaced via the order-reconcile path in
        /api/futures/orders. If we filled them locally too, the user
        would end up with both a phantom local position AND the real
        KuCoin position once it executes.
        """
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
                # Mode tag added when /order persisted the engine row.
                # Fallback to "paper" for orders created before the tag
                # existed (in-memory only — wiped on next restart).
                if getattr(order, "mode", "paper") != "paper":
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
        base["paused"]        = self._paused
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
                # Position.close() writes `exit_reason`, not `reason` — the
                # old key broke status() with AttributeError, which cascaded
                # into the Bot panel "Recovery failed" error and made every
                # bot disappear from the UI.
                "reason": getattr(t, "exit_reason", None),
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
