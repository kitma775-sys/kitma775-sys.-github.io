"""NegRisk (mutually exclusive multi-outcome) strategies.

NO-basket convert (risk: low)
    Buy NO on a subset S (|S| >= 2) of an event's questions, then call
    ``NegRiskCtfCollateralAdapter.convertPositions(marketId, indexSet, q)`` which
    burns the NOs and returns ``(|S| - 1) * q`` pUSD **plus ``q`` free YES shares on
    every question outside S**.

    v0.6 upgrades:
    1. **Depth-Tier Subset Optimizer**: Evaluates candidate share-depth tiers ``Q``
       across the book so a thin 5-share retail bid on one outcome can NEVER poison
       or cap a 50-share arbitrage across the remaining deep outcomes.
    2. **Concentrated Convert + Surplus YES Liquidation**: Prices deep resting YES
       bids on ``m not in S`` (with exact taker fee + 1-tick cushion + conservative
       haircut) and immediately FAK-sells minted free YES tokens after conversion.

YES-basket (risk: medium)
    Buy YES on every outcome of an exhaustive event; exactly one pays $1 at
    resolution.
"""

from __future__ import annotations

import math
import re
from datetime import timedelta
from typing import Iterable, Sequence

from polyarb.book import EPS, take
from polyarb.config import ConvertConfig, YesBasketConfig
from polyarb.fees import fee_per_share
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import MIN_ORDER_NOTIONAL, BasketFill, LegBook, floor_shares, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_CATCH_ALL = re.compile(
    r"\b(other|others|none|no one|nobody|neither|no change|unchanged|field|any other|someone else|"
    r"not listed|no winner|tie|draw|no agreement|no deal)\b",
    re.I,
)
_LOW_OPEN = re.compile(r"^\s*[<≤↓]|\bor (less|fewer|lower|below)\b|\b(under|below|less than|fewer than)\b", re.I)
_HIGH_OPEN = re.compile(r"^\s*[>≥↑]|\d\s*\+|\bor (more|higher|above|greater)\b|\b(over|above|more than|greater than|at least)\b", re.I)


def exhaustive_reason(event: Event) -> str | None:
    """Heuristic evidence that the listed outcomes cover every possibility."""
    if any(m.neg_risk_other for m in event.markets):
        return "other-market"
    labels = [m.group_title or m.question for m in event.markets]
    if any(_CATCH_ALL.search(label) for label in labels):
        return "catch-all-label"
    if any(_LOW_OPEN.search(label) for label in labels) and any(_HIGH_OPEN.search(label) for label in labels):
        return "open-ended-partition"
    return None


def _convertible(event: Event) -> list[Market]:
    if not event.neg_risk or event.version != "v1" or not event.neg_risk_market_id:
        return []
    return [
        m
        for m in event.tradeable_markets
        if m.is_yes_no and m.neg_risk_market_id == event.neg_risk_market_id and m.neg_risk_index is not None
    ]


def surplus_yes_specs(all_markets: Sequence[Market], chosen_ids: set[str]) -> list[dict]:
    """Serializable descriptors for listed outcomes outside S that receive free YES tokens on convert."""
    return [
        {
            "yes_token": m.yes_token,
            "market_id": m.id,
            "min_size": m.min_size,
            "tick_size": m.tick_size,
            "fee_rate": m.fee.rate,
            "fee_exponent": m.fee.exponent,
        }
        for m in all_markets
        if m.id not in chosen_ids and m.yes_token
    ]


