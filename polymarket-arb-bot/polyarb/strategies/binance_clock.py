"""Deterministic Binance Spot closed-candle lock strategy (``binance_lock``).

Trades ONLY 100% mathematically decided outcomes on Polymarket crypto events
whose official settlement source is Binance Spot (``oracle_sig`` starting with
``binance_``):

1. Hourly ``"<Coin> above ___ on <Date>, <Hour> ET?"`` (``binance_1h``)
2. Daily noon ``"<Coin> above ___ on <Date>?"`` & ``"<Coin> price on <Date>?"`` (``binance_1200et``)
3. Daily / weekly / monthly ``"What price will <Coin> hit ...?"`` barriers (``binance_1m``)

Works in two complementary modes:
* **Phase 1 (Taker Snipe)**: Sweeps stale asks immediately after a closed candle
  locks the outcome (``fast_turnover=True``, ``fast_flip=True``).
* **Phase 2 (``maker_binance`` 0-Hedge-Leg Bonding)**: Exposes all decided markets
  (even when ``asks == []``) to ``MakerArbManager`` so it can step 1 tick ahead
  of 0.90–0.991 bonding walls with ``postOnly`` (0 maker fee, 0 leg risk) and
  auto-flip at ``0.995`` or auto-redeem in ~1–2 hours.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from polyarb.config import BinanceClockConfig
from polyarb.data.binance import ET_ZONE, BinanceFeed
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc
from polyarb.strategies.twin import OPEN_HIGH, OPEN_LOW, parse_range_bracket

_ASSET_SYMBOLS: dict[str, str] = {
    "bitcoin": "BTCUSDT",
    "btc": "BTCUSDT",
    "btc_usdt": "BTCUSDT",
    "ethereum": "ETHUSDT",
    "eth": "ETHUSDT",
    "eth_usdt": "ETHUSDT",
    "solana": "SOLUSDT",
    "sol": "SOLUSDT",
    "sol_usdt": "SOLUSDT",
    "xrp": "XRPUSDT",
    "xrp_usdt": "XRPUSDT",
    "bnb": "BNBUSDT",
    "bnb_usdt": "BNBUSDT",
    "dogecoin": "DOGEUSDT",
    "doge": "DOGEUSDT",
    "doge_usdt": "DOGEUSDT",
}
_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

_HOURLY_ABOVE_RE = re.compile(
    r"^(?:Will\s+)?([A-Za-z0-9]+)\s+(?:be\s+)?above\s+___\s+on\s+([A-Za-z]+)\s+(\d{1,2}),\s*(\d{1,2})\s*(AM|PM)\s*ET\??$",
    re.I,
)
_NOON_ABOVE_RE = re.compile(
    r"^(?:Will\s+)?([A-Za-z0-9]+)\s+(?:be\s+)?above\s+___\s+on\s+([A-Za-z]+)\s+(\d{1,2})\??$",
    re.I,
)
_NOON_RANGE_RE = re.compile(
    r"^([A-Za-z0-9]+)\s+price\s+on\s+([A-Za-z]+)\s+(\d{1,2})\??$",
    re.I,
)
_HIT_RE = re.compile(
    r"^What\s+price\s+will\s+([A-Za-z0-9]+)\s+hit\s+(.+?)\??$",
    re.I,
)
_NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")


def _parse_num(text: str) -> float | None:
    m = _NUM_RE.search(text.replace("$", ""))
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _infer_year(month: int, day: int, ref_end: datetime | None) -> int:
    if ref_end is not None:
        et_end = ref_end.astimezone(ET_ZONE)
        if et_end.month == 1 and month == 12:
            return et_end.year - 1
        return et_end.year
    return datetime.now(ET_ZONE).year


def _parse_hit_window(raw_period: str, ref_end: datetime | None) -> tuple[date, date] | None:
    s = raw_period.strip().lower().rstrip("?")
    if m := re.match(r"^on\s+([a-z]+)\s+(\d{1,2})(?:,\s*(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(3)) if m.group(3) else _infer_year(mon, int(m.group(2)), ref_end)
        d = date(yr, mon, int(m.group(2)))
        return (d, d)
    if m := re.match(r"^([a-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})(?:,\s*(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(4)) if m.group(4) else _infer_year(mon, int(m.group(3)), ref_end)
        return (date(yr, mon, int(m.group(2))), date(yr, mon, int(m.group(3))))
    if m := re.match(r"^in\s+([a-z]+)(?:\s+(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(2)) if m.group(2) else _infer_year(mon, 28, ref_end)
        last_day = calendar.monthrange(yr, mon)[1]
        return (date(yr, mon, 1), date(yr, mon, last_day))
    return None


@dataclass(frozen=True, slots=True)
class BinanceTargetSpec:
    kind: str  # "hourly_above" | "noon_above" | "noon_range" | "period_hit"
    symbol: str
    et_key: str  # "YYYY-MM-DD HH:00" or "YYYY-MM-DD"
    start_date: date | None
    end_date: date | None
    items: tuple[tuple[Market, str, float, float], ...]  # (market, dir_or_mode, lo_or_strike, hi)


def parse_binance_event(ev: Event) -> BinanceTargetSpec | None:
    sig = ev.oracle_sig or ""
    if not sig.startswith("binance_"):
        return None
    sig_pair = sig.split(":", 1)[1] if ":" in sig else ""
    t = ev.title.strip()

    if sig.startswith("binance_1h:") and (m := _HOURLY_ABOVE_RE.match(t)):
        sym = _ASSET_SYMBOLS.get(sig_pair) or _ASSET_SYMBOLS.get(m.group(1).lower())
        mon = _MONTHS.get(m.group(2).lower())
        if not sym or not mon:
            return None
        day = int(m.group(3))
        hr12 = int(m.group(4))
        ampm = m.group(5).upper()
        yr = _infer_year(mon, day, ev.end_date)
        hr24 = (0 if hr12 == 12 else hr12) if ampm == "AM" else (12 if hr12 == 12 else hr12 + 12)
        end_dt = datetime(yr, mon, day, hr24, 0, tzinfo=ET_ZONE)
        items = []
        for mkt in ev.tradeable_markets:
            if not mkt.is_yes_no:
                continue
            k = _parse_num(mkt.group_title or mkt.question)
            if k is not None:
                items.append((mkt, "gt", k, OPEN_HIGH))
        if not items:
            return None
        return BinanceTargetSpec("hourly_above", sym, end_dt.strftime("%Y-%m-%d %H:00"), None, None, tuple(items))

    if sig.startswith("binance_1200et:") and (m := _NOON_ABOVE_RE.match(t)):
        sym = _ASSET_SYMBOLS.get(sig_pair) or _ASSET_SYMBOLS.get(m.group(1).lower())
        mon = _MONTHS.get(m.group(2).lower())
        if not sym or not mon:
            return None
        day = int(m.group(3))
        yr = _infer_year(mon, day, ev.end_date)
        d_str = f"{yr:04d}-{mon:02d}-{day:02d}"
        items = []
        for mkt in ev.tradeable_markets:
            if not mkt.is_yes_no:
                continue
            k = _parse_num(mkt.group_title or mkt.question)
            if k is not None:
                items.append((mkt, "gt", k, OPEN_HIGH))
        if not items:
            return None
        return BinanceTargetSpec("noon_above", sym, d_str, None, None, tuple(items))

    if sig.startswith("binance_1200et:") and (m := _NOON_RANGE_RE.match(t)):
        sym = _ASSET_SYMBOLS.get(sig_pair) or _ASSET_SYMBOLS.get(m.group(1).lower())
        mon = _MONTHS.get(m.group(2).lower())
        if not sym or not mon:
            return None
        day = int(m.group(3))
        yr = _infer_year(mon, day, ev.end_date)
        d_str = f"{yr:04d}-{mon:02d}-{day:02d}"
        items = []
        for mkt in ev.tradeable_markets:
            if not mkt.is_yes_no:
                continue
            brk = parse_range_bracket(mkt.group_title)
            if brk is not None:
                items.append((mkt, "range", brk[0], brk[1]))
        if not items:
            return None
        return BinanceTargetSpec("noon_range", sym, d_str, None, None, tuple(items))

    if sig.startswith("binance_1m:") and (m := _HIT_RE.match(t)):
        sym = _ASSET_SYMBOLS.get(sig_pair) or _ASSET_SYMBOLS.get(m.group(1).lower())
        win = _parse_hit_window(m.group(2), ev.end_date)
        if not sym or win is None:
            return None
        items = []
        for mkt in ev.tradeable_markets:
            if not mkt.is_yes_no or mkt.creation_scoped:
                continue
            q = mkt.question.lower()
            k = _parse_num(mkt.group_title or mkt.question)
            if k is None:
                continue
            if "reach" in q or "↑" in mkt.group_title or "(high)" in q:
                items.append((mkt, "up", k, OPEN_HIGH))
            elif "dip" in q or "↓" in mkt.group_title or "(low)" in q:
                items.append((mkt, "down", OPEN_LOW, k))
        if not items:
            return None
        return BinanceTargetSpec("period_hit", sym, f"{win[0]}..{win[1]}", win[0], win[1], tuple(items))

    return None


def guaranteed_binance_outcome(spec: BinanceTargetSpec, item: tuple[Market, str, float, float], feed: BinanceFeed) -> tuple[Outcome, str] | None:
    """Return ``(Outcome, proof_str)`` when 100% decided by closed Binance candles, else None."""
    _, mode, lo, hi = item
    if spec.kind == "hourly_above":
        c = feed.hourly_close(spec.symbol, spec.et_key)
        if c is None:
            return None
        return (Outcome.YES if c > lo else Outcome.NO), f"1h close={c:g}"

    if spec.kind == "noon_above":
        band = feed.noon_close_band(spec.symbol, spec.et_key)
        if band is None:
            return None
        if band[0] > lo:
            return Outcome.YES, f"noon=[{band[0]:g},{band[1]:g}]>{lo:g}"
        if band[1] <= lo:
            return Outcome.NO, f"noon=[{band[0]:g},{band[1]:g}]<={lo:g}"
        return None

    if spec.kind == "noon_range":
        band = feed.noon_close_band(spec.symbol, spec.et_key)
        if band is None:
            return None
        if band[0] >= lo and band[1] < hi:
            return Outcome.YES, f"noon=[{band[0]:g},{band[1]:g}] in bracket"
        if band[1] < lo or band[0] >= hi:
            return Outcome.NO, f"noon=[{band[0]:g},{band[1]:g}] outside bracket"
        return None

    if spec.kind == "period_hit" and spec.start_date and spec.end_date:
        ps = feed.period_summary(spec.symbol, spec.start_date, spec.end_date)
        if ps is None:
            return None
        min_lo, max_hi, finished = ps
        if mode == "up":
            if max_hi >= lo:
                return Outcome.YES, f"high={max_hi:g}>={lo:g}"
            if finished and max_hi < lo:
                return Outcome.NO, f"finished high={max_hi:g}<{lo:g}"
        elif mode == "down":
            if min_lo <= hi:
                return Outcome.YES, f"low={min_lo:g}<={hi:g}"
            if finished and min_lo > hi:
                return Outcome.NO, f"finished low={min_lo:g}>{hi:g}"
    return None


class BinanceClockLock:
    name = "binance_lock"

    def __init__(
        self,
        cfg: BinanceClockConfig,
        feed: BinanceFeed | None = None,
        max_trade_usd: float = math.inf,
        flip_ask_price: float = 0.995,
        flip_min_instant_bid: float = 0.990,
        flip_min_profit_usd: float = 0.08,
    ) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.feed = feed
        self.max_trade_usd = max_trade_usd
        self.flip_ask_price = flip_ask_price
        self.flip_min_instant_bid = flip_min_instant_bid
        self.flip_min_profit_usd = flip_min_profit_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out: list[Target] = []
        for ev in events:
            spec = parse_binance_event(ev)
            if spec is None:
                continue
            tokens = tuple(m.yes_token for m, _, _, _ in spec.items)
            out.append(Target(f"binance:{ev.id}", self.name, ev, tokens, spec))
        return out

    def locked_markets(self, target: Target, now_dt: datetime) -> list[tuple[Market, Outcome, datetime]]:
        """Return ``(market, locked_outcome, resolves_at)`` for 0-hedge-leg maker bonding."""
        if self.feed is None:
            return []
        spec: BinanceTargetSpec = target.payload
        ev = target.event
        out: list[tuple[Market, Outcome, datetime]] = []
        for item in spec.items:
            mkt = item[0]
            res = guaranteed_binance_outcome(spec, item, self.feed)
            if res is None:
                continue
            locked, _ = res
            end_dt = mkt.end_date or ev.end_date
            resolves_at = (
                max(end_dt, now_dt) + timedelta(days=self.cfg.resolution_lag_days)
                if end_dt
                else (now_dt + timedelta(hours=3))
            )
            if (resolves_at - now_dt).total_seconds() / 86400.0 <= self.cfg.max_days:
                out.append((mkt, locked, resolves_at))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        if self.feed is None:
            return None
        spec: BinanceTargetSpec = target.payload
        best: float | None = None
        for item in spec.items:
            res = guaranteed_binance_outcome(spec, item, self.feed)
            if res is None:
                continue
            mkt = item[0]
            locked, _ = res
            book = books.get(mkt.yes_token)
            if book is None:
                continue
            levels = book.asks if locked is Outcome.YES else book.no_asks()
            c = unit_cost(levels, mkt.fee)
            if c is not None:
                edge = 1.0 - c
                if best is None or edge > best:
                    best = edge
        return best

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        if self.feed is None:
            return []
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        spec: BinanceTargetSpec = target.payload
        ev = target.event
        now_dt = utc(now)
        found: list[Opportunity] = []

        for item in spec.items:
            res = guaranteed_binance_outcome(spec, item, self.feed)
            if res is None:
                continue
            mkt = item[0]
            locked, proof = res
            book = books.get(mkt.yes_token)
            if book is None:
                continue
            levels = book.asks if locked is Outcome.YES else book.no_asks()
            if not levels:
                continue
            first_cost = unit_cost(levels, mkt.fee)
            if first_cost is None or 1.0 - first_cost < cfg.min_unit_edge:
                continue
            end_dt = mkt.end_date or ev.end_date
            resolves_at = (
                max(end_dt, now_dt) + timedelta(days=cfg.resolution_lag_days)
                if end_dt
                else (now_dt + timedelta(hours=3))
            )
            if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
                continue
            fill = size_basket([LegBook(levels, mkt.fee, mkt.min_size)], 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
            if fill is None:
                continue
            opp = Opportunity(
                strategy=self.name,
                key=f"{target.key}:{mkt.id}:{locked.value}",
                event_id=ev.id,
                title=ev.title,
                legs=[buy_leg(mkt, locked, fill.fills[0], f"{locked.value} {mkt.group_title or mkt.question[:50]}")],
                shares=fill.shares,
                cost=fill.cost,
                payout_floor=fill.payout,
                settlement=Settlement.RESOLUTION,
                risk="low",
                resolves_at=resolves_at,
                detected_at=now,
                meta={
                    "symbol": spec.symbol,
                    "kind": spec.kind,
                    "proof": proof,
                    "fast_turnover": True,
                    "fast_flip": cfg.flip_enabled,
                    "flip_ask": self.flip_ask_price,
                    "flip_min_bid": self.flip_min_instant_bid,
                    "flip_min_profit_usd": self.flip_min_profit_usd,
                    "independent_legs": True,
                    "condition_ids": [mkt.condition_id] if mkt.condition_id else [],
                },
            )
            if opp.profit >= cfg.min_profit_usd and opp.edge >= cfg.min_edge:
                found.append(opp)

        found.sort(key=lambda o: -o.profit)
        return found
