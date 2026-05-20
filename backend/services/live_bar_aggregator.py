"""
Live tick → OHLCV bar aggregator.

The scalp engine needs a continuously-updated OHLC representation of
recent price action so strategy signal functions (which all operate on
DataFrames of bars) can be re-evaluated as new bars close. This module
converts the raw tick stream from the KuCoin WebSocket into bars at the
caller's desired timeframe (1m, 5m, 15m, etc).

Design choices:
  • Bars are bucketed by floor(tick_ts / tf_secs) * tf_secs to align
    with KuCoin's REST kline boundaries — sub-bar from WS is the SAME
    bar as the eventual REST candle for that minute, so backtest <->
    live parity is preserved.
  • Each instance handles ONE (symbol, timeframe) pair. Multi-symbol /
    multi-TF setups should hold a dict[(symbol, tf)] -> Aggregator.
  • Bounded history buffer (default 500 bars) — enough for any
    indicator the strategies use (longest is EMA200 + buffer).
  • on_bar_close callback fires only when a bar genuinely closes, so
    strategy evaluation doesn't run mid-bar.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

log = logging.getLogger(__name__)


TF_SECS_MAP = {
    "1m":  60,    "3m":  180,   "5m":  300,
    "15m": 900,   "30m": 1800,
    "1h":  3600,  "2h":  7200,  "4h":  14400,
    "6h":  21600, "8h":  28800, "12h": 43200,
    "1d":  86400,
}


@dataclass
class Bar:
    """One OHLCV candle keyed by its CLOSE timestamp (epoch seconds).

    Matches the shape of bars returned by load_futures_ohlcv() so the
    same DataFrame transforms work on live and backtest data without
    branching.
    """
    ts_close: int        # epoch seconds at the bar's close
    open:  float
    high:  float
    low:   float
    close: float
    volume: float = 0.0
    # tick_count is live-only — backtest bars don't have this.
    tick_count: int = 0


# Callback fires when a bar finishes (NOT mid-bar). Receives the closed
# Bar object. Used by the scalp engine to trigger strategy evaluation.
OnBarClose = Callable[[Bar], Awaitable[None]]


class LiveBarAggregator:
    """Aggregates a tick stream into OHLCV bars at a fixed timeframe.

    Usage:
        agg = LiveBarAggregator(symbol="XBTUSDTM", timeframe="1m")
        agg.on_close(my_async_handler)

        async def handle_tick(msg):
            d = msg.get("data") or {}
            price = float(d.get("price", 0))
            ts_ms = int(d.get("ts", time.time() * 1_000_000_000)) // 1_000_000
            await agg.feed_tick(price, ts_ms)

        ws_client.on_topic(f"/contractMarket/tickerV2:{symbol}", handle_tick)
    """

    def __init__(self, symbol: str, timeframe: str, history_size: int = 500):
        self.symbol = symbol
        self.timeframe = timeframe
        self.tf_secs = TF_SECS_MAP.get(timeframe)
        if self.tf_secs is None:
            raise ValueError(f"unsupported timeframe: {timeframe}")
        self._current_bar: Optional[Bar] = None
        self._closed_bars: deque[Bar] = deque(maxlen=history_size)
        self._handlers: list[OnBarClose] = []
        # Stats
        self.stats = {
            "ticks_processed": 0,
            "bars_closed":     0,
            "last_tick_price": 0.0,
            "last_tick_ts_ms": 0,
        }

    def on_close(self, handler: OnBarClose) -> None:
        """Register an async callback fired when a bar CLOSES.

        Multiple handlers allowed; called in registration order. If a
        handler raises, the exception is logged but other handlers
        still run.
        """
        self._handlers.append(handler)

    async def feed_tick(self, price: float, ts_ms: int) -> None:
        """Consume one tick. Updates current bar's high/low/close; if
        the tick belongs to a NEW bar bucket, closes the prior bar and
        starts a new one (firing on_close handlers for the closed bar).
        """
        if price <= 0:
            return
        ts_secs = ts_ms // 1000
        bucket_start = (ts_secs // self.tf_secs) * self.tf_secs
        bucket_close = bucket_start + self.tf_secs

        self.stats["ticks_processed"] += 1
        self.stats["last_tick_price"] = price
        self.stats["last_tick_ts_ms"] = ts_ms

        cur = self._current_bar
        if cur is None or cur.ts_close != bucket_close:
            # New bar bucket — close the previous one (if any) and start fresh.
            if cur is not None:
                self._closed_bars.append(cur)
                self.stats["bars_closed"] += 1
                # Fire all on_close handlers for the just-closed bar.
                for h in self._handlers:
                    try:
                        await h(cur)
                    except Exception:
                        log.exception("on_close handler error")
            self._current_bar = Bar(
                ts_close   = bucket_close,
                open       = price,
                high       = price,
                low        = price,
                close      = price,
                volume     = 0.0,
                tick_count = 1,
            )
        else:
            # Same bar — update high/low/close, increment tick count.
            cur.close = price
            if price > cur.high:
                cur.high = price
            if price < cur.low:
                cur.low = price
            cur.tick_count += 1

    # ─────────────────── Read-only accessors ─────────────────────────────

    def closed_bars(self) -> list[Bar]:
        """Snapshot of all closed bars in this aggregator's history."""
        return list(self._closed_bars)

    def current_bar(self) -> Optional[Bar]:
        """The bar currently being built (mid-bar). Read-only snapshot.

        For strategy evaluation, prefer iterating closed_bars() — using
        the current (partial) bar would expose your strategy to look-
        ahead bias since the bar isn't done.
        """
        return self._current_bar

    def as_dataframe(self):
        """Return all closed bars as a pandas DataFrame matching the
        shape produced by native_backtester.load_futures_ohlcv().

        Same column names + dtypes, so any strategy code that runs on
        backtest data runs on this output without modification.
        """
        import pandas as pd
        bars = self._closed_bars
        if not bars:
            return pd.DataFrame(columns=["date", "open", "high", "low", "close", "vol"])
        df = pd.DataFrame({
            "date":  pd.to_datetime([b.ts_close for b in bars], unit="s", utc=True),
            "open":  [b.open for b in bars],
            "high":  [b.high for b in bars],
            "low":   [b.low for b in bars],
            "close": [b.close for b in bars],
            "vol":   [b.volume for b in bars],
        })
        return df
