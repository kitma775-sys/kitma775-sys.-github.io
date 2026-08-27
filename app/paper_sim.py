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
from app.hunter import Level, Setup, walk

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


def fok_pair(
    *,
    up_asks: list[Level],
    down_asks: list[Level],
    shares: float,
    up_limit: float,
    down_limit: float,
    fee_rate: float,
) -> TakerSim:
    """Fill-or-kill both legs: full size at or better than the limits, or nothing.

    This is the pair-FOK paper model. Live CLOB cannot atomically FOK two tokens;
    paper still fills neither if either leg would kill, so we do not credit a
    one-sided snapshot the way the old taker ledger did.
    """
    need = float(shares)
    if need <= 0:
        return TakerSim(False, 0.0, 0.0, 0.0, 0.0, 0.0, False, "fok_no_size")
    up_cap = [lv for lv in up_asks if lv.price <= float(up_limit) + 1e-12]
    dn_cap = [lv for lv in down_asks if lv.price <= float(down_limit) + 1e-12]
    filled_up, up_vwap = walk(up_cap, need, asks=True)
    filled_dn, dn_vwap = walk(dn_cap, need, asks=True)
    if filled_up + 1e-9 < need:
        return TakerSim(False, up_vwap, dn_vwap, 0.0, 0.0, 0.0, False, "fok_up_short")
    if filled_dn + 1e-9 < need:
        return TakerSim(False, up_vwap, dn_vwap, 0.0, 0.0, 0.0, False, "fok_down_short")
    fees = pair_taker_fee(need, up_vwap, dn_vwap, fee_rate)
    net = taker_net(need, up_vwap, dn_vwap, fee_rate)
    cost = round(need - net, 6)
    if net <= 0:
        return TakerSim(False, round(up_vwap, 4), round(dn_vwap, 4), net, cost, fees, False, "fok_net")
    return TakerSim(
        True,
        round(up_vwap, 4),
        round(dn_vwap, 4),
        round(net, 5),
        cost,
        fees,
        False,
        "fok_filled",
    )


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
    left = seconds_left(end, now)
    return left is not None and left <= 0


def seconds_left(end: str | None, now: datetime | None = None) -> float | None:
    dt = parse_end(end)
    if dt is None:
        return None
    clock = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    return (dt - clock).total_seconds()
