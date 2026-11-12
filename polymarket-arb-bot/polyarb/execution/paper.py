from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Iterable, Sequence

from polyarb.book import EPS, Level, OrderBook, spend
from polyarb.data.store import BookStore
from polyarb.execution.base import LegResult, LimitOrderResult, OrderState, SellResult
from polyarb.fees import fee_per_share
from polyarb.models import FEE_FREE, FeeSchedule, Leg, Market, Outcome, Side


@dataclass
class _PaperOrder:
    order_id: str
    token_id: str
    outcome: Outcome
    yes_token: str
    side: Side
    price: float
    original_size: float
    size_matched: float = 0.0
    status: str = "LIVE"


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
        self.redeemed: list[str] = []
        self._orders: dict[str, _PaperOrder] = {}
        self._seq = 0

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

    async def place_limit(
        self,
        token_id: str,
        outcome: Outcome,
        yes_token: str,
        side: Side,
        price: float,
        shares: float,
        tick: float = 0.01,
        post_only: bool = True,
    ) -> LimitOrderResult:
        qty = math.floor(shares * 100 + 1e-7) / 100
        if qty <= 0:
            return LimitOrderResult(False, status="dust", error="dust")
        book = self.books.raw(yes_token)
        if post_only and book is not None:
            if side is Side.BUY:
                opp_levels = _buy_levels(book, outcome)
                if opp_levels and opp_levels[0].price <= price + EPS:
                    return LimitOrderResult(False, status="post_only_would_cross", error="crosses book")
            else:
                opp_levels = _sell_levels(book, outcome)
                if opp_levels and opp_levels[0].price >= price - EPS:
                    return LimitOrderResult(False, status="post_only_would_cross", error="crosses book")
        self._seq += 1
        oid = f"paper-limit-{self._seq}"
        self._orders[oid] = _PaperOrder(oid, token_id, outcome, yes_token, side, price, qty)
        return LimitOrderResult(True, order_id=oid, status="live", matched_shares=0.0)

    def _sweep_resting(self, order: _PaperOrder) -> None:
        if order.status != "LIVE":
            return
        book = self.books.raw(order.yes_token)
        if book is None:
            return
        rem = order.original_size - order.size_matched
        if rem <= EPS:
            order.status = "MATCHED"
            return
        if order.side is Side.BUY:
            avail = sum(lvl.size for lvl in _buy_levels(book, order.outcome) if lvl.price <= order.price + EPS)
            if avail > EPS:
                fill_q = min(rem, avail)
                _consume(book, order.outcome, True, fill_q, order.price)
                order.size_matched = round(order.size_matched + fill_q, 4)
        else:
            avail = sum(lvl.size for lvl in _sell_levels(book, order.outcome) if lvl.price >= order.price - EPS)
            if avail > EPS:
                fill_q = min(rem, avail)
                _consume(book, order.outcome, False, fill_q, order.price)
                order.size_matched = round(order.size_matched + fill_q, 4)
        if order.original_size - order.size_matched <= EPS:
            order.size_matched = order.original_size
            order.status = "MATCHED"

    async def get_order_state(self, order_id: str) -> OrderState | None:
        order = self._orders.get(order_id)
        if order is None:
            return None
        self._sweep_resting(order)
        return OrderState(order.order_id, order.status, order.original_size, order.size_matched, order.price)

    async def cancel(self, order_id: str) -> bool:
        order = self._orders.get(order_id)
        if order is None:
            return False
        if order.status == "LIVE":
            order.status = "CANCELED"
        return True

    async def redeem(self, condition_id: str) -> tuple[bool, str | None]:
        self.redeemed.append(condition_id)
        return True, "paper-redeem"

    def warm_markets(self, markets: Iterable[Market]) -> int:
        return 0

    async def close(self) -> None:
        return None
