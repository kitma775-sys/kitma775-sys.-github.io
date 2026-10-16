from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping, Protocol

from polyarb.book import Fill, OrderBook
from polyarb.models import Event, Leg, Market, Opportunity, Outcome, Side

Books = Mapping[str, OrderBook]


@dataclass(frozen=True, slots=True)
class Target:
    """A group of markets one strategy evaluates together; ``tokens`` are the YES books it needs."""

    key: str
    strategy: str
    event: Event
    tokens: tuple[str, ...]
    payload: Any = None


class Strategy(Protocol):
    name: str
    enabled: bool

    def targets(self, events: Iterable[Event]) -> list[Target]: ...

    def evaluate(self, target: Target, books: Books, now: float) -> list[Opportunity]: ...

    def top_edge(self, target: Target, books: Books) -> float | None:
        """Per-unit profit at the top of book ignoring depth and gates (diagnostics / hot-set ranking)."""


def buy_leg(market: Market, outcome: Outcome, fill: Fill, label: str | None = None) -> Leg:
    return Leg(
        token_id=market.token(outcome),
        market_id=market.id,
        outcome=outcome,
        side=Side.BUY,
        shares=fill.shares,
        notional=fill.notional,
        fee=fill.fee,
        limit_price=fill.worst_price,
        tick_size=market.tick_size,
        min_size=market.min_size,
        neg_risk=market.neg_risk,
        label=label or market.group_title or market.question[:70],
        yes_token=market.yes_token,
        fee_schedule=market.fee,
        neg_risk_index=market.neg_risk_index,
    )


def utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz=timezone.utc)
