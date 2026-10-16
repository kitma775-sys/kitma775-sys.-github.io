from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime, timezone


class Side(str, enum.Enum):
    BUY = "BUY"
    SELL = "SELL"


class Outcome(str, enum.Enum):
    YES = "YES"
    NO = "NO"


class Settlement(str, enum.Enum):
    """How an opportunity turns positions back into collateral."""

    CONVERT = "convert"        # NegRisk NO-basket -> pUSD immediately on-chain
    RESOLUTION = "resolution"  # hold until the markets resolve


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Polymarket taker fee: fee = shares * rate * (p * (1 - p)) ** exponent, paid in pUSD."""

    rate: float = 0.0
    exponent: float = 1.0
    taker_only: bool = True
    rebate_rate: float = 0.0

    @property
    def free(self) -> bool:
        return self.rate <= 0.0


FEE_FREE = FeeSchedule()


@dataclass(slots=True)
class Market:
    id: str
    condition_id: str
    question: str
    slug: str
    yes_token: str
    no_token: str
    outcomes: tuple[str, str]
    group_title: str
    fee: FeeSchedule
    tick_size: float
    min_size: float
    end_date: datetime | None
    neg_risk: bool
    neg_risk_market_id: str | None
    question_id: str | None
    neg_risk_other: bool
    accepting_orders: bool
    version: str
    closed: bool = False
    resolved_outcome: str | None = None
    seconds_delay: int = 0
    volume_24h: float = 0.0
    liquidity: float = 0.0
    fee_type: str = ""

    @property
    def is_yes_no(self) -> bool:
        return self.outcomes[0].strip().lower() == "yes" and self.outcomes[1].strip().lower() == "no"

    @property
    def neg_risk_index(self) -> int | None:
        """Question index inside a v1 NegRisk market: questionId = marketId | index."""
        if not (self.neg_risk_market_id and self.question_id):
            return None
        try:
            idx = int(self.question_id, 16) ^ int(self.neg_risk_market_id, 16)
        except ValueError:
            return None
        return idx if 0 <= idx < 256 else None

    def token(self, outcome: Outcome) -> str:
        return self.yes_token if outcome is Outcome.YES else self.no_token


@dataclass(slots=True)
class Event:
    id: str
    slug: str
    title: str
    markets: list[Market]
    neg_risk: bool
    neg_risk_augmented: bool
    neg_risk_market_id: str | None
    end_date: datetime | None
    version: str
    tags: tuple[str, ...] = ()
    cumulative: bool | None = None
    sort_by: str | None = None

    @property
    def tradeable_markets(self) -> list[Market]:
        return [m for m in self.markets if m.accepting_orders]


@dataclass(frozen=True, slots=True)
class Leg:
    """One order of an opportunity. Prices refer to the token being traded."""

    token_id: str
    market_id: str
    outcome: Outcome
    side: Side
    shares: float
    notional: float      # sum(price * size) over the book levels consumed
    fee: float
    limit_price: float   # worst level touched; becomes max_price / min_price on the order
    tick_size: float
    min_size: float
    neg_risk: bool
    label: str
    yes_token: str = ""
    fee_schedule: FeeSchedule = FEE_FREE
    neg_risk_index: int | None = None

    @property
    def avg_price(self) -> float:
        return self.notional / self.shares if self.shares > 0 else 0.0

    @property
    def cash(self) -> float:
        """Signed pUSD flow of this leg (negative = spend)."""
        if self.side is Side.BUY:
            return -(self.notional + self.fee)
        return self.notional - self.fee


@dataclass(slots=True)
class Opportunity:
    strategy: str
    key: str
    event_id: str
    title: str
    legs: list[Leg]
    shares: float
    cost: float
    payout_floor: float
    settlement: Settlement
    risk: str
    resolves_at: datetime | None
    detected_at: float
    meta: dict = field(default_factory=dict)

    @property
    def profit(self) -> float:
        return self.payout_floor - self.cost

    @property
    def edge(self) -> float:
        return self.profit / self.cost if self.cost > 0 else 0.0

    @property
    def fees(self) -> float:
        return sum(leg.fee for leg in self.legs)

    def days_to_resolution(self, now: datetime | None = None) -> float | None:
        if self.settlement is Settlement.CONVERT:
            return 0.0
        if self.resolves_at is None:
            return None
        now = now or datetime.now(timezone.utc)
        return max((self.resolves_at - now).total_seconds() / 86400.0, 0.0)

    def apr(self, now: datetime | None = None, min_days: float = 1.0) -> float | None:
        """Simple annualised return; capital is locked until resolution."""
        days = self.days_to_resolution(now)
        if days is None or self.cost <= 0:
            return None
        if self.settlement is Settlement.CONVERT:
            return None
        return self.edge * 365.0 / max(days, min_days)

    def summary(self) -> dict:
        return {
            "strategy": self.strategy,
            "key": self.key,
            "event_id": self.event_id,
            "title": self.title,
            "shares": round(self.shares, 2),
            "cost": round(self.cost, 4),
            "payout_floor": round(self.payout_floor, 4),
            "profit": round(self.profit, 4),
            "edge_pct": round(100 * self.edge, 3),
            "fees": round(self.fees, 4),
            "settlement": self.settlement.value,
            "risk": self.risk,
            "days": None if (d := self.days_to_resolution()) is None else round(d, 2),
            "apr_pct": None if (a := self.apr()) is None else round(100 * a, 1),
            "legs": [
                {
                    "label": leg.label,
                    "outcome": leg.outcome.value,
                    "side": leg.side.value,
                    "shares": round(leg.shares, 2),
                    "avg": round(leg.avg_price, 4),
                    "limit": leg.limit_price,
                    "fee": round(leg.fee, 5),
                }
                for leg in self.legs
            ],
            **({"meta": self.meta} if self.meta else {}),
        }
