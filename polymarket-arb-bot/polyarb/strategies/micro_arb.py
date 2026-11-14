"""BTC 5-minute micro arbitrage (``micro_arb``).

Three engines, each round capped by ``max_cost_usd`` (default $10, both legs):

* **merge** — buy UP and DOWN of the same 5m market only when the two real
  token asks, fees included, sum to at most ``merge_max_combined``, then
  ``mergePositions`` back to pUSD. The YES-book mirror cannot show this, so
  both token books are read.
* **corridor** — on the last 5m of a 15m window, when the two strikes differ
  by at least ``min_strike_gap``, buy the low strike's UP and the high
  strike's DOWN. Floor payout is $1. A one-leg fill is always sold back.
* **tail** — in the last ``tail_last_s`` seconds, buy the leading side only
  when the remaining prints would have to move at least
  ``tail_min_reversal_usd`` to cross the strike, and the ask is at or under
  ``tail_max_ask``.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Iterable

log = logging.getLogger(__name__)

_MICRO_STRATEGIES = ("micro_arb", "maker_micro_merge", "maker_micro_corridor")


def round_slot_open(busy: set[str], event_id: str, cap: int) -> bool:
    """True when this 5m round may still take its $10 slot.

    A round already in ``busy`` can finish its own entry. A new round waits
    until fewer than ``cap`` rounds are still holding or quoting, so redeem
    of the previous round does not block the next one until three are open.
    """
    if cap <= 0 or event_id in busy:
        return True
    return len(busy) < cap

from polyarb.config import MicroArbConfig
from polyarb.data.rtds import ChainlinkRtdsFeed
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, floor_shares, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc
from polyarb.strategies.crypto_short import (
    ShortSingleSpec,
    Sync5m15mSpec,
    _side_label,
    parse_short_single,
)

_ASSET = "btc"


class MicroArb:
    name = "micro_arb"

    def __init__(
        self,
        cfg: MicroArbConfig,
        rtds_feed: ChainlinkRtdsFeed | None = None,
        max_trade_usd: float = float("inf"),
        flip_ask_price: float = 0.994,
        flip_min_instant_bid: float = 0.990,
        flip_min_profit_usd: float = 0.04,
    ) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.rtds_feed = rtds_feed
        self.max_trade_usd = max_trade_usd
        self.max_cost_usd = cfg.max_cost_usd
        self.flip_ask_price = flip_ask_price
        self.flip_min_instant_bid = flip_min_instant_bid
        self.flip_min_profit_usd = flip_min_profit_usd
        self.safe_skips: set[int] = set()

    def targets(self, events: Iterable[Event]) -> list[Target]:
        if not self.enabled:
            return []
        singles_5m: dict[tuple[str, int], tuple[Event, ShortSingleSpec]] = {}
        singles_15m: dict[tuple[str, int], tuple[Event, ShortSingleSpec]] = {}
        out: list[Target] = []
        for ev in events:
            spec = parse_short_single(ev)
            if spec is None or spec.asset != _ASSET:
                continue
            if spec.duration == "5m":
                singles_5m[(spec.asset, spec.end_ts)] = (ev, spec)
                out.append(
                    Target(
                        f"micro:{ev.id}",
                        self.name,
                        ev,
                        (spec.market.yes_token, spec.market.no_token),
                        spec,
                    )
                )
            elif spec.duration == "15m":
                singles_15m[(spec.asset, spec.end_ts)] = (ev, spec)
        if not self.cfg.corridor_enabled:
            return out
        for key, (ev15, sp15) in singles_15m.items():
            hit = singles_5m.get(key)
            if hit is None:
                continue
            ev5, sp5 = hit
            if sp5.start_ts != sp15.start_ts + 600:
                continue
            pair = Sync5m15mSpec(
                asset=sp15.asset,
                start_15m=sp15.start_ts,
                start_5m=sp5.start_ts,
                end_ts=sp15.end_ts,
                ev_15m=ev15,
                ev_5m=ev5,
                mkt_15m=sp15.market,
                mkt_5m=sp5.market,
            )
            out.append(
                Target(
                    f"micro-sync:{ev5.id}:{ev15.id}",
                    self.name,
                    ev5,
                    (sp5.market.yes_token, sp15.market.yes_token),
                    pair,
                )
            )
        return out

    def _cap(self, max_cost: float | None) -> float:
        cap = min(self.max_cost_usd, self.max_trade_usd)
        if max_cost is not None:
            cap = min(cap, max_cost)
        return cap

    def _strike(self, ev: Event, spec: ShortSingleSpec) -> float | None:
        if ev.price_to_beat is not None and ev.price_to_beat > 0:
            return ev.price_to_beat
        if self.rtds_feed is None:
            return None
        return self.rtds_feed.twap_at(spec.asset, spec.start_ts, tolerance_s=0)

    def corridor_legs(
        self, spec: Sync5m15mSpec, now_s: float
    ) -> tuple[list[tuple[Market, Outcome]], datetime, str, list[str]] | None:
        cfg = self.cfg
        if not (spec.start_5m <= now_s < spec.end_ts - cfg.tail_min_left_s):
            return None
        sp15 = ShortSingleSpec("twap_single", spec.asset, "15m", spec.start_15m, spec.end_ts, spec.mkt_15m)
        sp5 = ShortSingleSpec("twap_single", spec.asset, "5m", spec.start_5m, spec.end_ts, spec.mkt_5m)
        s15 = self._strike(spec.ev_15m, sp15)
        s5 = self._strike(spec.ev_5m, sp5)
        if s15 is None or s5 is None or abs(s5 - s15) + 1e-9 < cfg.min_strike_gap:
            return None
        resolves_at = datetime.fromtimestamp(spec.end_ts + 90, tz=utc(now_s).tzinfo)
        if s5 > s15:
            legs = [(spec.mkt_5m, Outcome.NO), (spec.mkt_15m, Outcome.YES)]
            title = f"BTC 走廊 5m跌<{s5:.0f} ⇄ 15m升≥{s15:.0f}"
        else:
            legs = [(spec.mkt_5m, Outcome.YES), (spec.mkt_15m, Outcome.NO)]
            title = f"BTC 走廊 5m升≥{s5:.0f} ⇄ 15m跌<{s15:.0f}"
        return legs, resolves_at, title, [spec.ev_5m.id, spec.ev_15m.id]

    def _asks(self, market: Market, outcome: Outcome, books: Books) -> list:
        direct = books.get(market.token(outcome))
        if direct is not None and direct.asks:
            return direct.asks
        yes = books.get(market.yes_token)
        if yes is None:
            return []
        return yes.asks if outcome is Outcome.YES else yes.no_asks()

    def _sized(self, legs: list[LegBook], min_unit_edge: float, cap: float):
        if cap <= 0 or not legs:
            return None
        tops: list[float] = []
        for leg in legs:
            if not leg.levels:
                return None
            top = leg.levels[0].size * self.cfg.taker_top_fraction
            if top + 1e-9 < leg.min_size:
                top = leg.levels[0].size
            tops.append(top)
        return size_basket(
            legs,
            1.0,
            min_unit_edge=min_unit_edge,
            max_shares=floor_shares(min(tops)),
            max_cost=cap,
        )

    def top_edge(self, target: Target, books: Books) -> float | None:
        return self._edge(target, books, time.time())

    def _edge(self, target: Target, books: Books, now_s: float) -> float | None:
        payload = target.payload
        if isinstance(payload, ShortSingleSpec):
            edges: list[float] = []
            elapsed = now_s - payload.start_ts
            if self.cfg.merge_enabled and self.cfg.merge_start_s <= elapsed <= self.cfg.merge_window_s:
                up = self._asks(payload.market, Outcome.YES, books)
                down = self._asks(payload.market, Outcome.NO, books)
                cu, cd = unit_cost(up, payload.market.fee), unit_cost(down, payload.market.fee)
                if cu is not None and cd is not None:
                    edges.append(1.0 - (cu + cd))
            if self.cfg.tail_enabled:
                locked = self._tail_state(target.event, payload, now_s)
                if locked is not None:
                    levels = self._asks(payload.market, locked[0], books)
                    c = unit_cost(levels, payload.market.fee)
                    if c is not None:
                        edges.append(1.0 - c)
            return max(edges) if edges else None
        if isinstance(payload, Sync5m15mSpec) and self.cfg.corridor_enabled:
            pair = self.corridor_legs(payload, now_s)
            if pair is None:
                return None
            total = 0.0
            for market, outcome in pair[0]:
                c = unit_cost(self._asks(market, outcome, books), market.fee)
                if c is None:
                    return None
                total += c
            return 1.0 - total
        return None

    def _tail_state(self, ev: Event, spec: ShortSingleSpec, now_s: float):
        cfg = self.cfg
        rem = spec.end_ts - now_s
        if self.rtds_feed is None or not (cfg.tail_min_left_s <= rem <= cfg.tail_last_s):
            return None
        open_px = self._strike(ev, spec)
        if open_px is None or open_px <= 0:
            return None
        return self.rtds_feed.tail_reversal_lock(
            spec.asset, open_px, spec.end_ts, now_s, cfg.tail_min_reversal_usd
        )

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        if not self.enabled:
            return []
        cap = self._cap(max_cost)
        if cap <= 0:
            return []
        payload = target.payload
        if isinstance(payload, ShortSingleSpec):
            found: list[Opportunity] = []
            merged = self._eval_merge(target, payload, books, now, cap)
            if merged is not None:
                found.append(merged)
            tail = self._eval_tail(target, payload, books, now, cap)
            if tail is not None:
                found.append(tail)
            return found
        if isinstance(payload, Sync5m15mSpec):
            opp = self._eval_corridor(target, payload, books, now, cap)
            return [opp] if opp is not None else []
        return []

    def _accept(self, opp: Opportunity) -> Opportunity | None:
        cfg = self.cfg
        if opp.profit + 1e-9 < cfg.min_profit_usd or opp.edge + 1e-12 < cfg.min_edge:
            return None
        if opp.cost > self._cap(None) + 1e-6:
            return None
        return opp

    def _eval_merge(
        self, target: Target, spec: ShortSingleSpec, books: Books, now: float, cap: float
    ) -> Opportunity | None:
        cfg = self.cfg
        elapsed = now - spec.start_ts
        if not cfg.merge_enabled or not (cfg.merge_start_s <= elapsed <= cfg.merge_window_s):
            return None
        market = spec.market
        # Both token books. The YES mirror of NO is 1 - bid, so it can never sum under $1.
        yes_book = books.get(market.yes_token)
        no_book = books.get(market.no_token)
        if yes_book is None or no_book is None or not yes_book.asks or not no_book.asks:
            return None
        legs = [
            LegBook(yes_book.asks, market.fee, market.min_size),
            LegBook(no_book.asks, market.fee, market.min_size),
        ]
        if sum(unit_cost(leg.levels, leg.fee) or 1.0 for leg in legs) > cfg.merge_max_combined + 1e-9:
            return None
        fill = self._sized(legs, max(cfg.min_edge, 1.0 - cfg.merge_max_combined), cap)
        if fill is None:
            return None
        outcomes = (Outcome.YES, Outcome.NO)
        built = [
            buy_leg(market, out, one, f"{_side_label(market, out)} {target.event.title[:42]}")
            for out, one in zip(outcomes, fill.fills)
        ]
        opp = Opportunity(
            strategy=self.name,
            key=f"{target.key}:merge",
            event_id=target.event.id,
            title=f"BTC 5m 同盤合成 {target.event.title[:42]}",
            legs=built,
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=utc(spec.end_ts + 90),
            detected_at=now,
            meta={
                "mode": "micro_merge",
                "asset": spec.asset,
                "duration": "5m",
                "fast_turnover": True,
                "condition_ids": [market.condition_id] if market.condition_id else [],
            },
        )
        return self._accept(opp)

    def _eval_tail(
        self, target: Target, spec: ShortSingleSpec, books: Books, now: float, cap: float
    ) -> Opportunity | None:
        cfg = self.cfg
        if not cfg.tail_enabled:
            return None
        state = self._tail_state(target.event, spec, now)
        if state is None:
            self._note_safe_skip(target.event, spec, now)
            return None
        locked, req, proof = state
        market = spec.market
        levels = [lvl for lvl in self._asks(market, locked, books) if lvl.price <= cfg.tail_max_ask + 1e-9]
        if not levels:
            return None
        fill = self._sized([LegBook(levels, market.fee, market.min_size)], cfg.min_edge, cap)
        if fill is None:
            return None
        opp = Opportunity(
            strategy=self.name,
            key=f"{target.key}:tail:{locked.value}",
            event_id=target.event.id,
            title=target.event.title,
            legs=[buy_leg(market, locked, fill.fills[0], f"{_side_label(market, locked)} {target.event.title[:42]}")],
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=utc(spec.end_ts + 90),
            detected_at=now,
            meta={
                "mode": "micro_tail",
                "asset": spec.asset,
                "duration": "5m",
                "proof": proof,
                "reversal_usd": round(req, 1),
                "fast_turnover": True,
                "fast_flip": True,
                "flip_ask": self.flip_ask_price,
                "flip_min_bid": self.flip_min_instant_bid,
                "flip_min_profit_usd": self.flip_min_profit_usd,
                "independent_legs": True,
                "condition_ids": [market.condition_id] if market.condition_id else [],
            },
        )
        return self._accept(opp)

    def _eval_corridor(
        self, target: Target, spec: Sync5m15mSpec, books: Books, now: float, cap: float
    ) -> Opportunity | None:
        cfg = self.cfg
        if not cfg.corridor_enabled:
            return None
        elapsed = now - spec.start_5m
        if cfg.merge_enabled and cfg.merge_start_s <= elapsed <= cfg.merge_window_s:
            merge_cost = self._merge_cost(spec.mkt_5m, books)
            if merge_cost is not None and merge_cost <= cfg.merge_max_combined + 1e-9:
                return None
        pair = self.corridor_legs(spec, now)
        if pair is None:
            return None
        leg_specs, resolves_at, title, ev_ids = pair
        leg_books: list[LegBook] = []
        for market, outcome in leg_specs:
            levels = self._asks(market, outcome, books)
            if not levels:
                return None
            leg_books.append(LegBook(levels, market.fee, market.min_size))
        if sum(unit_cost(lb.levels, lb.fee) or 1.0 for lb in leg_books) > cfg.corridor_max_combined + 1e-9:
            return None
        fill = self._sized(leg_books, max(cfg.min_edge, 1.0 - cfg.corridor_max_combined), cap)
        if fill is None:
            return None
        legs = [
            buy_leg(market, outcome, one, f"{_side_label(market, outcome)} {market.group_title or market.question[:42]}")
            for (market, outcome), one in zip(leg_specs, fill.fills)
        ]
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=target.event.id,
            title=title,
            legs=legs,
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=resolves_at,
            detected_at=now,
            meta={
                "mode": "micro_corridor",
                "asset": spec.asset,
                "fast_turnover": True,
                "event_ids": ev_ids,
                "condition_ids": [m.condition_id for m, _ in leg_specs if m.condition_id],
            },
        )
        return self._accept(opp)

    def _merge_cost(self, market: Market, books: Books) -> float | None:
        yes = books.get(market.yes_token)
        no = books.get(market.no_token)
        if yes is None or no is None or not yes.asks or not no.asks:
            return None
        up = unit_cost(yes.asks, market.fee)
        down = unit_cost(no.asks, market.fee)
        if up is None or down is None:
            return None
        return up + down

    def _note_safe_skip(self, ev: Event, spec: ShortSingleSpec, now_s: float) -> None:
        """One log per round when the tail is close enough to the strike to flip."""
        cfg = self.cfg
        rem = spec.end_ts - now_s
        if self.rtds_feed is None or not (cfg.tail_min_left_s <= rem <= cfg.tail_last_s):
            return
        if spec.end_ts in self.safe_skips:
            return
        open_px = self._strike(ev, spec)
        if open_px is None:
            return
        measured = self.rtds_feed.window_reversal_usd(spec.asset, open_px, spec.end_ts, now_s)
        if measured is None:
            return
        _lead, req, _spot, _twap = measured
        if req + 1e-9 >= cfg.tail_min_reversal_usd:
            return
        self.safe_skips.add(spec.end_ts)
        log.info(
            "micro safe skip %s reversal $%.0f under $%.0f",
            ev.title[:48],
            req,
            cfg.tail_min_reversal_usd,
        )
