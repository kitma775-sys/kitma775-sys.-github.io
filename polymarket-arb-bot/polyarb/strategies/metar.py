"""NOAA/AWC METAR deterministic temperature lock + surviving weather basket (risk: low).

Uses real-time routine METAR observations from ``aviationweather.gov`` (Polymarket's
official primary resolution source) to trade only mathematically decided outcomes:

1. Single-bracket lock:
   * Highest-temp event: once running high reaches T, any bracket strictly below T
     is guaranteed NO ($1), and an open top bracket ``<= T`` is guaranteed YES ($1).
   * Lowest-temp event: once running low reaches T, any bracket strictly above T
     is guaranteed NO ($1), and an open bottom bracket ``>= T`` is guaranteed YES ($1).
   * Finished day (station already issued a routine METAR on local date > obs_date):
     the bracket containing the day's extreme is guaranteed YES ($1) and all others NO ($1).

2. METAR-pruned surviving YES basket:
   * Drops all METAR-eliminated brackets (avoiding dead 0.1c legs that would block
     the $1 minimum order notional) and buys YES across only the surviving brackets
     when their combined cost + taker fees < $1.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from polyarb.config import MetarConfig
from polyarb.data.metar import ICAO_TIMEZONES, MetarFeed, StationDaySummary
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_LE_RE = re.compile(r"^(-?\d+)\s*°?([CF])\s*or\s*(?:below|lower|less|under)$", re.I)
_GE_RE = re.compile(r"^(-?\d+)\s*°?([CF])\s*or\s*(?:higher|above|more|over)$", re.I)
_RNG_RE = re.compile(r"^(-?\d+)\s*-\s*(-?\d+)\s*°?([CF])$", re.I)
_EX_RE = re.compile(r"^(-?\d+)\s*°?([CF])$", re.I)


@dataclass(frozen=True, slots=True)
class TempBracket:
    market: Market
    lo: float
    hi: float
    unit: str


def parse_temp_bracket(market: Market) -> TempBracket | None:
    s = (market.group_title or "").strip().replace("−", "-")
    if not s:
        return None
    if m := _LE_RE.match(s):
        return TempBracket(market, -math.inf, float(int(m.group(1))), m.group(2).upper())
    if m := _GE_RE.match(s):
        return TempBracket(market, float(int(m.group(1))), math.inf, m.group(2).upper())
    if m := _RNG_RE.match(s):
        a, b = int(m.group(1)), int(m.group(2))
        return TempBracket(market, float(min(a, b)), float(max(a, b)), m.group(3).upper())
    if m := _EX_RE.match(s):
        v = float(int(m.group(1)))
        return TempBracket(market, v, v, m.group(2).upper())
    return None


def guaranteed_outcome(brk: TempBracket, summary: StationDaySummary, mode: str) -> Outcome | None:
    """Return the 100% mathematically locked outcome for ``brk``, or None if still open."""
    stat_val = summary.stat(mode, brk.unit)
    if summary.day_finished:
        return Outcome.YES if (brk.lo <= stat_val <= brk.hi) else Outcome.NO
    if mode == "high":
        if brk.hi < stat_val:
            return Outcome.NO
        if brk.lo <= stat_val and brk.hi == math.inf:
            return Outcome.YES
    elif mode == "low":
        if brk.lo > stat_val:
            return Outcome.NO
        if brk.hi >= stat_val and brk.lo == -math.inf:
            return Outcome.YES
    return None


class MetarLock:
    name = "metar_lock"

    def __init__(self, cfg: MetarConfig, feed: MetarFeed | None = None, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.feed = feed
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out: list[Target] = []
        for ev in events:
            if not (ev.metar_icao and ev.metar_obs_date and ev.metar_mode):
                continue
            if ev.metar_icao.upper() not in ICAO_TIMEZONES:
                continue
            brackets: list[TempBracket] = []
            all_parsed = True
            for m in ev.tradeable_markets:
                if not m.is_yes_no:
                    all_parsed = False
                    continue
                brk = parse_temp_bracket(m)
                if brk is None:
                    all_parsed = False
                else:
                    brackets.append(brk)
            if not brackets:
                continue
            out.append(
                Target(
                    f"metar:{ev.id}",
                    self.name,
                    ev,
                    tuple(b.market.yes_token for b in brackets),
                    (tuple(brackets), all_parsed),
                )
            )
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        if self.feed is None:
            return None
        ev = target.event
        summary = self.feed.get(ev.metar_icao, ev.metar_obs_date)
        if summary is None:
            return None
        brackets, all_parsed = target.payload
        mode = ev.metar_mode or "high"
        best: float | None = None
        surviving: list[Market] = []

        for brk in brackets:
            m = brk.market
            locked = guaranteed_outcome(brk, summary, mode)
            book = books.get(m.yes_token)
            if locked is Outcome.NO:
                if book is not None:
                    c = unit_cost(book.no_asks(), m.fee)
                    if c is not None:
                        edge = 1.0 - c
                        if best is None or edge > best:
                            best = edge
            elif locked is Outcome.YES:
                if book is not None:
                    c = unit_cost(book.asks, m.fee)
                    if c is not None:
                        edge = 1.0 - c
                        if best is None or edge > best:
                            best = edge
                surviving.append(m)
            else:
                surviving.append(m)

        if all_parsed and ev.neg_risk and not ev.neg_risk_augmented and 1 < len(surviving) < len(brackets):
            if all(m.resolved_outcome in (None, "NO") for m in ev.markets):
                total = 0.0
                ok = True
                for sm in surviving:
                    sb = books.get(sm.yes_token)
                    c = unit_cost(sb.asks, sm.fee) if sb is not None else None
                    if c is None:
                        ok = False
                        break
                    total += c
                if ok:
                    edge = 1.0 - total
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
        ev = target.event
        summary = self.feed.get(ev.metar_icao, ev.metar_obs_date)
        if summary is None:
            return []
        brackets, all_parsed = target.payload
        mode = ev.metar_mode or "high"
        now_dt = utc(now)
        found: list[Opportunity] = []
        surviving: list[Market] = []

        for brk in brackets:
            m = brk.market
            locked = guaranteed_outcome(brk, summary, mode)
            if locked is None:
                surviving.append(m)
                continue
            if locked is Outcome.YES:
                surviving.append(m)
            book = books.get(m.yes_token)
            if book is None:
                continue
            levels = book.asks if locked is Outcome.YES else book.no_asks()
            if not levels:
                continue
            first_cost = unit_cost(levels, m.fee)
            if first_cost is None or 1.0 - first_cost < cfg.min_unit_edge:
                continue
            end_dt = m.end_date or ev.end_date
            resolves_at = (end_dt + timedelta(days=cfg.resolution_lag_days)) if end_dt else (now_dt + timedelta(days=1.0))
            if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
                continue
            fill = size_basket([LegBook(levels, m.fee, m.min_size)], 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
            if fill is None:
                continue
            stat_val = summary.stat(mode, brk.unit)
            opp = Opportunity(
                strategy=self.name,
                key=f"{target.key}:{m.id}:{locked.value}",
                event_id=ev.id,
                title=ev.title,
                legs=[buy_leg(m, locked, fill.fills[0], f"{locked.value} {m.group_title or m.question[:50]}")],
                shares=fill.shares,
                cost=fill.cost,
                payout_floor=fill.payout,
                settlement=Settlement.RESOLUTION,
                risk="low",
                resolves_at=resolves_at,
                detected_at=now,
                meta={
                    "icao": summary.icao,
                    "obs_date": summary.obs_date,
                    "mode": mode,
                    "observed": f"{stat_val}°{brk.unit}",
                    "day_finished": summary.day_finished,
                    "obs_count": summary.count,
                    "fast_turnover": True,
                    "fast_flip": cfg.flip_enabled,
                    "flip_ask": cfg.flip_ask_price,
                    "flip_min_bid": cfg.flip_min_instant_bid,
                    "flip_min_profit_usd": cfg.flip_min_profit_usd,
                    "condition_ids": [m.condition_id] if m.condition_id else [],
                },
            )
            if opp.profit >= cfg.min_profit_usd and opp.edge >= cfg.min_edge:
                found.append(opp)

        # Surviving-only NegRisk YES basket (all eliminated legs dropped).
        if (
            all_parsed
            and ev.neg_risk
            and not ev.neg_risk_augmented
            and ev.version == "v1"
            and 1 < len(surviving) < len(brackets)
            and all(not m.accepting_orders and m.closed and m.resolved_outcome == "NO" or m.accepting_orders for m in ev.markets)
            and all(m.resolved_outcome in (None, "NO") for m in ev.markets)
        ):
            legs = []
            ok = True
            for sm in surviving:
                sb = books.get(sm.yes_token)
                if sb is None or not sb.asks:
                    ok = False
                    break
                legs.append(LegBook(sb.asks, sm.fee, sm.min_size))
            if ok and sum(unit_cost(lb.levels, lb.fee) or 1.0 for lb in legs) <= 1.0 - cfg.min_unit_edge:
                ends = [sm.end_date for sm in surviving if sm.end_date]
                resolves_at = (max(ends) if ends else (ev.end_date or now_dt)) + timedelta(days=cfg.resolution_lag_days)
                if (resolves_at - now_dt).total_seconds() / 86400.0 <= cfg.max_days:
                    fill = size_basket(legs, 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
                    if fill is not None:
                        opp = Opportunity(
                            strategy=self.name,
                            key=f"{target.key}:basket:{len(surviving)}",
                            event_id=ev.id,
                            title=ev.title,
                            legs=[buy_leg(sm, Outcome.YES, f, f"YES {sm.group_title}") for sm, f in zip(surviving, fill.fills)],
                            shares=fill.shares,
                            cost=fill.cost,
                            payout_floor=fill.payout,
                            settlement=Settlement.RESOLUTION,
                            risk="low",
                            resolves_at=resolves_at,
                            detected_at=now,
                            meta={
                                "icao": summary.icao,
                                "obs_date": summary.obs_date,
                                "surviving_legs": len(surviving),
                                "total_legs": len(brackets),
                                "fast_turnover": True,
                                "condition_ids": [sm.condition_id for sm in surviving if sm.condition_id],
                            },
                        )
                        if opp.profit >= cfg.basket_min_profit_usd and opp.edge >= cfg.basket_min_edge:
                            found.append(opp)

        found.sort(key=lambda o: -o.profit)
        return found
