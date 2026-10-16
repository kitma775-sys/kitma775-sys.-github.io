"""Monotone ladder arbitrage (risk: medium).

If rung A implies rung B (A stronger), then P(B) >= P(A) and the pair
"YES on B + NO on A" pays at least $1 in every state of the world:

    A true          -> B true:   YES_B pays 1, NO_A pays 0
    A false, B true ->           YES_B pays 1, NO_A pays 1   (bonus)
    A false, B false ->          YES_B pays 0, NO_A pays 1

With NO_A ask = 1 - bid_A, the trade is profitable when
``ask_B + fee_B < bid_A - fee_A``. The guaranteed dollar is released once both
markets resolve, so the opportunity is gated on time-to-resolution and APR.
"""

from __future__ import annotations

import math
from datetime import timedelta
from typing import Iterable

from polyarb.config import LadderConfig
from polyarb.fees import fee_per_share
from polyarb.models import Event, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket
from polyarb.strategies.base import Books, Target, buy_leg, utc
from polyarb.strategies.ladder_parse import Ladder, detect_ladders


class LadderArb:
    name = "ladder"

    def __init__(self, cfg: LadderConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out = []
        allowed = set(self.cfg.directions)
        for ev in events:
            for ladder in detect_ladders(ev):
                if ladder.direction.value in allowed:
                    out.append(Target(f"ladder:{ladder.key}", self.name, ev, tuple(ladder.tokens), ladder))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        """Best 1 - (NO_strong + fee) - (YES_weak + fee) over all implication pairs."""
        best = None
        rungs = target.payload.rungs
        for i, strong in enumerate(rungs):
            book_a = books.get(strong.market.yes_token)
            if book_a is None or not book_a.bids:
                continue
            p_no = 1.0 - book_a.bids[0].price
            c_no = p_no + fee_per_share(strong.market.fee, p_no)
            for weak in rungs[i + 1:]:
                book_b = books.get(weak.market.yes_token)
                if book_b is None or not book_b.asks:
                    continue
                p_yes = book_b.asks[0].price
                edge = 1.0 - c_no - p_yes - fee_per_share(weak.market.fee, p_yes)
                if best is None or edge > best:
                    best = edge
        return best

    def evaluate(self, target: Target, books: Books, now: float) -> list[Opportunity]:
        cfg = self.cfg
        ladder: Ladder = target.payload
        rungs = ladder.rungs
        now_dt = utc(now)
        found: list[Opportunity] = []
        for i, strong in enumerate(rungs):
            book_a = books.get(strong.market.yes_token)
            if book_a is None or not book_a.bids:
                continue
            no_a = book_a.no_asks()
            c_no = no_a[0].price + fee_per_share(strong.market.fee, no_a[0].price)
            for weak in rungs[i + 1:]:
                book_b = books.get(weak.market.yes_token)
                if book_b is None or not book_b.asks:
                    continue
                yes_b = book_b.asks
                c_yes = yes_b[0].price + fee_per_share(weak.market.fee, yes_b[0].price)
                if 1.0 - c_no - c_yes < cfg.min_unit_edge:
                    continue
                ends = [d for d in (strong.market.end_date, weak.market.end_date) if d]
                if not ends:
                    continue
                resolves_at = max(ends) + timedelta(days=cfg.resolution_lag_days)
                if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
                    continue
                fill = size_basket(
                    [LegBook(yes_b, weak.market.fee, weak.market.min_size), LegBook(no_a, strong.market.fee, strong.market.min_size)],
                    1.0,
                    min_unit_edge=cfg.min_unit_edge,
                    max_cost=self.max_trade_usd,
                )
                if fill is None:
                    continue
                opp = Opportunity(
                    strategy=self.name,
                    key=f"{target.key}:{strong.market.id}>{weak.market.id}",
                    event_id=ladder.event.id,
                    title=ladder.event.title,
                    legs=[
                        buy_leg(weak.market, Outcome.YES, fill.fills[0], f"YES {weak.label}"),
                        buy_leg(strong.market, Outcome.NO, fill.fills[1], f"NO {strong.label}"),
                    ],
                    shares=fill.shares,
                    cost=fill.cost,
                    payout_floor=fill.payout,
                    settlement=Settlement.RESOLUTION,
                    risk="medium",
                    resolves_at=resolves_at,
                    detected_at=now,
                    meta={
                        "direction": ladder.direction.value,
                        "strong": strong.market.question,
                        "weak": weak.market.question,
                        "early_release_at": min(ends).isoformat(),
                        "bonus_state": "pays 2 if strong=NO and weak=YES",
                    },
                )
                apr = opp.apr(now_dt)
                if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge:
                    continue
                if apr is not None and apr < cfg.min_apr:
                    continue
                found.append(opp)
        found.sort(key=lambda o: -o.profit)
        return found[:1]
