from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Sequence

import websockets

from polyarb.book import OrderBook
from polyarb.data.store import BookStore

WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
log = logging.getLogger(__name__)


class MarketStream:
    """Market-channel websocket fan-out that keeps a BookStore current.

    The feed only pushes changes, so while a connection is healthy (PONG
    received) its books are re-stamped as fresh; if it stalls they age out of
    the BookStore and strategies stop seeing them.
    """

    def __init__(
        self,
        store: BookStore,
        on_change: Callable[[str], None] | None = None,
        url: str = WS_URL,
        assets_per_conn: int = 400,
        ping_s: float = 10.0,
        stale_s: float = 35.0,
    ) -> None:
        self.store = store
        self.on_change = on_change
        self.url = url
        self.assets_per_conn = assets_per_conn
        self.ping_s = ping_s
        self.stale_s = stale_s
        self._tasks: list[asyncio.Task] = []
        self.tokens: tuple[str, ...] = ()
        self.messages = 0
        self.reconnects = 0

    @property
    def running(self) -> bool:
        return any(not t.done() for t in self._tasks)

    async def start(self, token_ids: Sequence[str]) -> None:
        await self.stop()
        self.tokens = tuple(dict.fromkeys(token_ids))
        chunks = [self.tokens[i:i + self.assets_per_conn] for i in range(0, len(self.tokens), self.assets_per_conn)]
        self._tasks = [asyncio.create_task(self._run(list(c), n)) for n, c in enumerate(chunks)]

    async def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks = []

    async def _run(self, tokens: list[str], n: int) -> None:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=None, max_size=2**24, open_timeout=20) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    backoff = 1.0
                    last_pong = time.monotonic()

                    async def pinger() -> None:
                        while True:
                            await asyncio.sleep(self.ping_s)
                            if time.monotonic() - last_pong > self.stale_s:
                                log.warning("ws[%d] heartbeat lost; reconnecting", n)
                                await ws.close()
                                return
                            await ws.send("PING")

                    ping_task = asyncio.create_task(pinger())
                    try:
                        async for msg in ws:
                            if msg == "PONG":
                                last_pong = time.monotonic()
                                for t in tokens:
                                    self.store.touch(t)
                                continue
                            self.handle(msg)
                    finally:
                        ping_task.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors, server closes
                log.warning("ws[%d] error: %s", n, exc)
            self.reconnects += 1
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def handle(self, msg: str | bytes) -> set[str]:
        try:
            data = json.loads(msg)
        except ValueError:
            return set()
        items = data if isinstance(data, list) else [data]
        changed: set[str] = set()
        for ev in items:
            if not isinstance(ev, dict):
                continue
            self.messages += 1
            kind = ev.get("event_type")
            if kind == "book":
                book = OrderBook.from_api(ev)
                self.store.put(book)
                changed.add(book.token_id)
            elif kind == "price_change":
                for ch in ev.get("price_changes") or []:
                    aid = str(ch.get("asset_id"))
                    book = self.store.raw(aid)
                    if book is None:
                        continue
                    book.set_level(str(ch.get("side", "")), ch.get("price", 0), ch.get("size", 0))
                    changed.add(aid)
            elif kind == "tick_size_change":
                book = self.store.raw(str(ev.get("asset_id")))
                if book is not None and ev.get("new_tick_size"):
                    book.tick_size = float(ev["new_tick_size"])
            elif kind == "market_resolved":
                self.store.resolved.update(str(a) for a in ev.get("assets_ids") or [])
        if self.on_change:
            for aid in changed:
                self.on_change(aid)
        return changed
