from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest

from polyarb.book import OrderBook
from polyarb.data.store import BookStore
from polyarb.models import FEE_FREE, Event, FeeSchedule, Market

_ids = itertools.count(1)
NOW = datetime(2026, 10, 16, tzinfo=timezone.utc)
MARKET_ID = "0x" + "ab" * 31 + "00"


def make_market(
    question: str = "Will it happen?",
    group: str = "",
    fee: FeeSchedule = FEE_FREE,
    neg_risk: bool = False,
    index: int | None = None,
    end: datetime | None = None,
    tick: float = 0.01,
    min_size: float = 5.0,
    accepting: bool = True,
    closed: bool = False,
    resolved: str | None = None,
    other: bool = False,
    delay: int = 0,
) -> Market:
    n = next(_ids)
    qid = None
    if neg_risk and index is not None:
        qid = "0x" + format(int(MARKET_ID, 16) | index, "064x")
    return Market(
        id=str(n),
        condition_id="0x" + format(n, "064x"),
        question=question,
        slug=f"m{n}",
        yes_token=f"Y{n}",
        no_token=f"N{n}",
        outcomes=("Yes", "No"),
        group_title=group,
        fee=fee,
        tick_size=tick,
        min_size=min_size,
        end_date=end or NOW + timedelta(days=10),
        neg_risk=neg_risk,
        neg_risk_market_id=MARKET_ID if neg_risk else None,
        question_id=qid,
        neg_risk_other=other,
        accepting_orders=accepting,
        version="v1",
        closed=closed,
        resolved_outcome=resolved,
        seconds_delay=delay,
    )


def make_event(markets: list[Market], neg_risk: bool = False, augmented: bool = False, title: str = "Event") -> Event:
    return Event(
        id=str(next(_ids)),
        slug="event",
        title=title,
        markets=markets,
        neg_risk=neg_risk,
        neg_risk_augmented=augmented,
        neg_risk_market_id=MARKET_ID if neg_risk else None,
        end_date=max((m.end_date for m in markets if m.end_date), default=None),
        version="v1",
    )


def book(token: str, bids=(), asks=()) -> OrderBook:
    return OrderBook(token, bids=bids, asks=asks)


@pytest.fixture
def now_ts() -> float:
    return NOW.timestamp()


@pytest.fixture
def store() -> BookStore:
    return BookStore(max_age_s=1e9)
