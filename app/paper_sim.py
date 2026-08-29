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

from app.fees import pair_taker_fee, taker_fee, taker_net
from app.hunter import Level, Setup, hunt, is_one_leg_setup, plus_ev_fill, total_size, walk

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
    shares: float = 0.0


def simulate_taker(
    setup: Setup,
    *,
    slip_ticks: int = 0,
    fee_rate: float | None = None,
    tick: float = TICK,
) -> TakerSim:
    rate = float(fee_rate if fee_rate is not None else setup.extra.get("fee_rate") or 0.07)
    slip = max(0, int(slip_ticks)) * float(tick)
    if is_one_leg_setup(setup):
        leg = str((setup.extra or {}).get("leg") or ("up" if setup.up_price >= setup.down_price else "down"))
        raw = setup.up_price if leg == "up" else setup.down_price
        px = min(0.99, round(float(raw) + slip, 4))
        fees = taker_fee(setup.shares, px, rate)
        net = round(float(setup.shares) * (1.0 - px) - fees, 5)
        cost = round(float(setup.shares) * px + fees, 6)
        up = px if leg == "up" else 0.0
        down = px if leg == "down" else 0.0
        slipped = slip > 1e-12
        if net <= 0:
            return TakerSim(
                False, up, down, net, cost, fees, slipped,
                "slip_killed_edge" if slipped else "non_positive_net",
                setup.shares,
            )
        return TakerSim(True, up, down, net, cost, fees, slipped, "filled", setup.shares)
    up = min(0.99, round(float(setup.up_price) + slip, 4))
    down = min(0.99, round(float(setup.down_price) + slip, 4))
    fees = pair_taker_fee(setup.shares, up, down, rate)
    net = taker_net(setup.shares, up, down, rate)
    cost = round(float(setup.shares) - net, 6)
    slipped = slip > 1e-12
    if net <= 0:
        return TakerSim(
            False, up, down, net, cost, fees, slipped,
            "slip_killed_edge" if slipped else "non_positive_net",
            setup.shares,
        )
    return TakerSim(True, up, down, net, cost, fees, slipped, "filled", setup.shares)


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
        return TakerSim(False, round(up_vwap, 4), round(dn_vwap, 4), net, cost, fees, False, "fok_net", 0.0)
    return TakerSim(
        True,
        round(up_vwap, 4),
        round(dn_vwap, 4),
        round(net, 5),
        cost,
        fees,
        False,
        "fok_filled",
        need,
    )


def fak_pair(
    *,
    up_asks: list[Level],
    down_asks: list[Level],
    shares: float,
    up_limit: float,
    down_limit: float,
    min_shares: float,
    min_edge: float,
    fee_rate: float,
    tail_confirm: float,
) -> TakerSim:
    """Fill-and-kill at the original limits: take whatever +EV size remains, or nothing.

    Strict FOK of the snapshot size dies when 26 shares shrink to 8. Live FAK
    (and a requote loop) would still lift the remaining 8 if they are +EV.
    """
    need = float(shares)
    floor = float(min_shares)
    up_cap = [lv for lv in up_asks if lv.price <= float(up_limit) + 1e-12]
    dn_cap = [lv for lv in down_asks if lv.price <= float(down_limit) + 1e-12]
    available = min(total_size(up_cap), total_size(dn_cap), need)
    if available + 1e-9 < floor:
        return TakerSim(False, 0.0, 0.0, 0.0, 0.0, 0.0, False, "fok_short")
    clipped = plus_ev_fill(up_cap, dn_cap, available, floor, min_edge, fee_rate, tail_confirm)
    if not clipped:
        return TakerSim(False, 0.0, 0.0, 0.0, 0.0, 0.0, False, "fok_net")
    sz, up_vwap, dn_vwap, _gross, net, _tail = clipped
    fees = pair_taker_fee(sz, up_vwap, dn_vwap, fee_rate)
    cost = round(sz - net, 6)
    reason = "fok_fak" if sz + 1e-9 < need else "fok_filled"
    return TakerSim(True, round(up_vwap, 4), round(dn_vwap, 4), round(net, 5), cost, fees, False, reason, sz)


def fak_one(
    *,
    asks: list[Level],
    shares: float,
    limit: float,
    min_shares: float,
    min_px: float,
    max_px: float,
    fee_rate: float,
) -> TakerSim:
    """One-leg FAK inside the favorite band at or better than the limit."""
    need = float(shares)
    floor = float(min_shares)
    cap = [
        lv
        for lv in asks
        if lv.price <= float(limit) + 1e-12 and float(min_px) - 1e-12 <= lv.price <= float(max_px) + 1e-12
    ]
    available = min(total_size(cap), need)
    if available + 1e-9 < floor:
        return TakerSim(False, 0.0, 0.0, 0.0, 0.0, 0.0, False, "fok_short")
    filled, vwap = walk(cap, available, asks=True)
    if filled + 1e-9 < floor:
        return TakerSim(False, vwap, 0.0, 0.0, 0.0, 0.0, False, "fok_short")
    fees = taker_fee(filled, vwap, fee_rate)
    net = round(filled * (1.0 - vwap) - fees, 5)
    if net <= 0:
        return TakerSim(False, round(vwap, 4), 0.0, net, 0.0, fees, False, "fok_net")
    cost = round(filled - net, 6)
    reason = "fok_fak" if filled + 1e-9 < need else "fok_filled"
    return TakerSim(True, round(vwap, 4), 0.0, net, cost, fees, False, reason, filled)


def confirm_pair(
    *,
    setup: Setup,
    up_asks: list[Level],
    down_asks: list[Level],
    up_bids: list[Level] | None = None,
    down_bids: list[Level] | None = None,
    min_shares: float,
    min_edge: float,
    fee_rate: float,
    tail_confirm: float,
    max_usd: float,
    prefer_tail: bool = True,
) -> TakerSim:
    """After the 250ms taker delay: FAK leftover +EV size at the snapshot
    limits, else hunt the delayed book (requote, no second delay).

    Live crypto up/down holds the first order 250ms (`itode`). A second wait
    would model requote+itode and miss holes that only last ~300–400ms.
    Requote-at-delayed-book is the honest fill of a sticky hole, not a ghost
    snapshot.
    """
    fill = fak_pair(
        up_asks=up_asks,
        down_asks=down_asks,
        shares=setup.shares,
        up_limit=setup.up_price,
        down_limit=setup.down_price,
        min_shares=min_shares,
        min_edge=min_edge,
        fee_rate=fee_rate,
        tail_confirm=tail_confirm,
    )
    if fill.ok:
        return fill
    requote = hunt(
        slug=setup.slug,
        title=setup.title,
        condition_id=setup.condition_id,
        up_token=setup.up_token,
        down_token=setup.down_token,
        up_asks=up_asks,
        down_asks=down_asks,
        up_bids=up_bids or [],
        down_bids=down_bids or [],
        max_usd=max_usd,
        min_shares=min_shares,
        min_edge=min_edge,
        fee_rate=fee_rate,
        prefer_tail=prefer_tail,
        tail_confirm=tail_confirm,
        maker_first=False,
        end=setup.end,
        maker_window_seconds=0.0,
    )
    if requote is None or requote.kind != "taker" or requote.net <= 0:
        return fill
    return TakerSim(
        True,
        requote.up_price,
        requote.down_price,
        requote.net,
        requote.cost,
        requote.fees,
        False,
        "fok_requote",
        requote.shares,
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
