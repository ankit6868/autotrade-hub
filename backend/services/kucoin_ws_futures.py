"""
KuCoin Futures WebSocket client — public market data streams.

Handles the parts of the WS protocol that change rarely:
  • Bullet-public token fetch (REST POST /api/v1/bullet-public)
  • WS connection setup with token + connectId
  • Welcome message exchange
  • Periodic ping (KuCoin requires every ~18s; server replies pong)
  • Subscription / unsubscription to topics
  • Message dispatch to per-topic handlers
  • Automatic reconnection on disconnect with exponential backoff

Used by the paper-scalp engine and the tick collector. NOT used for
authenticated streams (orders, balance) — those need bullet-private
and the API key/secret, which we'll add in 2B.3 when we wire up
post-only order placement.

Reference: https://docs.kucoin.com/futures/#websocket-feed
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

log = logging.getLogger(__name__)


KUCOIN_FUTURES_REST = "https://api-futures.kucoin.com"
BULLET_PUBLIC_ENDPOINT = "/api/v1/bullet-public"

# KuCoin requires a ping at least every 18s; sending every 15s gives buffer.
PING_INTERVAL_SECS = 15

# Reconnection backoff (seconds). Caps at 30s so we don't go silent for
# minutes during a long outage.
RECONNECT_BACKOFF = [1, 2, 5, 10, 15, 30]


# Type alias for tick / message handler callbacks. Async callables that
# receive the parsed message dict.
MessageHandler = Callable[[dict], Awaitable[None]]


class KuCoinFuturesWSClient:
    """Public-stream WebSocket client for KuCoin Futures.

    Usage (asyncio):
        client = KuCoinFuturesWSClient()
        client.on_topic("/contractMarket/tickerV2:XBTUSDTM", handle_tick)
        async with client:
            await client.subscribe("/contractMarket/tickerV2:XBTUSDTM")
            await client.run()   # blocks until client.stop() is called
    """

    def __init__(self):
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._endpoint: str = ""
        self._token: str = ""
        self._ping_interval: int = PING_INTERVAL_SECS
        self._connect_id: str = uuid.uuid4().hex
        # Topic → list of handlers. A topic can have multiple handlers.
        self._handlers: dict[str, list[MessageHandler]] = {}
        # Topics we've subscribed to — re-subscribed automatically after
        # a reconnect.
        self._subscribed_topics: set[str] = set()
        self._stop_event: asyncio.Event = asyncio.Event()
        # Pending ack futures keyed by request id, resolved when the
        # server confirms subscription / unsubscription.
        self._pending_acks: dict[str, asyncio.Future] = {}
        # Stats — exposed via stats() for monitoring/diagnostics
        self.stats = {
            "ticks_received": 0,
            "reconnects":     0,
            "subscribe_acks": 0,
            "ping_count":     0,
            "last_tick_ts":   0.0,
            "connected_at":   0.0,
            "errors":         0,
        }

    # ─────────────────── Connection lifecycle ───────────────────────────

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *exc):
        await self.disconnect()

    async def connect(self) -> None:
        """Fetch bullet-public token, then open WS connection.

        Idempotent — calling again while connected is a no-op.
        """
        if self._ws is not None:
            return
        # REST POST → returns {token, instanceServers: [{endpoint, pingInterval}]}
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(KUCOIN_FUTURES_REST + BULLET_PUBLIC_ENDPOINT)
            resp.raise_for_status()
            data = resp.json()
        if str(data.get("code")) != "200000":
            raise RuntimeError(f"bullet-public failed: {data}")
        d = data["data"]
        servers = d.get("instanceServers") or []
        if not servers:
            raise RuntimeError("bullet-public returned no instance servers")
        self._endpoint = servers[0]["endpoint"]
        self._token = d["token"]
        # Server tells us how often to ping; respect it but cap to our
        # default (don't ping more than every 15s — wasteful — or less
        # than KuCoin's max of 18s — disconnect).
        srv_ping_ms = int(servers[0].get("pingInterval", 18000))
        self._ping_interval = min(15, max(5, srv_ping_ms // 1000 - 3))

        url = f"{self._endpoint}?token={self._token}&connectId={self._connect_id}"
        self._ws = await websockets.connect(
            url,
            ping_interval=None,   # we handle KuCoin's app-level ping ourselves
            close_timeout=5,
            max_size=2_000_000,   # 2 MB safety margin for orderbook snapshots
        )
        # First message from server is "welcome" — wait for it.
        welcome = await asyncio.wait_for(self._ws.recv(), timeout=10)
        wmsg = json.loads(welcome)
        if wmsg.get("type") != "welcome":
            raise RuntimeError(f"unexpected welcome message: {wmsg}")
        self.stats["connected_at"] = time.time()
        log.info("KuCoin Futures WS connected (id=%s)", self._connect_id)

    async def disconnect(self) -> None:
        """Close the WS cleanly. Safe to call multiple times."""
        self._stop_event.set()
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

    # ─────────────────── Subscription ────────────────────────────────────

    def on_topic(self, topic: str, handler: MessageHandler) -> None:
        """Register a handler for messages on a topic.

        Multiple handlers per topic are allowed (called in registration
        order). Handlers must be async.
        """
        self._handlers.setdefault(topic, []).append(handler)

    async def subscribe(self, topic: str, private: bool = False) -> None:
        """Subscribe to a topic. Awaits the server ack.

        Example topic: "/contractMarket/tickerV2:XBTUSDTM"
        """
        if self._ws is None:
            raise RuntimeError("not connected — call connect() first")
        req_id = uuid.uuid4().hex
        fut: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending_acks[req_id] = fut
        msg = {
            "id": req_id,
            "type": "subscribe",
            "topic": topic,
            "privateChannel": bool(private),
            "response": True,    # we want an ack
        }
        await self._ws.send(json.dumps(msg))
        try:
            await asyncio.wait_for(fut, timeout=10)
        except asyncio.TimeoutError:
            self._pending_acks.pop(req_id, None)
            raise RuntimeError(f"subscribe ack timeout for topic {topic}")
        self._subscribed_topics.add(topic)
        self.stats["subscribe_acks"] += 1
        log.info("WS subscribed: %s", topic)

    # ─────────────────── Main message loop ───────────────────────────────

    async def run(self) -> None:
        """Run the receive + ping loops until stop() is called.

        On disconnect: tries to reconnect with exponential backoff and
        re-subscribes to all previously-subscribed topics.
        """
        while not self._stop_event.is_set():
            try:
                if self._ws is None:
                    await self.connect()
                    # Re-subscribe to topics from before disconnect.
                    for t in list(self._subscribed_topics):
                        try:
                            # Don't double-add to the set — already in it.
                            self._subscribed_topics.discard(t)
                            await self.subscribe(t)
                        except Exception:
                            log.exception("re-subscribe failed for %s", t)
                # Run receive + ping concurrently.
                receive_task = asyncio.create_task(self._receive_loop())
                ping_task    = asyncio.create_task(self._ping_loop())
                done, pending = await asyncio.wait(
                    [receive_task, ping_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for t in pending:
                    t.cancel()
                # If we got here, one task finished — connection issue.
                self._ws = None
                if self._stop_event.is_set():
                    return
            except (ConnectionClosed, OSError, asyncio.TimeoutError) as e:
                log.warning("WS connection lost: %s — reconnecting", e)
                self._ws = None
                self.stats["errors"] += 1
            except Exception:
                log.exception("WS run loop unexpected error")
                self.stats["errors"] += 1
                self._ws = None
            if self._stop_event.is_set():
                return
            # Backoff before reconnect
            backoff = RECONNECT_BACKOFF[
                min(self.stats["reconnects"], len(RECONNECT_BACKOFF) - 1)
            ]
            self.stats["reconnects"] += 1
            await asyncio.sleep(backoff)

    def stop(self) -> None:
        """Request the run loop to exit cleanly. Idempotent."""
        self._stop_event.set()

    # ─────────────────── Internal loops ──────────────────────────────────

    async def _receive_loop(self) -> None:
        """Read messages from WS, dispatch to handlers."""
        assert self._ws is not None
        async for raw in self._ws:
            try:
                msg = json.loads(raw)
            except json.JSONDecodeError:
                self.stats["errors"] += 1
                continue
            mtype = msg.get("type")
            # Server acks for subscribe/unsubscribe
            if mtype == "ack":
                req_id = msg.get("id")
                fut = self._pending_acks.pop(req_id, None) if req_id else None
                if fut and not fut.done():
                    fut.set_result(True)
                continue
            # Server pong
            if mtype == "pong":
                continue
            # Server error
            if mtype == "error":
                log.error("WS server error: %s", msg)
                self.stats["errors"] += 1
                # Resolve any pending ack with the error so subscribe()
                # doesn't hang.
                req_id = msg.get("id")
                fut = self._pending_acks.pop(req_id, None) if req_id else None
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(str(msg)))
                continue
            # Data messages — dispatch to topic handlers
            if mtype == "message":
                topic = msg.get("topic", "")
                self.stats["ticks_received"] += 1
                self.stats["last_tick_ts"] = time.time()
                # Match handlers: exact topic OR topic prefix (e.g.
                # "/contractMarket/tickerV2:XBTUSDTM" handlers can be
                # registered for the exact topic).
                for registered_topic, handlers in self._handlers.items():
                    if registered_topic == topic:
                        for h in handlers:
                            try:
                                await h(msg)
                            except Exception:
                                log.exception("handler error for %s", topic)
                                self.stats["errors"] += 1

    async def _ping_loop(self) -> None:
        """Send periodic ping to keep connection alive."""
        assert self._ws is not None
        while True:
            await asyncio.sleep(self._ping_interval)
            try:
                await self._ws.send(json.dumps({
                    "id":   uuid.uuid4().hex,
                    "type": "ping",
                }))
                self.stats["ping_count"] += 1
            except Exception:
                # Receive loop will detect the broken connection on its
                # next read; let it handle reconnect.
                return
