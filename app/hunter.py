from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.fees import gross_edge, pair_taker_fee, taker_fee, taker_net
from app.config import DEFAULT_SETTINGS
from app.twap import TwapParams, default_params, entry_edge, fee_per_share, reverse_placeholder_net, trade_leg, twap_entry_reason

MAKER_MIN_LEG = 0.22
MAKER_MAX_SKEW = 0.28
MAKER_WINDOW_SECONDS = 75.0


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

    @property
    def cost(self) -> float:
        """Cash to put on. Pair: shares - pair_net. One-leg: extra.cash_cost or shares-net."""
        cash = (self.extra or {}).get("cash_cost")
        if cash is not None:
            return round(float(cash), 6)
        return round(self.shares - self.net, 6)


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


def _seconds_left(end: str | None, now: datetime | None = None) -> float | None:
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (dt - stamp).total_seconds()


def is_tail(up: float, down: float, confirm: float) -> bool:
    hi = max(up, down)
    lo = min(up, down)
    return hi >= confirm and lo <= (1.0 - confirm + 0.02)


def parse_favorite_dir(raw) -> str:
    d = str(raw or "auto").strip().lower()
    return d if d in {"auto", "up", "down"} else "auto"


def in_favorite_window(seconds_left: float | None, window: float | None) -> bool:
    """window<=0 means the whole book (until 3s before end). None → default 60s."""
    if seconds_left is None or float(seconds_left) < 3:
        return False
    win = float(DEFAULT_SETTINGS["favorite_window_seconds"]) if window is None else float(window)
    if win <= 0:
        return True
    return float(seconds_left) <= win + 1e-9


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
    maker_min_leg: float = MAKER_MIN_LEG,
    maker_max_skew: float = MAKER_MAX_SKEW,
    maker_window_seconds: float = MAKER_WINDOW_SECONDS,
    maker_min_edge: float | None = None,
    now: datetime | None = None,
    strategy_mode: str = "complement",
    favorite_min_price: float = 0.97,
    favorite_max_price: float = 0.98,
    favorite_window_seconds: float = 60.0,
    favorite_maker: bool = False,
    favorite_dir: str = "auto",
    twap_snap=None,
    twap_params=None,
    first_px: float | None = None,
) -> Setup | None:
    mode = (strategy_mode or "complement").strip().lower()
    if mode not in {"complement", "favorite", "auto", "twap"}:
        mode = "complement"
    taker = None
    if mode in {"complement", "auto"}:
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
    maker_edge = min_edge if maker_min_edge is None else float(maker_min_edge)
    window = float(maker_window_seconds or 0)
    maker = None
    if mode == "complement" and window >= 3:
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
            min_edge=maker_edge,
            tail_confirm=tail_confirm,
            end=end,
            seconds_left=_seconds_left(end, now),
            maker_min_leg=maker_min_leg,
            maker_max_skew=maker_max_skew,
            maker_window_seconds=window,
        )
    # Two-ask complement always beats buying only the favorite.
    if prefer_tail and taker and taker.tail:
        return taker
    if taker and taker.net > 0:
        return taker
    if mode in {"favorite", "auto"}:
        fav = _favorite_setup(
            slug=slug,
            title=title,
            condition_id=condition_id,
            up_token=up_token,
            down_token=down_token,
            up_asks=up_asks,
            down_asks=down_asks,
            up_bids=up_bids,
            down_bids=down_bids,
            max_usd=max_usd,
            min_shares=min_shares,
            fee_rate=fee_rate,
            end=end,
            now=now,
            min_px=favorite_min_price,
            max_px=favorite_max_price,
            window=favorite_window_seconds,
            allow_maker=bool(favorite_maker),
            dir=parse_favorite_dir(favorite_dir),
        )
        if fav:
            return fav
    if mode == "twap":
        tw = _twap_setup(
            slug=slug,
            title=title,
            condition_id=condition_id,
            up_token=up_token,
            down_token=down_token,
            up_asks=up_asks,
            down_asks=down_asks,
            up_bids=up_bids,
            down_bids=down_bids,
            max_usd=max_usd,
            min_shares=min_shares,
            fee_rate=fee_rate,
            end=end,
            now=now,
            snap=twap_snap,
            params=twap_params,
            first_px=first_px,
        )
        if tw:
            return tw
    if maker:
        return maker
    return taker


