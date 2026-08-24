from __future__ import annotations

from dataclasses import dataclass, field

from app.fees import gross_edge, pair_taker_fee, taker_net


@dataclass(frozen=True)
class Level:
    price: float
    size: float


@dataclass
class Setup:
    slug: str
    title: str
    condition_id: str
    up_token: str
    down_token: str
    kind: str  # taker | maker
    up_price: float
    down_price: float
    shares: float
    fillable: float
    gross: float
    fees: float
    net: float
    tail: bool
    end: str | None = None
    extra: dict = field(default_factory=dict)


def _sorted_asks(levels: list[Level]) -> list[Level]:
    return sorted((lv for lv in levels if lv.size > 0 and lv.price > 0), key=lambda x: x.price)


def _sorted_bids(levels: list[Level]) -> list[Level]:
    return sorted((lv for lv in levels if lv.size > 0 and lv.price > 0), key=lambda x: x.price, reverse=True)


def walk(levels: list[Level], shares: float, *, asks: bool) -> tuple[float, float]:
    """Fill `shares` walking the book. Returns (filled, vwap)."""
    ordered = _sorted_asks(levels) if asks else _sorted_bids(levels)
    need = float(shares)
    filled = 0.0
    cost = 0.0
    for lv in ordered:
        if filled >= need:
            break
        take = min(lv.size, need - filled)
        filled += take
        cost += take * lv.price
    if filled <= 0:
        return 0.0, 0.0
    return filled, cost / filled


def total_size(levels: list[Level]) -> float:
    return sum(lv.size for lv in levels if lv.size > 0)


def is_tail(up: float, down: float, confirm: float) -> bool:
    hi = max(up, down)
    lo = min(up, down)
    return hi >= confirm and lo <= (1.0 - confirm + 0.02)


def hunt(
    *,
    slug: str,
    title: str,
    condition_id: str,
    up_token: str,
    down_token: str,
    up_asks: list[Level],
    down_asks: list[Level],
    up_bids: list[Level],
    down_bids: list[Level],
    max_usd: float,
    min_shares: float,
    min_edge: float,
    fee_rate: float,
    prefer_tail: bool,
    tail_confirm: float,
    maker_first: bool,
    end: str | None = None,
) -> Setup | None:
    taker = _taker_setup(
        slug=slug,
        title=title,
        condition_id=condition_id,
        up_token=up_token,
        down_token=down_token,
        up_asks=up_asks,
        down_asks=down_asks,
        max_usd=max_usd,
        min_shares=min_shares,
        min_edge=min_edge,
        fee_rate=fee_rate,
        tail_confirm=tail_confirm,
        end=end,
    )
    maker = _maker_setup(
        slug=slug,
        title=title,
        condition_id=condition_id,
        up_token=up_token,
        down_token=down_token,
        up_bids=up_bids,
        down_bids=down_bids,
        max_usd=max_usd,
        min_shares=min_shares,
        min_edge=min_edge,
        tail_confirm=tail_confirm,
        end=end,
    )
    if prefer_tail and taker and taker.tail:
        return taker
    if taker and taker.net > 0:
        return taker
    if maker_first:
        return maker
    return taker


def _size_from_depth(up: list[Level], down: list[Level], max_usd: float, min_shares: float, pair_px: float) -> float:
    depth = min(total_size(up), total_size(down))
    if depth < min_shares:
        return 0.0
    pair = max(pair_px, 0.02)
    cap = max_usd / pair
    return max(0.0, min(depth, cap))


def _taker_setup(**kw) -> Setup | None:
    up_asks: list[Level] = _sorted_asks(kw["up_asks"])
    down_asks: list[Level] = _sorted_asks(kw["down_asks"])
    if not up_asks or not down_asks:
        return None
    top_pair = up_asks[0].price + down_asks[0].price if up_asks and down_asks else 1.0
    shares = _size_from_depth(up_asks, down_asks, kw["max_usd"], kw["min_shares"], max(top_pair, 0.5))
    if shares < kw["min_shares"]:
        return None
    filled_up, up_vwap = walk(up_asks, shares, asks=True)
    filled_dn, dn_vwap = walk(down_asks, shares, asks=True)
    filled = min(filled_up, filled_dn)
    if filled < kw["min_shares"]:
        return None
    if filled < shares:
        filled_up, up_vwap = walk(up_asks, filled, asks=True)
        filled_dn, dn_vwap = walk(down_asks, filled, asks=True)
        filled = min(filled_up, filled_dn)
    gross = gross_edge(up_vwap, dn_vwap)
    fees = pair_taker_fee(filled, up_vwap, dn_vwap, kw["fee_rate"])
    net = taker_net(filled, up_vwap, dn_vwap, kw["fee_rate"])
    tail = is_tail(up_vwap, dn_vwap, kw["tail_confirm"])
    if net <= 0 or gross < kw["min_edge"]:
        if not (tail and net > 0):
            return None
    return Setup(
        slug=kw["slug"],
        title=kw["title"],
        condition_id=kw["condition_id"],
        up_token=kw["up_token"],
        down_token=kw["down_token"],
        kind="taker",
        up_price=round(up_vwap, 4),
        down_price=round(dn_vwap, 4),
        shares=round(filled, 4),
        fillable=round(filled, 4),
        gross=round(gross, 4),
        fees=round(fees, 5),
        net=round(net, 5),
        tail=tail,
        end=kw.get("end"),
    )


def _maker_setup(**kw) -> Setup | None:
    up_bids: list[Level] = _sorted_bids(kw["up_bids"])
    down_bids: list[Level] = _sorted_bids(kw["down_bids"])
    if not up_bids or not down_bids:
        return None
    up_bid, dn_bid = up_bids[0].price, down_bids[0].price
    gross = gross_edge(up_bid, dn_bid)
    if gross < kw["min_edge"]:
        return None
    shares = _size_from_depth(up_bids, down_bids, kw["max_usd"], kw["min_shares"], max(up_bid + dn_bid, 0.5))
    if shares < kw["min_shares"]:
        return None
    filled_up, up_vwap = walk(up_bids, shares, asks=False)
    filled_dn, dn_vwap = walk(down_bids, shares, asks=False)
    filled = min(filled_up, filled_dn)
    if filled < kw["min_shares"]:
        return None
    gross = gross_edge(up_vwap, dn_vwap)
    if gross < kw["min_edge"]:
        return None
    tail = is_tail(up_vwap, dn_vwap, kw["tail_confirm"])
    net = round(gross * filled, 5)  # maker pays 0
    return Setup(
        slug=kw["slug"],
        title=kw["title"],
        condition_id=kw["condition_id"],
        up_token=kw["up_token"],
        down_token=kw["down_token"],
        kind="maker",
        up_price=round(up_vwap, 4),
        down_price=round(dn_vwap, 4),
        shares=round(filled, 4),
        fillable=round(filled, 4),
        gross=round(gross, 4),
        fees=0.0,
        net=net,
        tail=tail,
        end=kw.get("end"),
    )
