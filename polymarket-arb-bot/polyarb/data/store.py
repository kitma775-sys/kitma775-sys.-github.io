from __future__ import annotations

import time
from typing import Iterator, Mapping

from polyarb.book import OrderBook


class BookStore(Mapping[str, OrderBook]):
    """Latest book per YES token, fed by REST sweeps and websocket deltas.

    Books older than ``max_age_s`` are hidden from strategies so a stalled feed
    can never produce a phantom opportunity.
    """

    def __init__(self, max_age_s: float = 20.0) -> None:
        self.max_age_s = max_age_s
        self._books: dict[str, OrderBook] = {}
        self.resolved: set[str] = set()

    def __getitem__(self, token_id: str) -> OrderBook:
        book = self._books[token_id]
        if token_id in self.resolved or book.age() > self.max_age_s:
            raise KeyError(token_id)
        return book

    def __iter__(self) -> Iterator[str]:
        return iter(self._books)

    def __len__(self) -> int:
        return len(self._books)

    def raw(self, token_id: str) -> OrderBook | None:
        return self._books.get(token_id)

    def put(self, book: OrderBook) -> None:
        self._books[book.token_id] = book

    def update_many(self, books: Mapping[str, OrderBook]) -> None:
        self._books.update(books)

    def touch(self, token_id: str) -> None:
        book = self._books.get(token_id)
        if book is not None:
            book.received_at = time.time()

    def stats(self) -> dict:
        now = time.time()
        ages = [b.age(now) for b in self._books.values()]
        fresh = sum(1 for a in ages if a <= self.max_age_s)
        return {"books": len(ages), "fresh": fresh, "resolved": len(self.resolved)}