def is_favorite_setup(setup: Setup | None) -> bool:
    return bool(setup and (setup.extra or {}).get("strategy") == "favorite")


def is_twap_setup(setup: Setup | None) -> bool:
    return bool(setup and (setup.extra or {}).get("strategy") == "twap")


def is_one_leg_setup(setup: Setup | None) -> bool:
    return is_favorite_setup(setup) or is_twap_setup(setup)


def _twap_setup(
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
    fee_rate: float,
    end: str | None,
    now,
    snap,
    params,
    first_px: float | None = None,
) -> Setup | None:
    p = params if isinstance(params, TwapParams) else default_params(params if isinstance(params, dict) else None)
    left = _seconds_left(end, now)
    leg = trade_leg(snap, p)
    asks = up_asks if leg == "up" else down_asks if leg == "down" else []
    bids = up_bids if leg == "up" else down_bids if leg == "down" else []
    ask = _top(asks, asks=True)
    bid = _top(bids, asks=False)
    if twap_entry_reason(
        slug=slug, snap=snap, ask=ask, bid=bid, left=left, fee_rate=fee_rate, params=p, first_px=first_px
    ):
        return None
    assert snap is not None and ask is not None and leg in {"up", "down"}
    if p.reverse:
        fair = None if snap.fair_p_up is None else (snap.fair_p_up if leg == "up" else round(1.0 - snap.fair_p_up, 6))
    else:
        fair = snap.fair_p_side
        if fair is None:
            return None
    fee_share = fee_per_share(ask, fee_rate)
    unit = max(ask + fee_share, 0.02)
    depth = total_size([lv for lv in asks if p.min_price - 1e-12 <= lv.price <= min(ask + 1e-12, p.max_price)])
    shares = min(float(max_usd) / unit, depth)
    if shares + 1e-9 < float(min_shares):
        return None
    filled, vwap = walk(
        [lv for lv in asks if lv.price <= ask + 1e-12 and p.min_price - 1e-12 <= lv.price <= p.max_price + 1e-12],
        shares,
        asks=True,
    )
    if filled + 1e-9 < float(min_shares):
        return None
    fees = taker_fee(filled, vwap, fee_rate)
    if p.reverse:
        # Risk/FOK reject net<=0. Fade EV vs BM fair is usually negative by design.
        ev_net = reverse_placeholder_net(filled)
        gross = round((0.5 - vwap), 4)
    else:
        assert fair is not None
        ev_net = round(filled * entry_edge(fair, vwap, fee_rate), 5)
        if ev_net <= 0:
            return None
        gross = round(fair - vwap, 4)
    cash = round(filled * vwap + fees, 6)
    up_px = vwap if leg == "up" else 0.0
    dn_px = vwap if leg == "down" else 0.0
    return Setup(
        slug=slug,
        title=title,
        condition_id=condition_id,
        up_token=up_token,
        down_token=down_token,
        kind="taker",
        up_price=up_px,
        down_price=dn_px,
        shares=round(filled, 4),
        fillable=round(filled, 4),
        gross=gross,
        fees=round(fees, 5),
        net=ev_net,
        tail=False,
        end=end,
        extra={
            "fee_rate": float(fee_rate),
            "strategy": "twap",
            "leg": leg,
            "fill_px": round(vwap, 4),
            "cash_cost": cash,
            "fair_p": fair,
            "lead_bps": snap.lead_bps,
            "lead_side": snap.side,
            "reverse": bool(p.reverse),
            "ptb": snap.ptb,
            "twap": snap.twap,
            "lookback": snap.lookback,
        },
    )


# Live paper was lifting a 97¢ ask while the tape was still 0.63–0.92, and
# later lifting leftover 97¢ after the book had already gone through to 99¢
# (eth-updown-5m-1788008400: FOK-kill 0.98, fill 0.97, dump 6s later).
# Backtests only count a public BUY at 97–98. The *touch* must be the
# favorite: true best ask in-band, bid still in/near the band (not 99¢),
# not crossed, spread tight, dog ask still cheap.
FAVORITE_BID_SLACK = 0.02
FAVORITE_MAX_SPREAD = 0.04
FAVORITE_MAX_OTHER_ASK = 0.10


