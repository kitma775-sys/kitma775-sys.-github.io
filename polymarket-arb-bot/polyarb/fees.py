from __future__ import annotations

from polyarb.models import FeeSchedule

FEE_DECIMALS = 5


def fee_per_share(fee: FeeSchedule, price: float) -> float:
    """Taker fee in pUSD for one share traded at ``price`` (symmetric in p and 1-p)."""
    if fee.rate <= 0.0:
        return 0.0
    base = price * (1.0 - price)
    if base <= 0.0:
        return 0.0
    if fee.exponent != 1.0:
        base = base ** fee.exponent
    return fee.rate * base


def taker_fee(fee: FeeSchedule, shares: float, price: float) -> float:
    if shares <= 0.0:
        return 0.0
    return shares * fee_per_share(fee, price)


def round_fee(amount: float) -> float:
    """Exchange precision: 5 decimals, anything below 0.00001 is waived."""
    return round(amount, FEE_DECIMALS)


def breakeven_underround(fee: FeeSchedule, prices: list[float]) -> float:
    """pUSD per basket unit needed just to pay taker fees on every leg at ``prices``."""
    return sum(fee_per_share(fee, p) for p in prices)
