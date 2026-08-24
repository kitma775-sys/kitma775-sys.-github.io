"""Paper fills that match live CLOB behaviour, not an optimistic ledger.

Taker: lift the scanned asks (optional extra ticks of slippage). If that
cross is still +EV after fees, it fills at that VWAP. That is the honest
"if I swept this snapshot" result.

Maker: never assume a fill. The order rests at the bid. A leg fills later
only if the ask trades through (enough size at ask <= resting bid).
Unmatched inventory is marked at $0 until the other leg fills and we merge.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.fees import pair_taker_fee, taker_net
from app.hunter import Level, Setup

TICK = 0.01


@dataclass(frozen=True)
class TakerSim:
    ok: bool
    up_price: float
    down_price: float
    net: float
    cost: float
    fees: float
    slipped: bool
    reason: str


def simulate_taker(
    setup: Setup,
    *,
    slip_ticks: int = 0,
    fee_rate: float | None = None,
    tick: float = TICK,
) -> TakerSim:
    rate = float(fee_rate if fee_rate is not None else setup.extra.get("fee_rate") or 0.07)
    slip = max(0, int(slip_ticks)) * float(tick)
    up = min(0.99, round(float(setup.up_price) + slip, 4))
    down = min(0.99, round(float(setup.down_price) + slip, 4))
    fees = pair_taker_fee(setup.shares, up, down, rate)
    net = taker_net(setup.shares, up, down, rate)
    cost = round(float(setup.shares) - net, 6)
    slipped = slip > 1e-12
    if net <= 0:
        return TakerSim(False, up, down, net, cost, fees, slipped, "slip_killed_edge" if slipped else "non_positive_net")
    return TakerSim(True, up, down, net, cost, fees, slipped, "filled")


def asks_cross_bid(asks: list[Level], bid_px: float, shares: float) -> bool:
    """True when the ask has enough size at or below our bid (traded through)."""
    if shares <= 0 or bid_px <= 0:
        return False
    need = float(shares)
    got = 0.0
    for lv in sorted((a for a in asks if a.size > 0 and a.price > 0), key=lambda x: x.price):
        if lv.price > bid_px + 1e-12:
            break
        got += lv.size
        if got + 1e-12 >= need:
            return True
    return False


def parse_end(end: str | None) -> datetime | None:
    if not end:
        return None
    try:
        return datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None


def market_expired(end: str | None, now: datetime | None = None) -> bool:
    dt = parse_end(end)
    if dt is None:
        return False
    clock = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt <= clock