def favorite_ws_ok(ws_status: str | None, book_src: str | None, *books: dict | None) -> bool:
    """Favorite lifts need a live WS book. HTTP fallback after 1013 is stale."""
    if str(ws_status or "") != "connected" or str(book_src or "") != "ws":
        return False
    for b in books:
        src = str((b or {}).get("source") or "ws")
        if src != "ws":
            return False
    return True


def favorite_lock_reason(
    *,
    asks: list[Level],
    bids: list[Level],
    other_asks: list[Level],
    min_px: float,
    max_px: float,
) -> str | None:
    """None if this is a locked 97–98 favorite. Else a kill reason."""
    lo, hi = min(float(min_px), float(max_px)), max(float(min_px), float(max_px))
    ask = _top(asks, asks=True)
    bid = _top(bids, asks=False)
    other = _top(other_asks, asks=True)
    if ask is None or bid is None:
        return "favorite_ghost"
    if not (lo - 1e-12 <= float(ask) <= hi + 1e-12):
        return "favorite_not_top"
    if is_ghost_favorite(ask, bid, min_px=lo, max_px=hi):
        if float(bid) > hi + 1e-12:
            return "favorite_through"
        if float(ask) + 1e-12 < float(bid):
            return "favorite_crossed"
        return "favorite_ghost"
    if favorite_other_too_rich(other):
        return "favorite_other_ask"
    return None


def is_ghost_favorite(
    ask: float | None,
    bid: float | None,
    min_px: float = 0.97,
    max_px: float = 0.98,
) -> bool:
    """Hanging 97¢ asks over a 70¢ last, or leftover 97¢ on a 99¢ bid, are not lift-able."""
    if ask is None or bid is None:
        return True
    lo, hi = min(float(min_px), float(max_px)), max(float(min_px), float(max_px))
    a, b = float(ask), float(bid)
    if b < lo - FAVORITE_BID_SLACK - 1e-12:
        return True
    if b > hi + 1e-12:
        return True
    if a + 1e-12 < b:
        return True
    if a - b >= FAVORITE_MAX_SPREAD - 1e-12:
        return True
    return False


def favorite_other_too_rich(other_ask: float | None) -> bool:
    """If the dog ask is already 10¢+, the 97¢ favorite is mid-flip."""
    if other_ask is None:
        return True
    return float(other_ask) >= FAVORITE_MAX_OTHER_ASK - 1e-12


def favorite_window_key(slug: str | None) -> str | None:
    """Same-clock lock: btc-updown-5m-T and eth-updown-5m-T share a key.

    15m uses `updown-15m-T`. Hourly Binance slugs are not TWAP windows.
    """
    from app.twap import parse_window

    parsed = parse_window(slug or "")
    if parsed:
        return f"updown-{parsed.horizon}-{parsed.start}"
    parts = str(slug or "").split("-")
    if len(parts) < 4:
        return None
    return "-".join(parts[1:])


def _band_asks(asks: list[Level], min_px: float, max_px: float) -> list[Level]:
    return [lv for lv in _sorted_asks(asks) if min_px - 1e-12 <= lv.price <= max_px + 1e-12]


def _favorite_setup(**kw) -> Setup | None:
    left = _seconds_left(kw.get("end"), kw.get("now"))
    if not in_favorite_window(left, kw.get("window")):
        return None
    min_px = min(float(kw["min_px"]), float(kw["max_px"]))
    max_px = max(float(kw["min_px"]), float(kw["max_px"]))
    kw = dict(kw)
    kw["min_px"] = min_px
    kw["max_px"] = max_px
    kw["dir"] = parse_favorite_dir(kw.get("dir"))
    taker = _favorite_taker_leg(**kw)
    if taker:
        return taker
    if kw.get("allow_maker"):
        return _favorite_maker_leg(**kw)
    return None


def _favorite_leg_candidates(kw) -> list[tuple[str, list[Level], list[Level]]]:
    d = parse_favorite_dir(kw.get("dir"))
    out: list[tuple[str, list[Level], list[Level]]] = []
    if d in {"auto", "up"}:
        out.append(("up", _band_asks(kw["up_asks"], float(kw["min_px"]), float(kw["max_px"])), kw["up_bids"]))
    if d in {"auto", "down"}:
        out.append(("down", _band_asks(kw["down_asks"], float(kw["min_px"]), float(kw["max_px"])), kw["down_bids"]))
    return out


