from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

from polyarb.book import EPS, Fill, Level, take
from polyarb.fees import fee_per_share
from polyarb.models import FeeSchedule

SHARE_DECIMALS = 2
MIN_ORDER_NOTIONAL = 1.0


@dataclass(frozen=True, slots=True)
class LegBook:
    """Liquidity available to BUY one leg (best/cheapest level first)."""

    levels: Sequence[Level]
    fee: FeeSchedule
    min_size: float = 0.0


@dataclass(frozen=True, slots=True)
class BasketFill:
    shares: float
    fills: tuple[Fill, ...]
    payout: float

    @property
    def cost(self) -> float:
        return sum(f.notional + f.fee for f in self.fills)

    @property
    def profit(self) -> float:
        return self.payout - self.cost


def floor_shares(q: float) -> float:
    scale = 10**SHARE_DECIMALS
    return math.floor(q * scale + 1e-7) / scale


def unit_cost(levels: Sequence[Level], fee: FeeSchedule) -> float | None:
    """All-in cost of the first share of a leg, or None when the book is empty."""
    if not levels:
        return None
    p = levels[0].price
    return p + fee_per_share(fee, p)


def max_profitable_units(
    legs: Sequence[LegBook],
    payout_per_unit: float,
    *,
    min_unit_edge: float = 0.0,
    max_shares: float = math.inf,
    max_cost: float = math.inf,
) -> float:
    """Largest equal per-leg size for which every marginal unit still earns ``min_unit_edge``.

    Each leg's cost curve is piecewise linear and convex, so the basket's
    marginal cost is non-decreasing and a single forward sweep is optimal.
    """
    if not legs or any(not leg.levels for leg in legs):
        return 0.0
    k = len(legs)
    idx = [0] * k
    left = [leg.levels[0].size for leg in legs]
    q = 0.0
    cost = 0.0
    while q < max_shares - EPS:
        marginal = 0.0
        for i, leg in enumerate(legs):
            p = leg.levels[idx[i]].price
            marginal += p + fee_per_share(leg.fee, p)
        if payout_per_unit - marginal < min_unit_edge - EPS:
            break
        step = min(min(left), max_shares - q)
        if math.isfinite(max_cost):
            step = min(step, max(0.0, (max_cost - cost) / marginal))
        if step <= EPS:
            break
        q += step
        cost += step * marginal
        for i in range(k):
            left[i] -= step
            if left[i] <= EPS:
                idx[i] += 1
                if idx[i] >= len(legs[i].levels):
                    return q
                left[i] = legs[i].levels[idx[i]].size
    return q


def size_basket(
    legs: Sequence[LegBook],
    payout_per_unit: float,
    *,
    min_unit_edge: float = 0.0,
    max_shares: float = math.inf,
    max_cost: float = math.inf,
) -> BasketFill | None:
    """Size an equal-quantity multi-leg BUY basket against current books.

    Returns None when the profitable size is below any leg's exchange minimum
    (``orderMinSize`` shares and $1 notional for marketable orders).
    """
    q = floor_shares(
        max_profitable_units(
            legs, payout_per_unit, min_unit_edge=min_unit_edge, max_shares=max_shares, max_cost=max_cost
        )
    )
    if q <= 0:
        return None
    if q < max((leg.min_size for leg in legs), default=0.0) - EPS:
        return None
    fills = tuple(take(leg.levels, q, leg.fee) for leg in legs)
    if any(f.shares < q - 1e-6 for f in fills):
        return None
    if any(f.notional < MIN_ORDER_NOTIONAL - EPS for f in fills):
        q = _bump_for_min_notional(legs, fills, q, payout_per_unit, max_shares, max_cost)
        if q is None:
            return None
        fills = tuple(take(leg.levels, q, leg.fee) for leg in legs)
        if any(f.shares < q - 1e-6 for f in fills):
            return None
    # A market BUY is validated on amount / max_price shares, not on the shares actually received.
    for leg, f in zip(legs, fills):
        if f.worst_price > 0 and f.notional / f.worst_price < leg.min_size - 1e-6:
            return None
    return BasketFill(q, fills, payout_per_unit * q)


def _bump_for_min_notional(
    legs: Sequence[LegBook],
    fills: Sequence[Fill],
    q: float,
    payout_per_unit: float,
    max_shares: float,
    max_cost: float,
) -> float | None:
    """Cheap legs (e.g. 0.3c) need more shares to reach $1; accept only if still profitable."""
    need = q
    for f in fills:
        if f.notional < MIN_ORDER_NOTIONAL and f.avg_price > 0:
            need = max(need, math.ceil(MIN_ORDER_NOTIONAL / f.avg_price * 100) / 100)
    if need > max_shares + EPS:
        return None
    trial = [take(leg.levels, need, leg.fee) for leg in legs]
    if any(t.shares < need - 1e-6 for t in trial):
        return None
    cost = sum(t.notional + t.fee for t in trial)
    if cost > max_cost + EPS or payout_per_unit * need - cost <= 0:
        return None
    if any(t.notional < MIN_ORDER_NOTIONAL - EPS for t in trial):
        return None
    return need
