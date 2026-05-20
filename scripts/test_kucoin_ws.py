"""
Standalone test for the KuCoin Futures WebSocket client + bar aggregator.

Run this BEFORE wiring the WS layer into anything else — confirms:
  • Token fetch works (REST POST /api/v1/bullet-public succeeds)
  • WS connection opens + welcome message received
  • Subscription gets server ack
  • Ticks flow in at the expected rate (~1-10/sec on liquid BTC perp)
  • Bar aggregator builds 1m candles correctly from the tick stream
  • Reconnection works (kill internet briefly to test)

Usage:
    cd C:\\Users\\Ankit\\Desktop\\tradebot
    python -m scripts.test_kucoin_ws
        OR
    python -m scripts.test_kucoin_ws --symbol XBTUSDTM --duration 120

Press Ctrl+C to stop. Prints a summary at the end.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import time

from backend.services.kucoin_ws_futures import KuCoinFuturesWSClient
from backend.services.live_bar_aggregator import LiveBarAggregator


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
)
log = logging.getLogger("ws-test")


async def main(symbol: str, duration: int):
    client = KuCoinFuturesWSClient()
    agg = LiveBarAggregator(symbol=symbol, timeframe="1m", history_size=200)
    last_print = {"ts": 0.0}

    # Tick handler — extract price+ts from the message and feed the aggregator.
    async def on_tick(msg):
        data = msg.get("data") or {}
        # tickerV2 fields: bestBidPrice, bestAskPrice, ts (nanoseconds), price (sometimes)
        price_str = data.get("price") or data.get("bestBidPrice") or data.get("bestAskPrice")
        if price_str is None:
            return
        try:
            price = float(price_str)
        except (TypeError, ValueError):
            return
        # ts is nanoseconds since epoch in KuCoin futures stream.
        ts_ns = int(data.get("ts", time.time_ns()))
        ts_ms = ts_ns // 1_000_000
        await agg.feed_tick(price, ts_ms)

        # Print a status line every 5 seconds so the user can see life.
        now = time.time()
        if now - last_print["ts"] > 5:
            last_print["ts"] = now
            cur = agg.current_bar()
            print(
                f"[{symbol}] price={price:>10.2f}  ticks={client.stats['ticks_received']:>6}  "
                f"closed_bars={len(agg.closed_bars()):>3}  "
                f"current_bar_ticks={cur.tick_count if cur else 0:>4}  "
                f"reconnects={client.stats['reconnects']}"
            )

    # Bar close handler — log every completed 1m bar.
    async def on_bar(bar):
        print(
            f"  BAR CLOSED {symbol} ts={bar.ts_close}  "
            f"OHLC={bar.open:.2f}/{bar.high:.2f}/{bar.low:.2f}/{bar.close:.2f}  "
            f"ticks={bar.tick_count}"
        )

    agg.on_close(on_bar)

    topic = f"/contractMarket/tickerV2:{symbol}"
    client.on_topic(topic, on_tick)

    # SIGINT handler — clean shutdown
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    def _sigint(*_):
        print("\n  ↳ Ctrl+C received, shutting down...")
        stop.set()
    try:
        loop.add_signal_handler(signal.SIGINT, _sigint)
    except NotImplementedError:
        # Windows doesn't support add_signal_handler for SIGINT in some
        # asyncio setups — fall back to KeyboardInterrupt around run().
        pass

    log.info("connecting to KuCoin Futures WS...")
    await client.connect()
    log.info("connected — subscribing to %s", topic)
    await client.subscribe(topic)
    log.info("subscribed; running for %ds (or Ctrl+C)", duration)

    # Run the WS loop in the background; wait for either the duration
    # to elapse or Ctrl+C.
    run_task = asyncio.create_task(client.run())
    try:
        await asyncio.wait_for(stop.wait(), timeout=duration)
    except asyncio.TimeoutError:
        pass
    client.stop()
    try:
        await asyncio.wait_for(run_task, timeout=5)
    except asyncio.TimeoutError:
        run_task.cancel()
    await client.disconnect()

    # Summary
    print("\n──── Summary ──────────────────────────────────────────")
    print(f"WS stats:        {client.stats}")
    print(f"Aggregator stats: {agg.stats}")
    print(f"Closed bars:     {len(agg.closed_bars())}")
    bars = agg.closed_bars()
    if bars:
        print(f"First bar: {bars[0]}")
        print(f"Last bar:  {bars[-1]}")
    print("──────────────────────────────────────────────────────")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbol",   default="XBTUSDTM",
                        help="KuCoin futures symbol (default: XBTUSDTM = BTC perp)")
    parser.add_argument("--duration", type=int, default=120,
                        help="how long to run, in seconds (default: 120)")
    args = parser.parse_args()
    try:
        asyncio.run(main(args.symbol, args.duration))
    except KeyboardInterrupt:
        pass