def haircut_surplus_proceeds(
    surplus_markets: Sequence[Market],
    shares: float,
    books: Books,
    haircut: float,
    min_depth_ratio: float = 2.0,
) -> float:
    """Conservative liquidatable pUSD floor of free YES tokens minted on ``m not in S``."""
    if haircut <= 0.0 or shares <= 0.0:
        return 0.0
    total = 0.0
    for m in surplus_markets:
        if shares < m.min_size - 1e-6:
            continue
        book = books.get(m.yes_token)
        if book is None or not book.bids:
            continue
        avail = sum(lvl.size for lvl in book.bids[:2])
        if avail < shares * min_depth_ratio - 1e-6:
            continue
        fill = take(book.bids, shares, m.fee)
        if fill.shares < shares - 1e-6:
            continue
        eff_haircut = (
            min(0.95, haircut + 0.08)
            if (avail >= shares * max(3.0, min_depth_ratio * 1.5) and book.bids[0].price >= 0.15)
            else haircut
        )
        cushioned = max(0.0, fill.notional - fill.fee - shares * m.tick_size) * eff_haircut
        if cushioned >= 0.05:
            total += cushioned
    return total


def _unit_surplus_ceiling(surplus_markets: Sequence[Market], books: Books, haircut: float) -> float:
    if haircut <= 0.0:
        return 0.0
    unit = 0.0
    for m in surplus_markets:
        book = books.get(m.yes_token)
        if book is None or not book.bids:
            continue
        top_bid = book.bids[0].price
        eff_h = min(0.95, haircut + 0.08) if top_bid >= 0.15 else haircut
        cushioned = max(0.0, top_bid - fee_per_share(m.fee, top_bid) - m.tick_size) * eff_h
        if cushioned > 0.0:
            unit += cushioned
    return unit


