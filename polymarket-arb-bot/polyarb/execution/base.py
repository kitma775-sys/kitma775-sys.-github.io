from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from polyarb.models import FEE_FREE, FeeSchedule, Leg, Opportunity, Outcome


@dataclass
class LegResult:
    leg: Leg
    ok: bool
    shares: float = 0.0
    spent: float = 0.0
    fee: float = 0.0
    order_id: str | None = None
    status: str = ""
    error: str | None = None

    @property
    def cost(self) -> float:
        return self.spent + self.fee


@dataclass
class SellResult:
    token_id: str
    outcome: Outcome
    ok: bool
    shares: float = 0.0
    proceeds: float = 0.0
    fee: float = 0.0
    error: str | None = None

    @property
    def net(self) -> float:
        return self.proceeds - self.fee


@dataclass
class ExecutionReport:
    opp: Opportunity
    mode: str
    started_at: float
    finished_at: float = 0.0
    sent: bool = False
    complete: bool = False
    legs: list[LegResult] = field(default_factory=list)
    retries: list[LegResult] = field(default_factory=list)
    sells: list[SellResult] = field(default_factory=list)
    settled_shares: float = 0.0
    convert_tx: str | None = None
    proceeds: float = 0.0
    realized_pnl: float = 0.0
    locked_cost: float = 0.0
    expected_payout: float = 0.0
    orphaned: bool = False
    action: str = ""
    error: str | None = None

    @property
    def spent(self) -> float:
        return sum(r.cost for r in self.legs + self.retries if r.ok)

    def summary(self) -> dict:
        return {
            "mode": self.mode,
            "strategy": self.opp.strategy,
            "key": self.opp.key,
            "title": self.opp.title,
            "sent": self.sent,
            "complete": self.complete,
            "action": self.action,
            "expected_profit": round(self.opp.profit, 4),
            "spent": round(self.spent, 4),
            "proceeds": round(self.proceeds, 4),
            "realized_pnl": round(self.realized_pnl, 4),
            "locked_cost": round(self.locked_cost, 4),
            "expected_payout": round(self.expected_payout, 4),
            "settled_shares": self.settled_shares,
            "orphaned": self.orphaned,
            "convert_tx": self.convert_tx,
            "latency_ms": round(1000 * (self.finished_at - self.started_at), 1),
            "legs": [
                {"label": r.leg.label, "ok": r.ok, "shares": round(r.shares, 4), "cost": round(r.cost, 4), "status": r.status, "error": r.error}
                for r in self.legs + self.retries
            ],
            "sells": [{"outcome": s.outcome.value, "ok": s.ok, "shares": s.shares, "net": round(s.net, 4)} for s in self.sells],
            "error": self.error,
        }


class Gateway(Protocol):
    mode: str

    async def buy(self, legs: Sequence[Leg], slippage_ticks: int = 0) -> list[LegResult]:
        """Submit every leg at once as marketable FOK BUY orders (amount = expected notional, max_price = limit)."""

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
        """Sell ``shares`` of an outcome token at or above ``min_price`` (FAK)."""

    async def convert(self, neg_risk_market_id: str, index_set: int, shares: float) -> tuple[bool, str | None, float]:
        """NegRisk convert; returns (ok, tx hash, pUSD received)."""

    async def close(self) -> None: ...