def _favorite_taker_leg(**kw) -> Setup | None:
    min_px, max_px = float(kw["min_px"]), float(kw["max_px"])
    candidates = _favorite_leg_candidates(kw)
    best: Setup | None = None
    for leg, asks, bids in candidates:
        if not asks:
            continue
        full_asks = kw["up_asks"] if leg == "up" else kw["down_asks"]
        other_asks = kw["down_asks"] if leg == "up" else kw["up_asks"]
        if favorite_lock_reason(
            asks=full_asks,
            bids=bids,
            other_asks=other_asks,
            min_px=min_px,
            max_px=max_px,
        ):
            continue
        top = asks[0].price
        shares = _size_from_depth(asks, asks, kw["max_usd"], kw["min_shares"], max(top, 0.5))
        if shares < kw["min_shares"]:
            continue
        filled, vwap = walk(asks, shares, asks=True)
        if filled < kw["min_shares"]:
            continue
        if not (min_px - 1e-12 <= vwap <= max_px + 1e-12):
            continue
        fees = taker_fee(filled, vwap, kw["fee_rate"])
        net = round(filled * (1.0 - vwap) - fees, 5)
        if net <= 0:
            continue
        up_px = round(vwap, 4) if leg == "up" else 0.0
        dn_px = round(vwap, 4) if leg == "down" else 0.0
        setup = Setup(
            slug=kw["slug"],
            title=kw["title"],
            condition_id=kw["condition_id"],
            up_token=kw["up_token"],
            down_token=kw["down_token"],
            kind="taker",
            up_price=up_px,
            down_price=dn_px,
            shares=round(filled, 4),
            fillable=round(filled, 4),
            gross=round(1.0 - vwap, 4),
            fees=round(fees, 5),
            net=net,
            tail=True,
            end=kw.get("end"),
            extra={
                "fee_rate": float(kw["fee_rate"]),
                "strategy": "favorite",
                "leg": leg,
                "favorite_px": round(vwap, 4),
            },
        )
        if best is None or vwap > float((best.extra or {}).get("favorite_px") or 0):
            best = setup
    return best


def _favorite_maker_leg(**kw) -> Setup | None:
    """Rest a buy at favorite_min on the rich leg. Fill only on trade-through.

    Pays 0 maker fee so a 95¢ fill beats lifting 99¢, but last-second dumps
    hit this bid — that is the steamroller. Paper never assume-fills.
    """
    min_px, max_px = float(kw["min_px"]), float(kw["max_px"])
    tick = 0.01
    d = parse_favorite_dir(kw.get("dir"))
    legs = []
    if d in {"auto", "up"}:
        legs.append(("up", kw["up_asks"], kw["up_bids"]))
    if d in {"auto", "down"}:
        legs.append(("down", kw["down_asks"], kw["down_bids"]))
    best_leg = None
    best_rich = 0.0
    for leg, asks, bids in legs:
        ask = _top(asks, asks=True)
        bid = _top(bids, asks=False)
        rich = ask if ask is not None else bid
        if rich is None:
            continue
        if not (min_px - 1e-12 <= float(rich) <= max_px + 1e-12):
            continue
        if is_ghost_favorite(ask if ask is not None else rich, bid, min_px=min_px, max_px=max_px):
            continue
        other_asks = kw["down_asks"] if leg == "up" else kw["up_asks"]
        if favorite_other_too_rich(_top(other_asks, asks=True)):
            continue
        if float(rich) >= best_rich:
            best_rich = float(rich)
            best_leg = (leg, ask, bid)
    if best_leg is None:
        return None
    leg, ask, bid = best_leg
    quote = round(min_px, 4)
    # Post-only: if the min already touches the ask, step one tick under.
    if ask is not None and quote + 1e-12 >= float(ask):
        quote = round(float(ask) - tick, 4)
    if quote < 0.89 or quote > max_px:
        return None
    if ask is not None and quote + 1e-12 >= float(ask):
        return None
    shares = float(kw["max_usd"]) / max(quote, 0.01)
    if shares < kw["min_shares"]:
        return None
    net = round(shares * (1.0 - quote), 5)
    up_px = quote if leg == "up" else 0.0
    dn_px = quote if leg == "down" else 0.0
    return Setup(
        slug=kw["slug"],
        title=kw["title"],
        condition_id=kw["condition_id"],
        up_token=kw["up_token"],
        down_token=kw["down_token"],
        kind="maker",
        up_price=up_px,
        down_price=dn_px,
        shares=round(shares, 4),
        fillable=round(shares, 4),
        gross=round(1.0 - quote, 4),
        fees=0.0,
        net=net,
        tail=True,
        end=kw.get("end"),
        extra={
            "fee_rate": 0.0,
            "strategy": "favorite",
            "leg": leg,
            "favorite_px": quote,
        },
    )


