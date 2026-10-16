from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Iterable, Sequence

import httpx

from polyarb.book import OrderBook
from polyarb.fastjson import loads

CLOB_URL = "https://clob.polymarket.com"
BOOKS_BATCH = 500
log = logging.getLogger(__name__)


class RateLimiter:
    """Token bucket shared by concurrent requests (Cloudflare limit for /books is 500 req / 10 s)."""

    def __init__(self, rate_per_s: float, burst: int | None = None) -> None:
        self.rate = rate_per_s
        self.capacity = float(burst or max(1, int(rate_per_s)))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return
                await asyncio.sleep((1.0 - self.tokens) / self.rate)


class ClobClient:
    """Public CLOB market-data client (order books)."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        base_url: str = CLOB_URL,
        books_rps: float = 30.0,
        concurrency: int = 8,
        timeout: float = 15.0,
        max_retries: int = 4,
    ) -> None:
        self._own = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout)
        self._base = base_url.rstrip("/")
        self._limiter = RateLimiter(books_rps)
        self._sem = asyncio.Semaphore(concurrency)
        self._max_retries = max_retries

    async def __aenter__(self) -> "ClobClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._http.aclose()

    async def _post_books(self, token_ids: Sequence[str]) -> list[dict]:
        delay = 0.5
        for attempt in range(self._max_retries):
            await self._limiter.acquire()
            try:
                async with self._sem:
                    resp = await self._http.post(
                        f"{self._base}/books", json=[{"token_id": t} for t in token_ids]
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                data = loads(resp.content)
                return data if isinstance(data, list) else []
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    log.warning("books request rejected (%s); dropping %d tokens", status, len(token_ids))
                    return []
                if attempt == self._max_retries - 1:
                    log.warning("books request failed after retries: %s", exc)
                    return []
                await asyncio.sleep(delay)
                delay = min(delay * 2, 8.0)
        return []

    async def get_books(
        self, token_ids: Iterable[str], skip: Callable[[str, str | None], bool] | None = None
    ) -> dict[str, OrderBook]:
        """Fetch books in batches of 500; ``skip(token, server_hash)`` returning True leaves an entry unparsed."""
        ids = list(dict.fromkeys(token_ids))
        books: dict[str, OrderBook] = {}

        async def fetch(chunk: list[str]) -> None:
            # Parse each batch as it lands: a full sweep decoded at once is several hundred MB of dicts.
            for raw in await self._post_books(chunk):
                if not isinstance(raw, dict):
                    continue
                token = raw.get("asset_id")
                if not token or (skip is not None and skip(str(token), raw.get("hash"))):
                    continue
                book = OrderBook.from_api(raw)
                books[book.token_id] = book

        await asyncio.gather(*(fetch(ids[i:i + BOOKS_BATCH]) for i in range(0, len(ids), BOOKS_BATCH)))
        return books

    async def get_book(self, token_id: str) -> OrderBook | None:
        books = await self.get_books([token_id])
        return books.get(token_id)
