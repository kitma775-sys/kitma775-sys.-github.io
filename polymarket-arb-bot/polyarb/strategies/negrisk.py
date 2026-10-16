"""NegRisk (mutually exclusive multi-outcome) strategies.

NO-basket convert (risk: low)
    Buy NO on a subset S (|S| >= 2) of an event's questions, then call
    ``NegRiskCtfCollateralAdapter.convertPositions(marketId, indexSet, q)`` which
    burns the NOs and returns (|S| - 1) * q pUSD plus YES on every question
    outside S. Since NO ask = 1 - YES bid, the per-unit profit is
    ``sum_{i in S}(bid_i - fee_i) - 1``. No resolution risk; only leg risk.

YES-basket (risk: medium)
    Buy YES on every outcome of an exhaustive event; exactly one pays $1 at
    resolution. Requires the outcome list to be complete (no augmented
    placeholders, a catch-all / open-ended bucket) and ties capital up until
    resolution, so it is gated on APR.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from typing import Iterable

from polyarb.config import ConvertConfig, YesBasketConfig
from polyarb.fees import fee_per_share
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_CATCH_ALL = re.compile(
    r"\b(other|others|none|no one|nobody|neither|no change|unchanged|field|any other|someone else|"
    r"not listed|no winner|tie|draw|no agreement|no deal)\b",
    re.I,
)
_LOW_OPEN = re.compile(r"^\s*[<≤↓]|\bor (less|fewer|lower|below)\b|\b(under|below|less than|fewer than)\b", re.I)
_HIGH_OPEN = re.compile(r"^\s*[>≥↑]|\d\s*\+|\bor (more|higher|above|greater)\b|\b(over|above|more than|greater than|at least)\b", re.I)


def exhaustive_reason(event: Event) -> str | None:
    """Heuristic evidence that the listed outcomes cover every possibility."""
    if any(m.neg_risk_other for m in event.markets):
        return "other-market"
    labels = [m.group_title or m.question for m in event.markets]
    if any(_CATCH_ALL.search(label) for label in labels):
        return "catch-all-label"
    if any(_LOW_OPEN.search(label) for label in labels) and any(_HIGH_OPEN.search(label) for label in labels):
        return "open-ended-partition"
    return None


def _convertible(event: Event) -> list[Market]:
    if not event.neg_risk or event.version != "v1" or not event.neg_risk_market_id:
        return []
    return [
        m
        for m in event.tradeable_markets
        if m.is_yes_no and m.neg_risk_market_id == event.neg_risk_market_id and m.neg_risk_index is not None
    ]


class NegRiskConvert:
    name = "negrisk_convert"

    def __init__(self, cfg: ConvertConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out = []
        for ev in events:
            markets = _convertible(ev)
            if len(markets) >= 2:
                out.append(Target(f"convert:{ev.id}", self.name, ev, tuple(m.yes_token for m in markets), tuple(markets)))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        """Best top-of-book profit per converted unit, sum(bid_i - fee_i) - 1 over the best subset."""
        gains = []
        for m in target.payload:
            book = books.get(m.yes_token)
            if book is None or not book.bids:
                continue
            p = 1.0 - book.bids[0].price
            gains.append(1.0 - p - fee_per_share(m.fee, p))
        if len(gains) < 2:
            return None
        gains.sort(reverse=True)
        positive = [g for g in gains[: self.cfg.max_legs] if g > 0]
        return (sum(positive) if len(positive) >= 2 else gains[0] + gains[1]) - 1.0

    def evaluate(self, target: Target, books: Books, now: float) -> list[Opportunity]:
        cfg = self.cfg
        entries = []
        for m in target.payload:
            book = books.get(m.yes_token)
            if book is None:
                continue
            levels = book.no_asks()
            if not levels:
                continue
            p = levels[0].price
            gain = 1.0 - p - fee_per_share(m.fee, p)
            if gain > 0:
                entries.append((gain, m, levels))
        if len(entries) < 2:
            return []
        entries.sort(key=lambda e: -e[0])
        entries = entries[: cfg.max_legs]

        best = None
        prefix = 0.0
        for k, (gain, _, _) in enumerate(entries, start=1):
            prefix += gain
            if k < 2 or prefix - 1.0 < cfg.min_unit_edge:
                continue
            legs = [LegBook(lv, m.fee, m.min_size) for _, m, lv in entries[:k]]
            fill = size_basket(legs, float(k - 1), min_unit_edge=cfg.min_unit_edge, max_cost=self.max_trade_usd)
            if fill is not None and (best is None or fill.profit > best[1].profit):
                best = (k, fill)
        if best is None:
            return []
        k, fill = best
        chosen = [m for _, m, _ in entries[:k]]
        ev = target.event
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=ev.id,
            title=ev.title,
            legs=[buy_leg(m, Outcome.NO, f) for m, f in zip(chosen, fill.fills)],
            shares=fill.shares,
            cost=fill.cost + cfg.gas_usd,
            payout_floor=fill.payout,
            settlement=Settlement.CONVERT,
            risk="low",
            resolves_at=None,
            detected_at=now,
            meta={
                "neg_risk_market_id": ev.neg_risk_market_id,
                "index_set": sum(1 << m.neg_risk_index for m in chosen),
                "legs_used": k,
                "legs_listed": len(target.payload),
                "augmented": ev.neg_risk_augmented,
            },
        )
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge:
            return []
        return [opp]


class NegRiskYesBasket:
    name = "negrisk_yes_basket"

    def __init__(self, cfg: YesBasketConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out = []
        for ev in events:
            if not ev.neg_risk or ev.neg_risk_augmented or ev.version != "v1":
                continue
            # A paused (not closed) leg or any non-NO resolution makes the basket incomplete.
            if any(not m.accepting_orders and not m.closed for m in ev.markets):
                continue
            if any(m.closed and m.resolved_outcome != "NO" for m in ev.markets):
                continue
            markets = ev.tradeable_markets
            if len(markets) < 2 or len(markets) > self.cfg.max_legs or not all(m.is_yes_no for m in markets):
                continue
            reason = exhaustive_reason(ev)
            if self.cfg.require_catch_all and reason is None:
                continue
            if not all(m.end_date for m in markets):
                continue
            out.append(Target(f"yes:{ev.id}", self.name, ev, tuple(m.yes_token for m in markets), (tuple(markets), reason)))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        """1 - sum(ask_i + fee_i): profit per basket unit at the top of book."""
        markets, _ = target.payload
        total = 0.0
        for m in markets:
            book = books.get(m.yes_token)
            if book is None or not book.asks:
                return None
            total += unit_cost(book.asks, m.fee) or 0.0
        return 1.0 - total

    def evaluate(self, target: Target, books: Books, now: float) -> list[Opportunity]:
        cfg = self.cfg
        markets, reason = target.payload
        legs = []
        for m in markets:
            book = books.get(m.yes_token)
            if book is None or not book.asks:
                return []
            legs.append(LegBook(book.asks, m.fee, m.min_size))
        if sum(unit_cost(leg.levels, leg.fee) for leg in legs) > 1.0 - cfg.min_unit_edge:
            return []
        if sum(leg.levels[0].price for leg in legs) < cfg.min_ask_sum:
            return []
        resolves_at = max(m.end_date for m in markets) + timedelta(days=cfg.resolution_lag_days)
        now_dt = utc(now)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
            return []
        fill = size_basket(legs, 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=self.max_trade_usd)
        if fill is None:
            return []
        ev = target.event
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=ev.id,
            title=ev.title,
            legs=[buy_leg(m, Outcome.YES, f) for m, f in zip(markets, fill.fills)],
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="medium",
            resolves_at=resolves_at,
            detected_at=now,
            meta={"exhaustive": reason, "legs": len(markets)},
        )
        apr = opp.apr(now_dt)
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge or (apr is not None and apr < cfg.min_apr):
            return []
        return [opp]