def _top(levels: list[Level], *, asks: bool) -> float | None:
    ordered = _sorted_asks(levels) if asks else _sorted_bids(levels)
    return ordered[0].price if ordered else None


def book_quote(
    *,
    slug: str,
    up_asks: list[Level],
    down_asks: list[Level],
    up_bids: list[Level],
    down_bids: list[Level],
    fee_rate: float,
    end: str | None = None,
) -> dict:
    """Top-of-book tape so idle scans can show 'no arb' instead of looking hung."""
    up_ask, dn_ask = _top(up_asks, asks=True), _top(down_asks, asks=True)
    up_bid, dn_bid = _top(up_bids, asks=False), _top(down_bids, asks=False)
    ask_sum = None if up_ask is None or dn_ask is None else round(up_ask + dn_ask, 4)
    bid_sum = None if up_bid is None or dn_bid is None else round(up_bid + dn_bid, 4)
    tnet = None if up_ask is None or dn_ask is None else round(taker_net(1.0, up_ask, dn_ask, fee_rate), 4)
    left = _seconds_left(end)
    balanced = (
        up_bid is not None
        and dn_bid is not None
        and min(up_bid, dn_bid) >= MAKER_MIN_LEG
        and abs(up_bid - dn_bid) <= MAKER_MAX_SKEW
    )
    return {
        "slug": slug,
        "ask_sum": ask_sum,
        "bid_sum": bid_sum,
        "taker_net": tnet,
        "maker_gross": None if bid_sum is None else round(gross_edge(up_bid or 0.0, dn_bid or 0.0), 4),
        "maker_balanced": balanced,
        "seconds_left": None if left is None else round(left, 1),
        "up_ask": up_ask,
        "down_ask": dn_ask,
        "up_bid": up_bid,
        "down_bid": dn_bid,
    }


def summarize_quotes(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0}
    asks = [r["ask_sum"] for r in rows if r.get("ask_sum") is not None]
    bids = [r["bid_sum"] for r in rows if r.get("maker_balanced") and r.get("bid_sum") is not None]
    nets = [r["taker_net"] for r in rows if r.get("taker_net") is not None]
    mg = [r["maker_gross"] for r in rows if r.get("maker_balanced") and r.get("maker_gross") is not None]
    lefts = [(r["seconds_left"], r.get("slug") or "") for r in rows if r.get("seconds_left") is not None]
    best_taker = max(rows, key=lambda r: (r.get("taker_net") is not None, r.get("taker_net") if r.get("taker_net") is not None else -9e9))
    maker_rows = [r for r in rows if r.get("maker_balanced") and r.get("maker_gross") is not None]
    best_maker = max(maker_rows, key=lambda r: r.get("maker_gross") or 0) if maker_rows else {}
    nearest = min(lefts) if lefts else (None, None)
    return {
        "n": len(rows),
        "min_ask_sum": min(asks) if asks else None,
        "max_ask_sum": max(asks) if asks else None,
        "max_taker_net": max(nets) if nets else None,
        "min_bid_sum": min(bids) if bids else None,
        "max_maker_gross": max(mg) if mg else None,
        "best_taker_slug": best_taker.get("slug"),
        "best_maker_slug": best_maker.get("slug"),
        "nearest_s": nearest[0],
        "nearest_slug": nearest[1],
    }


