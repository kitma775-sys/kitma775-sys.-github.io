from __future__ import annotations

import random
from typing import Sequence

from polyarb.book import EPS, Level, OrderBook, spend
from polyarb.data.store import BookStore
from polyarb.execution.base import LegResult, SellResult
from polyarb.fees import fee_per_share
from polyarb.models import FEE_FREE, FeeSchedule, Leg, Outcome


def _buy_levels(book: OrderBook, outcome: Outcome) -> list[Level]:
    return book.asks if outcome is Outcome.YES else book.no_asks()


def _sell_levels(book: OrderBook, outcome: Outcome) -> list[Level]:
    return book.bids if outcome is Outcome.YES else book.no_bids()


def _consume(book: OrderBook, outcome: Outcome, buying: bool, shares: float, limit: float) -> None:
    """Remove the liquidity a simulated fill took so the same size cannot be traded twice."""
    levels = _buy_levels(book, outcome) if buying else _sell_levels(book, outcome)
    # YES buys lift asks, NO buys hit YES bids, YES sells hit bids, NO sells lift YES asks.
    yes_side = "SELL" if (buying == (outcome is Outcome.YES)) else "BUY"
    remaining = shares
    for lvl in levels:
        if remaining <= EPS or (buying and lvl.price > limit + EPS) or (not buying and lvl.price < limit - EPS):
            break
        q = min(remaining, lvl.size)
        yes_price = lvl.price if outcome is Outcome.YES else round(1.0 - lvl.price, 6)
        book.set_level(yes_side, yes_price, lvl.size - q)
        remaining -= q


class PaperGateway:
    """Fills orders against the live books held in a BookStore with exchange semantics.

    BUY = market FOK for ``leg.notional`` pUSD capped at ``limit_price``; if the
    book can no longer absorb the full amount the leg is rejected ("unmatched"),
    exactly like the CLOB would. ``fail_prob`` injects random leg misses to
    exercise the partial-fill / unwind paths.
    """

    mode = "paper"

    def __init__(self, books: BookStore, fail_prob: float = 0.0, seed: int | None = None) -> None:
        self.books = books
        self.fail_prob = fail_prob
        self.rng = random.Random(seed)
        self.converted: list[tuple[str, int, float]] = []

    async def buy(self, legs: Sequence[Leg], slippage_ticks: int = 0) -> list[LegResult]:
        results = []
        for leg in legs:
            book = self.books.raw(leg.yes_token or leg.token_id)
            if book is None:
                results.append(LegResult(leg, False, status="rejected", error="no book"))
                continue
            limit = min(leg.limit_price + slippage_ticks * leg.tick_size, 1.0 - leg.tick_size)
            fill = spend(_buy_levels(book, leg.outcome), leg.notional, limit, leg.fee_schedule)
            if fill.notional < leg.notional - 1e-6:
                results.append(LegResult(leg, False, status="unmatched", error="FOK not fillable at limit"))
                continue
            if self.fail_prob and self.rng.random() < self.fail_prob:
                results.append(LegResult(leg, False, status="unmatched", error="simulated miss"))
                continue
            _consume(book, leg.outcome, True, fill.shares, limit)
            results.append(LegResult(leg, True, shares=fill.shares, spent=fill.notional, fee=fill.fee, status="matched", order_id="paper"))
        return results

    async def sell(
        self,
        token_id: str,
        outcome: Outcome,
        yes_token: str,
        shares: float,
        min_price: float,
        fee: FeeSchedule = FEE_FREE,
        tick: float = 0.01,
    ) -> SellResult:
        book = self.books.raw(yes_token)
        if book is None:
            return SellResult(token_id, outcome, False, error="no book")
        remaining = shares
        proceeds = 0.0
        fees = 0.0
        for lvl in _sell_levels(book, outcome):
            if remaining <= EPS or lvl.price < min_price - EPS:
                break
            q = min(remaining, lvl.size)
            proceeds += q * lvl.price
            fees += q * fee_per_share(fee, lvl.price)
            remaining -= q
        sold = shares - max(remaining, 0.0)
        if sold <= EPS:
            return SellResult(token_id, outcome, False, error="no bids at limit")
        _consume(book, outcome, False, sold, min_price)
        return SellResult(token_id, outcome, True, shares=sold, proceeds=proceeds, fee=fees)

    async def convert(self, neg_risk_market_id: str, index_set: int, shares: float) -> tuple[bool, str | None, float]:
        n = bin(index_set).count("1")
        self.converted.append((neg_risk_market_id, index_set, shares))
        return True, "paper-convert", (n - 1) * shares

    async def close(self) -> None:
        return None