class NegRiskConvert:
    name = "negrisk_convert"

    def __init__(self, cfg: ConvertConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out = []
        for ev in events:
            markets = _convertible(ev)
            if len(markets) >= 2:
                out.append(Target(f"convert:{ev.id}", self.name, ev, tuple(m.yes_token for m in markets), tuple(markets)))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        """Best top-of-book profit per converted unit, sum(bid_i - fee_i) - 1 over the best subset."""
        gains = []
        for m in target.payload:
            book = books.get(m.yes_token)
            if book is None or not book.bids:
                continue
            p = 1.0 - book.bids[0].price
            gains.append(1.0 - p - fee_per_share(m.fee, p))
        if len(gains) < 2:
            return None
        gains.sort(reverse=True)
        positive = [g for g in gains[: self.cfg.max_legs] if g > 0]
        return (sum(positive) if len(positive) >= 2 else gains[0] + gains[1]) - 1.0

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        all_markets: tuple[Market, ...] = target.payload
        entries = []
        for m in all_markets:
            book = books.get(m.yes_token)
            if book is None:
                continue
            levels = book.no_asks()
            if not levels:
                continue
            p = levels[0].price
            gain = 1.0 - p - fee_per_share(m.fee, p)
            if gain > 0:
                entries.append((gain, m, levels))
        if len(entries) < 2:
            return []
        entries.sort(key=lambda e: -e[0])
        top_entries = entries[: cfg.max_legs]

        # best = (chosen_markets, BasketFill, verified_payout_floor)
        best: tuple[list[Market], BasketFill, float] | None = None

        def _consider(chosen_m: list[Market], bfill: BasketFill, vpayout: float) -> None:
            nonlocal best
            vprofit = vpayout - bfill.cost
            if vprofit < bfill.shares * cfg.min_unit_edge - 1e-9:
                return
            if best is None or vprofit > (best[2] - best[1].cost):
                best = (chosen_m, bfill, vpayout)

        # Pass 1: Continuous marginal sweep over top-of-book prefixes
        prefix = 0.0
        for k, (gain, _, _) in enumerate(top_entries, start=1):
            prefix += gain
            if k < 2:
                continue
            chosen_k = [m for _, m, _ in top_entries[:k]]
            chosen_ids = {m.id for m in chosen_k}
            surplus_mkts = [m for m in all_markets if m.id not in chosen_ids]
            legs = [LegBook(lv, m.fee, m.min_size) for _, m, lv in top_entries[:k]]

            if prefix - 1.0 >= cfg.min_unit_edge:
                fill = size_basket(legs, float(k - 1), min_unit_edge=cfg.min_unit_edge, max_cost=cap)
                if fill is not None:
                    extra = haircut_surplus_proceeds(
                        surplus_mkts, fill.shares, books, cfg.surplus_haircut, cfg.surplus_min_depth_ratio
                    )
                    _consider(chosen_k, fill, fill.payout + extra)

            if cfg.surplus_haircut > 0.0 and surplus_mkts and k <= min(6, len(top_entries)):
                unit_surplus = _unit_surplus_ceiling(surplus_mkts, books, cfg.surplus_haircut)
                if unit_surplus > 0.0 and (prefix + unit_surplus - 1.0) >= cfg.min_unit_edge:
                    c_fill = size_basket(legs, float(k - 1) + unit_surplus, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
                    if c_fill is not None:
                        extra = haircut_surplus_proceeds(
                            surplus_mkts, c_fill.shares, books, cfg.surplus_haircut, cfg.surplus_min_depth_ratio
                        )
                        _consider(chosen_k, c_fill, float(k - 1) * c_fill.shares + extra)

        # Pass 2: Depth-tier subset optimizer (bypasses thin 5-share retail orders that would cap Pass 1)
        raw_tiers: set[float] = set()
        for _, m, lv in entries:
            s1 = floor_shares(lv[0].size)
            if s1 >= m.min_size:
                raw_tiers.add(s1)
            if len(lv) > 1:
                s2 = floor_shares(lv[0].size + lv[1].size)
                if s2 >= m.min_size:
                    raw_tiers.add(s2)
        if math.isfinite(cap):
            for mult in (1.02, 1.95, 2.95):
                sq = floor_shares(cap / mult)
                if sq >= 5.0:
                    raw_tiers.add(sq)

        for q_tier in sorted(raw_tiers, reverse=True)[:10]:
            eligible: list[tuple[float, Market, object]] = []
            for _, m, lv in entries:
                if q_tier < m.min_size - EPS:
                    continue
                f = take(lv, q_tier, m.fee)
                if f.shares < q_tier - 1e-6 or f.notional < MIN_ORDER_NOTIONAL - EPS:
                    continue
                if f.worst_price > 0 and f.notional / f.worst_price < m.min_size - 1e-6:
                    continue
                unit_gain = 1.0 - (f.notional + f.fee) / q_tier
                if unit_gain > 0:
                    eligible.append((unit_gain, m, f))
            if len(eligible) < 2:
                continue
            eligible.sort(key=lambda item: -item[0])
            eligible = eligible[: cfg.max_legs]
            cum_cost = 0.0
            for k in range(1, len(eligible) + 1):
                f_k = eligible[k - 1][2]
                cum_cost += f_k.notional + f_k.fee
                if k < 2:
                    continue
                if cum_cost <= cap + EPS:
                    chosen_m = [m for _, m, _ in eligible[:k]]
                    chosen_ids = {m.id for m in chosen_m}
                    surplus_mkts = [m for m in all_markets if m.id not in chosen_ids]
                    bfill = BasketFill(q_tier, tuple(f for _, _, f in eligible[:k]), float(k - 1) * q_tier)
                    extra = haircut_surplus_proceeds(
                        surplus_mkts, q_tier, books, cfg.surplus_haircut, cfg.surplus_min_depth_ratio
                    )
                    _consider(chosen_m, bfill, bfill.payout + extra)
                elif math.isfinite(cap):
                    # Also try sizing this exact liquid subset down cleanly to cap!
                    sub_m = [m for _, m, _ in eligible[:k]]
                    sub_ids = {m.id for m in sub_m}
                    sub_lv = [LegBook(books.get(m.yes_token).no_asks(), m.fee, m.min_size) for m in sub_m]  # type: ignore[union-attr]
                    surplus_mkts = [m for m in all_markets if m.id not in sub_ids]
                    s_fill = size_basket(sub_lv, float(k - 1), min_unit_edge=cfg.min_unit_edge, max_cost=cap)
                    if s_fill is not None:
                        extra = haircut_surplus_proceeds(
                            surplus_mkts, s_fill.shares, books, cfg.surplus_haircut, cfg.surplus_min_depth_ratio
                        )
                        _consider(sub_m, s_fill, s_fill.payout + extra)
                    break

        if best is None:
            return []
        chosen, fill, verified_payout = best
        k = len(chosen)
        chosen_ids = {m.id for m in chosen}
        ev = target.event
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=ev.id,
            title=ev.title,
            legs=[buy_leg(m, Outcome.NO, f) for m, f in zip(chosen, fill.fills)],
            shares=fill.shares,
            cost=fill.cost + cfg.gas_usd,
            payout_floor=verified_payout,
            settlement=Settlement.CONVERT,
            risk="low",
            resolves_at=None,
            detected_at=now,
            meta={
                "neg_risk_market_id": ev.neg_risk_market_id,
                "index_set": sum(1 << m.neg_risk_index for m in chosen),
                "legs_used": k,
                "legs_listed": len(all_markets),
                "augmented": ev.neg_risk_augmented,
                "surplus_credit_usd": round(max(0.0, verified_payout - float(k - 1) * fill.shares), 4),
                "surplus_yes": surplus_yes_specs(all_markets, chosen_ids),
            },
        )
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge:
            return []
        return [opp]


class NegRiskYesBasket:
    name = "negrisk_yes_basket"

    def __init__(self, cfg: YesBasketConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out = []
        for ev in events:
            if not ev.neg_risk or ev.neg_risk_augmented or ev.version != "v1":
                continue
            if any(not m.accepting_orders and not m.closed for m in ev.markets):
                continue
            if any(m.closed and m.resolved_outcome != "NO" for m in ev.markets):
                continue
            markets = ev.tradeable_markets
            if len(markets) < 2 or len(markets) > self.cfg.max_legs or not all(m.is_yes_no for m in markets):
                continue
            reason = exhaustive_reason(ev)
            if self.cfg.require_catch_all and reason is None:
                continue
            if not all(m.end_date for m in markets):
                continue
            out.append(Target(f"yes:{ev.id}", self.name, ev, tuple(m.yes_token for m in markets), (tuple(markets), reason)))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        markets, _ = target.payload
        total = 0.0
        for m in markets:
            book = books.get(m.yes_token)
            if book is None or not book.asks:
                return None
            total += unit_cost(book.asks, m.fee) or 0.0
        return 1.0 - total

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        markets, reason = target.payload
        legs = []
        for m in markets:
            book = books.get(m.yes_token)
            if book is None or not book.asks:
                return []
            legs.append(LegBook(book.asks, m.fee, m.min_size))
        if sum(unit_cost(leg.levels, leg.fee) for leg in legs) > 1.0 - cfg.min_unit_edge:
            return []
        if sum(leg.levels[0].price for leg in legs) < cfg.min_ask_sum:
            return []
        resolves_at = max(m.end_date for m in markets) + timedelta(days=cfg.resolution_lag_days)
        now_dt = utc(now)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
            return []
        fill = size_basket(legs, 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
        if fill is None:
            return []
        ev = target.event
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=ev.id,
            title=ev.title,
            legs=[buy_leg(m, Outcome.YES, f) for m, f in zip(markets, fill.fills)],
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="medium",
            resolves_at=resolves_at,
            detected_at=now,
            meta={
                "exhaustive": reason,
                "legs": len(markets),
                "condition_ids": [m.condition_id for m in markets if m.condition_id],
            },
        )
        apr = opp.apr(now_dt)
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge or (apr is not None and apr < cfg.min_apr):
            return []
        return [opp]