def _size_from_depth(up: list[Level], down: list[Level], max_usd: float, min_shares: float, pair_px: float) -> float:
    depth = min(total_size(up), total_size(down))
    if depth < min_shares:
        return 0.0
    pair = max(pair_px, 0.02)
    cap = max_usd / pair
    return max(0.0, min(depth, cap))


def plus_ev_fill(
    up_asks: list[Level],
    down_asks: list[Level],
    max_shares: float,
    min_shares: float,
    min_edge: float,
    fee_rate: float,
    tail_confirm: float,
) -> tuple[float, float, float, float, float, bool] | None:
    """Largest size in [min_shares, max_shares] that is still +EV after fees.

    Taking the full USD cap can mix in worse levels and kill a real 5–10 share
    tail. Binary-search the prefix instead of all-or-nothing at max size.
    """
    up_asks = _sorted_asks(up_asks)
    down_asks = _sorted_asks(down_asks)
    hi = float(max_shares)
    lo = float(min_shares)
    if hi + 1e-9 < lo:
        return None

    def try_size(sz: float) -> tuple[float, float, float, float, float, bool] | None:
        sz = round(float(sz), 4)
        if sz + 1e-9 < lo:
            return None
        filled_up, up_vwap = walk(up_asks, sz, asks=True)
        filled_dn, dn_vwap = walk(down_asks, sz, asks=True)
        got = min(filled_up, filled_dn)
        if got + 1e-9 < sz:
            return None
        gross = gross_edge(up_vwap, dn_vwap)
        net = taker_net(sz, up_vwap, dn_vwap, fee_rate)
        tail = is_tail(up_vwap, dn_vwap, tail_confirm)
        if net <= 0:
            return None
        if gross < min_edge and not tail:
            return None
        return sz, up_vwap, dn_vwap, gross, net, tail

    best = try_size(hi)
    if best:
        return best
    best = try_size(lo)
    if not best:
        return None
    left, right = lo, hi
    for _ in range(20):
        if right - left < 0.05:
            break
        mid = (left + right) / 2
        got = try_size(mid)
        if got:
            best = got
            left = mid
        else:
            right = mid
    return best


def _taker_setup(**kw) -> Setup | None:
    up_asks: list[Level] = _sorted_asks(kw["up_asks"])
    down_asks: list[Level] = _sorted_asks(kw["down_asks"])
    if not up_asks or not down_asks:
        return None
    top_pair = up_asks[0].price + down_asks[0].price if up_asks and down_asks else 1.0
    shares = _size_from_depth(up_asks, down_asks, kw["max_usd"], kw["min_shares"], max(top_pair, 0.5))
    if shares < kw["min_shares"]:
        return None
    clipped = plus_ev_fill(
        up_asks,
        down_asks,
        shares,
        kw["min_shares"],
        kw["min_edge"],
        kw["fee_rate"],
        kw["tail_confirm"],
    )
    if not clipped:
        return None
    filled, up_vwap, dn_vwap, gross, net, tail = clipped
    fees = pair_taker_fee(filled, up_vwap, dn_vwap, kw["fee_rate"])
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
        extra={"fee_rate": float(kw["fee_rate"])},
    )


def _maker_setup(**kw) -> Setup | None:
    left = kw.get("seconds_left")
    raw_window = kw.get("maker_window_seconds")
    window = float(raw_window) if raw_window is not None else MAKER_WINDOW_SECONDS
    if left is None or left > window or left < 3:
        return None
    up_bids: list[Level] = _sorted_bids(kw["up_bids"])
    down_bids: list[Level] = _sorted_bids(kw["down_bids"])
    if not up_bids or not down_bids:
        return None
    up_bid, dn_bid = up_bids[0].price, down_bids[0].price
    min_leg = float(kw.get("maker_min_leg") or MAKER_MIN_LEG)
    max_skew = float(kw.get("maker_max_skew") or MAKER_MAX_SKEW)
    if min(up_bid, dn_bid) < min_leg:
        return None
    if abs(up_bid - dn_bid) > max_skew:
        return None
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
    if min(up_vwap, dn_vwap) < min_leg or abs(up_vwap - dn_vwap) > max_skew:
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
        extra={"fee_rate": 0.0},
    )
