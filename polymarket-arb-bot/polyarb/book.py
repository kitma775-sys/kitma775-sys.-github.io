from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from polyarb.fees import fee_per_share
from polyarb.models import FeeSchedule

EPS = 1e-9
PRICE_DECIMALS = 6


def _p(price: float | str) -> float:
    return round(float(price), PRICE_DECIMALS)


@dataclass(frozen=True, slots=True)
class Level:
    price: float
    size: float


class OrderBook:
    """L2 book for one outcome token.

    Polymarket runs a single book per binary market: the NO book is the price
    mirror of the YES book (NO ask = 1 - YES bid). Strategies therefore only
    need YES books and derive NO liquidity with ``no_asks``/``no_bids``.
    """

    __slots__ = ("token_id", "_bids", "_asks", "_bid_cache", "_ask_cache", "tick_size", "timestamp", "received_at")

    def __init__(
        self,
        token_id: str,
        bids: Iterable[tuple[float, float]] = (),
        asks: Iterable[tuple[float, float]] = (),
        tick_size: float | None = None,
        timestamp: float = 0.0,
    ) -> None:
        self.token_id = token_id
        self._bids: dict[float, float] = {}
        self._asks: dict[float, float] = {}
        self._bid_cache: list[Level] | None = None
        self._ask_cache: list[Level] | None = None
        self.tick_size = tick_size
        self.timestamp = timestamp
        self.received_at = time.time()
        self.replace(bids, asks)

    @classmethod
    def from_api(cls, payload: Mapping) -> "OrderBook":
        """Build from a CLOB REST ``/book(s)`` entry or a websocket ``book`` event."""
        bids = [(lvl["price"], lvl["size"]) for lvl in payload.get("bids") or []]
        asks = [(lvl["price"], lvl["size"]) for lvl in payload.get("asks") or []]
        tick = payload.get("tick_size")
        ts = payload.get("timestamp") or 0
        return cls(
            str(payload.get("asset_id")),
            bids,
            asks,
            tick_size=float(tick) if tick not in (None, "") else None,
            timestamp=float(ts) / 1000.0 if float(ts) > 1e11 else float(ts),
        )

    def replace(self, bids: Iterable[tuple[float, float]], asks: Iterable[tuple[float, float]]) -> None:
        self._bids = {}
        self._asks = {}
        for price, size in bids:
            if float(size) > 0:
                self._bids[_p(price)] = float(size)
        for price, size in asks:
            if float(size) > 0:
                self._asks[_p(price)] = float(size)
        self._bid_cache = None
        self._ask_cache = None
        self.received_at = time.time()

    def set_level(self, side: str, price: float | str, size: float | str) -> None:
        """Apply an absolute level update (websocket ``price_change``); size 0 deletes."""
        book = self._bids if side.upper() in ("BUY", "BID", "BIDS") else self._asks
        key = _p(price)
        qty = float(size)
        if qty > 0:
            book[key] = qty
        else:
            book.pop(key, None)
        if book is self._bids:
            self._bid_cache = None
        else:
            self._ask_cache = None
        self.received_at = time.time()

    @property
    def bids(self) -> list[Level]:
        if self._bid_cache is None:
            self._bid_cache = [Level(p, s) for p, s in sorted(self._bids.items(), reverse=True)]
        return self._bid_cache

    @property
    def asks(self) -> list[Level]:
        if self._ask_cache is None:
            self._ask_cache = [Level(p, s) for p, s in sorted(self._asks.items())]
        return self._ask_cache

    @property
    def best_bid(self) -> float | None:
        bids = self.bids
        return bids[0].price if bids else None

    @property
    def best_ask(self) -> float | None:
        asks = self.asks
        return asks[0].price if asks else None

    def no_asks(self) -> list[Level]:
        return [Level(_p(1.0 - lvl.price), lvl.size) for lvl in self.bids]

    def no_bids(self) -> list[Level]:
        return [Level(_p(1.0 - lvl.price), lvl.size) for lvl in self.asks]

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at

    def __repr__(self) -> str:
        return f"OrderBook({self.token_id[:10]}… bid={self.best_bid} ask={self.best_ask})"


@dataclass(frozen=True, slots=True)
class Fill:
    shares: float
    notional: float
    fee: float
    worst_price: float
    levels: int

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares if self.shares > 0 else 0.0


def take(levels: Sequence[Level], shares: float, fee: FeeSchedule) -> Fill:
    """Walk ``levels`` (best first) for up to ``shares``; returns what the book can supply."""
    remaining = shares
    notional = 0.0
    fees = 0.0
    worst = 0.0
    used = 0
    for lvl in levels:
        if remaining <= EPS:
            break
        q = min(remaining, lvl.size)
        notional += q * lvl.price
        fees += q * fee_per_share(fee, lvl.price)
        worst = lvl.price
        remaining -= q
        used += 1
    return Fill(shares - max(remaining, 0.0), notional, fees, worst, used)


def spend(levels: Sequence[Level], amount: float, max_price: float, fee: FeeSchedule) -> Fill:
    """Simulate a market BUY of ``amount`` pUSD bounded by ``max_price`` (exchange semantics)."""
    remaining = amount
    shares = 0.0
    notional = 0.0
    fees = 0.0
    worst = 0.0
    used = 0
    for lvl in levels:
        if remaining <= EPS or lvl.price > max_price + EPS:
            break
        q = min(lvl.size, remaining / lvl.price)
        shares += q
        notional += q * lvl.price
        fees += q * fee_per_share(fee, lvl.price)
        worst = lvl.price
        remaining -= q * lvl.price
        used += 1
    return Fill(shares, notional, fees, worst, used)


def depth(levels: Sequence[Level], limit_price: float, is_ask: bool = True) -> float:
    total = 0.0
    for lvl in levels:
        if (is_ask and lvl.price > limit_price + EPS) or (not is_ask and lvl.price < limit_price - EPS):
            break
        total += lvl.size
    return total
