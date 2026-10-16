from __future__ import annotations

import time
from bisect import bisect_left
from dataclasses import dataclass
from functools import partial
from typing import Iterable, Mapping, NamedTuple, Sequence

from polyarb.fees import fee_per_share
from polyarb.models import FeeSchedule

EPS = 1e-9
PRICE_DECIMALS = 6
TOP_LEVELS = 3  # depth that decides whether a book change can alter an opportunity worth re-evaluating
_CACHE_LIMIT = 200_000
_PRICES: dict[float | str, float] = {}
_MIRRORS: dict[float, float] = {}


def _p(price: float | str) -> float:
    """Canonical float for a price; interned because books repeat a few thousand tick prices millions of times."""
    value = _PRICES.get(price)
    if value is None:
        value = round(float(price), PRICE_DECIMALS)
        if len(_PRICES) < _CACHE_LIMIT:
            _PRICES[price] = value
    return value


def _mirror(price: float) -> float:
    value = _MIRRORS.get(price)
    if value is None:
        value = round(1.0 - price, PRICE_DECIMALS)
        if len(_MIRRORS) < _CACHE_LIMIT:
            _MIRRORS[price] = value
    return value


class Level(NamedTuple):
    price: float
    size: float


_level = partial(tuple.__new__, Level)


def _neg_price(level: Level) -> float:
    return -level[0]


def _price_of(level: Level) -> float:
    return level[0]


def _side(rows: Iterable[tuple[float | str, float | str]], descending: bool) -> list[Level]:
    by_price: dict[float, float] = {}
    for price, size in rows:
        by_price[_p(price)] = float(size)
    return [_level(item) for item in sorted(by_price.items(), reverse=descending) if item[1] > 0]


def _api_side(rows: Sequence[Mapping], descending: bool) -> list[Level]:
    """Fast path for exchange payloads (unique prices, positive sizes); anything else goes through ``_side``."""
    cached = _PRICES.get
    levels = [_level((cached(lvl["price"]) or _p(lvl["price"]), float(lvl["size"]))) for lvl in rows]
    levels.sort(reverse=descending)
    previous = None
    for price, size in levels:
        if price == previous or size <= 0:
            return _side(((lvl["price"], lvl["size"]) for lvl in rows), descending)
        previous = price
    return levels


class OrderBook:
    """L2 book for one outcome token.

    Polymarket runs a single book per binary market: the NO book is the price
    mirror of the YES book (NO ask = 1 - YES bid). Strategies therefore only
    need YES books and derive NO liquidity with ``no_asks``/``no_bids``.

    Level lists are replaced, never mutated, so a ladder handed to a strategy
    or a simulated fill stays a consistent snapshot while updates arrive.
    """

    __slots__ = ("token_id", "_bids", "_asks", "_no_asks", "_no_bids", "tick_size", "timestamp", "received_at", "hash")

    def __init__(
        self,
        token_id: str,
        bids: Iterable[tuple[float | str, float | str]] = (),
        asks: Iterable[tuple[float | str, float | str]] = (),
        tick_size: float | None = None,
        timestamp: float = 0.0,
        hash: str | None = None,
    ) -> None:
        self.token_id = token_id
        self.tick_size = tick_size
        self.timestamp = timestamp
        self.replace(bids, asks)
        self.hash = hash

    @classmethod
    def from_api(cls, payload: Mapping) -> "OrderBook":
        """Build from a CLOB REST ``/book(s)`` entry or a websocket ``book`` event."""
        book = cls.__new__(cls)
        book.token_id = str(payload.get("asset_id"))
        tick = payload.get("tick_size")
        book.tick_size = float(tick) if tick not in (None, "") else None
        ts = float(payload.get("timestamp") or 0)
        book.timestamp = ts / 1000.0 if ts > 1e11 else ts
        book._bids = _api_side(payload.get("bids") or (), descending=True)
        book._asks = _api_side(payload.get("asks") or (), descending=False)
        book._no_asks = None
        book._no_bids = None
        book.hash = payload.get("hash") or None
        book.received_at = time.time()
        return book

    def replace(self, bids: Iterable[tuple[float | str, float | str]], asks: Iterable[tuple[float | str, float | str]]) -> None:
        self._bids = _side(bids, descending=True)
        self._asks = _side(asks, descending=False)
        self._no_asks = None
        self._no_bids = None
        self.hash = None
        self.received_at = time.time()

    def set_level(self, side: str, price: float | str, size: float | str) -> bool:
        """Apply an absolute level update (websocket ``price_change``); size 0 deletes.

        Returns whether the update landed within the top ``TOP_LEVELS`` of its side.
        """
        is_bid = side.upper() in ("BUY", "BID", "BIDS")
        levels = list(self._bids if is_bid else self._asks)
        key = _p(price)
        qty = float(size)
        i = bisect_left(levels, -key, key=_neg_price) if is_bid else bisect_left(levels, key, key=_price_of)
        found = i < len(levels) and levels[i][0] == key
        if qty > 0:
            if found:
                levels[i] = Level(key, qty)
            else:
                levels.insert(i, Level(key, qty))
        elif found:
            del levels[i]
        if is_bid:
            self._bids = levels
            self._no_asks = None
        else:
            self._asks = levels
            self._no_bids = None
        self.hash = None
        self.received_at = time.time()
        return i < TOP_LEVELS

    @property
    def bids(self) -> list[Level]:
        return self._bids

    @property
    def asks(self) -> list[Level]:
        return self._asks

    @property
    def best_bid(self) -> float | None:
        return self._bids[0][0] if self._bids else None

    @property
    def best_ask(self) -> float | None:
        return self._asks[0][0] if self._asks else None

    def no_asks(self) -> list[Level]:
        if self._no_asks is None:
            cached = _MIRRORS.get
            self._no_asks = [_level((cached(p) or _mirror(p), s)) for p, s in self._bids]
        return self._no_asks

    def no_bids(self) -> list[Level]:
        if self._no_bids is None:
            cached = _MIRRORS.get
            self._no_bids = [_level((cached(p) or _mirror(p), s)) for p, s in self._asks]
        return self._no_bids

    def age(self, now: float | None = None) -> float:
        return (now or time.time()) - self.received_at

    def fingerprint(self, depth: int = TOP_LEVELS) -> tuple:
        return tuple(self._bids[:depth]), tuple(self._asks[:depth])

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
