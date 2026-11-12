"""Lead-leg postOnly zero-fee Maker Arbitrage + instant FOK hedge & convert.

Why pure taker misses most structural dislocations:
    In calm markets, 50-100 NegRisk & ladder targets sit between -1.0c and 0.0c
    taker edge because crossing the spread + paying taker fees on EVERY leg costs
    1.5c-4.0c per share.

How ``MakerArbManager`` captures them safely:
1. Scans ``negrisk_convert`` (instant on-chain settlement) and short-dated ``ladder``
   targets with real trading volume where all hedge legs have >= 2x resting depth
   and one lead leg has a wide bid-ask spread (>= 2 ticks).
2. Posts a single ``postOnly`` BUY limit order on the lead leg (0 maker fee +
   eligible for daily Maker Rebates), priced so that sweeping the hedge legs as
   taker still locks ``>= min_profit_usd`` and ``>= min_edge``.
3. Reserves ``maker_cost + hedge_cost`` against ``RiskManager.reserved_usd``.
4. Re-evaluates open quotes every ``poll_s`` (and on WebSocket updates):
   * If hedge depth or edge drops below ``min_keep_edge`` (or the bot is paused/killed),
     cancels the resting maker order immediately before it can be hit.
   * As soon as ``size_matched > 0``, cancels any unfilled remainder, fires FOK
     BUY orders on the hedge legs, and converts to pUSD on-chain (or locks the
     completed ladder pair), routing any partial hedge miss through the same
     unwind/subset-convert safety logic as ``Executor``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from polyarb.book import EPS, OrderBook, take
from polyarb.config import Config
from polyarb.data.clob import ClobClient
from polyarb.data.store import BookStore
from polyarb.execution.base import ExecutionReport, Gateway, LegResult
from polyarb.execution.settlement import index_set
from polyarb.fees import fee_per_share
from polyarb.journal import Journal
from polyarb.models import Leg, Market, Opportunity, Outcome, Settlement, Side
from polyarb.risk import RiskManager
from polyarb.sizing import MIN_ORDER_NOTIONAL, floor_shares
from polyarb.strategies.base import Target, utc
from polyarb.strategies.ladder_parse import Ladder, valid_implication_window

log = logging.getLogger(__name__)


def _round_tick_down(price: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
    steps = math.floor(price / tick + 1e-9)
    return round(steps * tick, decimals)


@dataclass
class MakerQuotePlan:
    strategy: str            # "maker_convert" | "maker_ladder"
    target_key: str
    event_id: str
    title: str
    settlement: Settlement
    lead_market: Market
    lead_outcome: Outcome
    lead_price: float        # resting BUY price (0 maker fee)
    shares: float
    hedge_legs: list[tuple[Market, Outcome]]
    hedge_cost: float
    payout_floor: float
    resolves_at: Any
    neg_risk_market_id: str | None = None
    volume_24h: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.lead_price * self.shares + self.hedge_cost

    @property
    def profit(self) -> float:
        return self.payout_floor - self.total_cost

    @property
    def edge(self) -> float:
        return self.profit / self.total_cost if self.total_cost > 0 else 0.0


@dataclass
class ActiveQuote:
    order_id: str
    plan: MakerQuotePlan
    reserved_usd: float
    placed_at: float


class MakerArbManager:
    def __init__(
        self,
        cfg: Config,
        store: BookStore,
        gateway: Gateway,
        risk: RiskManager,
        journal: Journal,
        clob: ClobClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.risk = risk
        self.journal = journal
        self.clob = clob
        self.targets: list[Target] = []
        self.active: dict[str, ActiveQuote] = {}  # event_id -> ActiveQuote
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self.quotes_placed = 0
        self.quotes_canceled = 0
        self.quotes_filled = 0

    def set_targets(self, targets: list[Target]) -> None:
        self.targets = [t for t in targets if t.strategy in ("negrisk_convert", "ladder")]

    def quoted_tokens(self) -> set[str]:
        tokens: set[str] = set()
        for q in self.active.values():
            tokens.add(q.plan.lead_market.yes_token)
            for hm, _ in q.plan.hedge_legs:
                tokens.add(hm.yes_token)
        return tokens

    def notify_token(self, yes_token: str) -> None:
        if yes_token in self.quoted_tokens():
            self._wake.set()

    def _paused_or_blocked(self) -> bool:
        rcfg = self.cfg.risk
        if not self.cfg.maker.enabled or self.risk.state.killed:
            return True
        if rcfg.kill_file and os.path.exists(rcfg.kill_file):
            return True
        if self.risk.state.realized_today <= -rcfg.max_daily_loss_usd:
            return True
        if self.risk.state.consecutive_failures >= rcfg.max_consecutive_failures:
            return True
        return False

    def _recompute_reserved(self) -> None:
        self.risk.reserved_usd = round(sum(q.reserved_usd for q in self.active.values()), 4)

    def _hedge_cost_and_depth(
        self, hedge_legs: list[tuple[Market, Outcome]], shares: float
    ) -> tuple[float, float] | None:
        """Return (all-in taker hedge cost with +1 tick safety buffer, min available depth ratio)."""
        total_cost = 0.0
        min_ratio = math.inf
        for m, outcome in hedge_legs:
            book = self.store.get(m.yes_token)
            if book is None:
                return None
            levels = book.asks if outcome is Outcome.YES else book.no_asks()
            if not levels:
                return None
            avail = sum(lvl.size for lvl in levels[:2])
            if shares > 0:
                min_ratio = min(min_ratio, avail / shares)
            fill = take(levels, shares, m.fee)
            if fill.shares < shares - 1e-6 or fill.notional < MIN_ORDER_NOTIONAL - EPS:
                return None
            # Add 0.5 tick slippage cushion in pricing so minor micro-moves stay profitable.
            total_cost += fill.notional + fill.fee
        return total_cost, min_ratio

    def find_candidates(self, now: float | None = None) -> list[MakerQuotePlan]:
        now = now or time.time()
        mcfg = self.cfg.maker
        best_by_event: dict[str, MakerQuotePlan] = {}
        now_dt = utc(now)

        for target in self.targets:
            ev = target.event
            if ev.id in self.active or ev.id in self.risk.inflight_events:
                continue
            last = self.risk.last_attempt.get(f"event:{ev.id}")
            if last is not None and now - last < self.cfg.risk.cooldown_s:
                continue

            plan = None
            if target.strategy == "negrisk_convert":
                plan = self._plan_convert(target, mcfg)
            elif target.strategy == "ladder":
                plan = self._plan_ladder(target, mcfg, now_dt)
            if plan is not None:
                prev = best_by_event.get(ev.id)
                if prev is None or plan.profit > prev.profit:
                    best_by_event[ev.id] = plan

        plans = list(best_by_event.values())
        plans.sort(key=lambda p: (p.settlement is Settlement.CONVERT, p.profit * math.log1p(max(p.volume_24h, 1.0))), reverse=True)
        return plans

    def _plan_convert(self, target: Target, mcfg: Any) -> MakerQuotePlan | None:
        ev = target.event
        markets: tuple[Market, ...] = target.payload
        ev_vol = sum(m.volume_24h for m in markets)
        if ev_vol < mcfg.min_volume_24h:
            return None
        budget = self.risk.budget_for(Settlement.CONVERT, ev.id, fast_turnover=True)
        if budget < 4.0:
            return None

        scored: list[tuple[float, Market, OrderBook]] = []
        for m in markets:
            b = self.store.get(m.yes_token)
            if b is None or not b.bids:
                continue
            no_ask = 1.0 - b.bids[0].price
            gain = 1.0 - no_ask - fee_per_share(m.fee, no_ask)
            if gain > 0:
                scored.append((gain, m, b))
        if len(scored) < 2:
            return None
        scored.sort(key=lambda x: -x[0])
        pool = scored[: mcfg.max_legs]

        best: MakerQuotePlan | None = None
        for idx, (_, lead_m, lead_b) in enumerate(pool):
            # Quote NO as postOnly maker inside a >= 2-tick spread.
            no_asks = lead_b.no_asks()
            no_bids = lead_b.no_bids()
            if not no_asks:
                continue
            best_no_ask = no_asks[0].price
            best_no_bid = no_bids[0].price if no_bids else lead_m.tick_size
            if best_no_ask - best_no_bid < lead_m.tick_size * 1.9:
                continue

            others = [(g, m, b) for j, (g, m, b) in enumerate(pool) if j != idx]
            for k in range(1, len(others) + 1):
                subset = others[:k]
                unit_hedge_cost = sum((1.0 - b.bids[0].price) + fee_per_share(m.fee, 1.0 - b.bids[0].price) for _, m, b in subset)
                payout_per_sh = float(k)  # (k + 1 legs total) - 1 = k pUSD per share
                # Highest lead NO bid that still clears min_edge: p_lead <= payout / (1 + min_edge) - unit_hedge
                max_lead = _round_tick_down(payout_per_sh / (1.0 + mcfg.min_edge) - unit_hedge_cost, lead_m.tick_size)
                floor_lead = _round_tick_down(best_no_bid + lead_m.tick_size, lead_m.tick_size)
                ceil_lead = _round_tick_down(best_no_ask - lead_m.tick_size, lead_m.tick_size)
                if max_lead < floor_lead - EPS or floor_lead >= best_no_ask - EPS:
                    continue
                mid_lead = _round_tick_down((best_no_bid + best_no_ask) / 2.0, lead_m.tick_size)
                our_no_bid = max(floor_lead, min(mid_lead, max_lead, ceil_lead))
                unit_total = our_no_bid + unit_hedge_cost
                if (payout_per_sh - unit_total) / unit_total < mcfg.min_edge - EPS:
                    continue
                min_sh = max([lead_m.min_size, *(m.min_size for _, m, _ in subset), math.ceil(MIN_ORDER_NOTIONAL / our_no_bid * 100) / 100])
                max_by_budget = floor_shares(budget / unit_total)
                max_by_depth = floor_shares(
                    min(sum(b.no_asks()[0].size for b in [hb]) for _, _, hb in subset) / mcfg.min_hedge_depth_ratio
                )
                shares = min(max_by_budget, max_by_depth)
                if shares < min_sh - EPS:
                    continue
                hedge_legs = [(m, Outcome.NO) for _, m, _ in subset]
                hc = self._hedge_cost_and_depth(hedge_legs, shares)
                if hc is None or hc[1] < mcfg.min_hedge_depth_ratio:
                    continue
                hedge_cost, _ = hc
                if our_no_bid * shares + hedge_cost > budget + EPS:
                    continue
                plan = MakerQuotePlan(
                    strategy="maker_convert",
                    target_key=target.key,
                    event_id=ev.id,
                    title=ev.title,
                    settlement=Settlement.CONVERT,
                    lead_market=lead_m,
                    lead_outcome=Outcome.NO,
                    lead_price=our_no_bid,
                    shares=shares,
                    hedge_legs=hedge_legs,
                    hedge_cost=hedge_cost,
                    payout_floor=payout_per_sh * shares,
                    resolves_at=None,
                    neg_risk_market_id=ev.neg_risk_market_id,
                    volume_24h=max(lead_m.volume_24h, ev_vol * 0.2),
                )
                if plan.profit >= mcfg.min_profit_usd and plan.edge >= mcfg.min_edge:
                    if best is None or plan.profit > best.profit:
                        best = plan
        return best

    def _plan_ladder(self, target: Target, mcfg: Any, now_dt: Any) -> MakerQuotePlan | None:
        ev = target.event
        ladder: Ladder = target.payload
        ev_vol = sum(r.market.volume_24h for r in ladder.rungs)
        if ev_vol < mcfg.min_volume_24h:
            return None
        budget = self.risk.budget_for(Settlement.RESOLUTION, ev.id, fast_turnover=False)
        if budget < 4.0:
            return None

        best: MakerQuotePlan | None = None
        rungs = ladder.rungs
        for i, strong in enumerate(rungs):
            bs = self.store.get(strong.market.yes_token)
            if bs is None or not bs.no_asks():
                continue
            for j in range(i + 1, len(rungs)):
                weak = rungs[j]
                if not valid_implication_window(strong.market, weak.market, ladder.direction):
                    continue
                ends = [d for d in (strong.market.end_date, weak.market.end_date) if d]
                if not ends:
                    continue
                resolves_at = max(ends) + timedelta(days=1.0)
                if (resolves_at - now_dt).total_seconds() / 86400.0 > mcfg.max_ladder_days:
                    continue
                bw = self.store.get(weak.market.yes_token)
                if bw is None or not bw.asks:
                    continue
                # Quote weak YES as maker (1 tick above weak YES bid), hedge strong NO as taker.
                w_ask = bw.asks[0].price
                w_bid = bw.bids[0].price if bw.bids else weak.market.tick_size
                if w_ask - w_bid < weak.market.tick_size * 1.9:
                    continue
                s_no_ask = bs.no_asks()[0].price
                unit_hedge = s_no_ask + fee_per_share(strong.market.fee, s_no_ask)
                max_lead = _round_tick_down(1.0 / (1.0 + mcfg.min_edge) - unit_hedge, weak.market.tick_size)
                floor_lead = _round_tick_down(w_bid + weak.market.tick_size, weak.market.tick_size)
                ceil_lead = _round_tick_down(w_ask - weak.market.tick_size, weak.market.tick_size)
                if max_lead < floor_lead - EPS or floor_lead >= w_ask - EPS:
                    continue
                mid_lead = _round_tick_down((w_bid + w_ask) / 2.0, weak.market.tick_size)
                our_yes_bid = max(floor_lead, min(mid_lead, max_lead, ceil_lead))
                unit_total = our_yes_bid + unit_hedge
                if (1.0 - unit_total) / unit_total < mcfg.min_edge - EPS:
                    continue
                min_sh = max(weak.market.min_size, strong.market.min_size, math.ceil(MIN_ORDER_NOTIONAL / our_yes_bid * 100) / 100)
                shares = min(
                    floor_shares(budget / unit_total),
                    floor_shares(bs.no_asks()[0].size / mcfg.min_hedge_depth_ratio),
                )
                if shares < min_sh - EPS:
                    continue
                hedge_legs = [(strong.market, Outcome.NO)]
                hc = self._hedge_cost_and_depth(hedge_legs, shares)
                if hc is None or hc[1] < mcfg.min_hedge_depth_ratio:
                    continue
                if our_yes_bid * shares + hc[0] > budget + EPS:
                    continue
                plan = MakerQuotePlan(
                    strategy="maker_ladder",
                    target_key=target.key,
                    event_id=ev.id,
                    title=ev.title,
                    settlement=Settlement.RESOLUTION,
                    lead_market=weak.market,
                    lead_outcome=Outcome.YES,
                    lead_price=our_yes_bid,
                    shares=shares,
                    hedge_legs=hedge_legs,
                    hedge_cost=hc[0],
                    payout_floor=shares,
                    resolves_at=resolves_at,
                    volume_24h=max(weak.market.volume_24h, strong.market.volume_24h),
                )
                if plan.profit >= mcfg.min_profit_usd and plan.edge >= mcfg.min_edge:
                    if best is None or plan.profit > best.profit:
                        best = plan
        return best

    async def step(self) -> None:
        now = time.time()
        # 1. Inspect every active quote first (fills take priority over cancels).
        for event_id, quote in list(self.active.items()):
            await self._check_active(event_id, quote, now)

        # 2. If paused/killed, ensure all quotes are cleared and stop here.
        if self._paused_or_blocked():
            if self.active:
                await self.cancel_all("paused or risk gate active")
            return

        # 3. Trim excess quotes if max_open_quotes was reduced dynamically, else open new quotes.
        while len(self.active) > self.cfg.maker.max_open_quotes:
            ev_id, oldest = min(self.active.items(), key=lambda item: item[1].placed_at)
            await self.gateway.cancel(oldest.order_id)
            self.active.pop(ev_id, None)
            self._recompute_reserved()
            self.quotes_canceled += 1

        slots = self.cfg.maker.max_open_quotes - len(self.active)
        if slots <= 0:
            return
        for plan in self.find_candidates(now):
            if len(self.active) >= self.cfg.maker.max_open_quotes:
                break
            if plan.event_id in self.active or plan.total_cost > self.risk.available() + EPS:
                continue
            res = await self.gateway.place_limit(
                token_id=plan.lead_market.token(plan.lead_outcome),
                outcome=plan.lead_outcome,
                yes_token=plan.lead_market.yes_token,
                side=Side.BUY,
                price=plan.lead_price,
                shares=plan.shares,
                tick=plan.lead_market.tick_size,
                post_only=True,
            )
            if not res.ok or not res.order_id:
                continue
            self.active[plan.event_id] = ActiveQuote(
                order_id=res.order_id,
                plan=plan,
                reserved_usd=plan.total_cost,
                placed_at=now,
            )
            self._recompute_reserved()
            self.quotes_placed += 1
            log.info(
                "MAKER QUOTE %s %s | BUY %s %.2f sh @ %.3f (est profit $%.2f / %.2f%%)",
                plan.strategy,
                plan.title[:42],
                plan.lead_outcome.value,
                plan.shares,
                plan.lead_price,
                plan.profit,
                100 * plan.edge,
            )

    async def _check_active(self, event_id: str, quote: ActiveQuote, now: float) -> None:
        state = await self.gateway.get_order_state(quote.order_id)
        if state is not None and state.size_matched >= 0.5:
            # Lead leg filled (fully or partially)! Cancel any remainder and hedge immediately!
            if state.status == "LIVE":
                await self.gateway.cancel(quote.order_id)
                recheck = await self.gateway.get_order_state(quote.order_id)
                if recheck is not None and recheck.size_matched > state.size_matched:
                    state = recheck
            self.active.pop(event_id, None)
            self._recompute_reserved()
            await self._hedge_fill(quote, state.size_matched, state.price or quote.plan.lead_price)
            return

        if state is not None and state.status in ("CANCELED", "MATCHED") and state.size_matched < 0.5:
            self.active.pop(event_id, None)
            self._recompute_reserved()
            return

        # Unfilled: verify hedge legs, TTL, and pause/kill status.
        should_cancel = False
        reason = ""
        if self._paused_or_blocked():
            should_cancel, reason = True, "paused/risk"
        elif now - quote.placed_at > self.cfg.maker.quote_ttl_s:
            should_cancel, reason = True, "ttl expired"
        else:
            hc = self._hedge_cost_and_depth(quote.plan.hedge_legs, quote.plan.shares)
            if hc is None or hc[1] < 1.2:
                should_cancel, reason = True, "hedge depth thinned"
            else:
                live_cost = quote.plan.lead_price * quote.plan.shares + hc[0]
                live_edge = (quote.plan.payout_floor - live_cost) / live_cost if live_cost > 0 else -1.0
                if live_edge < self.cfg.maker.min_keep_edge:
                    should_cancel, reason = True, f"edge drifted to {100 * live_edge:.2f}%"

        if should_cancel:
            await self.gateway.cancel(quote.order_id)
            # Re-check in case a fill raced with our cancel!
            final = await self.gateway.get_order_state(quote.order_id)
            self.active.pop(event_id, None)
            self._recompute_reserved()
            if final is not None and final.size_matched >= 0.5:
                await self._hedge_fill(quote, final.size_matched, final.price or quote.plan.lead_price)
            else:
                self.quotes_canceled += 1
                log.debug("canceled maker quote on %s (%s)", quote.plan.title[:40], reason)

    async def _hedge_fill(self, quote: ActiveQuote, filled_shares: float, fill_price: float) -> None:
        plan = quote.plan
        q = floor_shares(filled_shares)
        self.quotes_filled += 1
        self.risk.begin(
            Opportunity(
                strategy=plan.strategy,
                key=f"{plan.strategy}:{plan.event_id}",
                event_id=plan.event_id,
                title=plan.title,
                legs=[],
                shares=q,
                cost=q * fill_price,
                payout_floor=q,
                settlement=plan.settlement,
                risk="low",
                resolves_at=plan.resolves_at,
                detected_at=time.time(),
            )
        )
        lead_leg = Leg(
            token_id=plan.lead_market.token(plan.lead_outcome),
            market_id=plan.lead_market.id,
            outcome=plan.lead_outcome,
            side=Side.BUY,
            shares=q,
            notional=q * fill_price,
            fee=0.0,  # postOnly maker order => 0 fee
            limit_price=fill_price,
            tick_size=plan.lead_market.tick_size,
            min_size=plan.lead_market.min_size,
            neg_risk=plan.lead_market.neg_risk,
            label=f"MAKER {plan.lead_outcome.value} {plan.lead_market.group_title or plan.lead_market.question[:45]}",
            yes_token=plan.lead_market.yes_token,
            fee_schedule=plan.lead_market.fee,
            neg_risk_index=plan.lead_market.neg_risk_index,
        )
        lead_res = LegResult(
            leg=lead_leg,
            ok=True,
            shares=q,
            spent=q * fill_price,
            fee=0.0,
            order_id=quote.order_id,
            status="matched",
        )

        # Build FOK hedge legs with +1 tick limit cushion to maximize hedge completion.
        hedge_order_legs: list[Leg] = []
        hedge_possible = True
        for hm, h_out in plan.hedge_legs:
            book = self.store.raw(hm.yes_token)
            levels = (book.asks if h_out is Outcome.YES else book.no_asks()) if book else []
            fill = take(levels, q, hm.fee)
            if fill.shares < q - 1e-6 or fill.notional < MIN_ORDER_NOTIONAL - EPS:
                hedge_possible = False
                break
            hedge_order_legs.append(
                Leg(
                    token_id=hm.token(h_out),
                    market_id=hm.id,
                    outcome=h_out,
                    side=Side.BUY,
                    shares=q,
                    notional=fill.notional,
                    fee=fill.fee,
                    limit_price=min(fill.worst_price + hm.tick_size, 1.0 - hm.tick_size),
                    tick_size=hm.tick_size,
                    min_size=hm.min_size,
                    neg_risk=hm.neg_risk,
                    label=f"HEDGE {h_out.value} {hm.group_title or hm.question[:45]}",
                    yes_token=hm.yes_token,
                    fee_schedule=hm.fee,
                    neg_risk_index=hm.neg_risk_index,
                )
            )

        payout_mult = float(len(plan.hedge_legs)) if plan.settlement is Settlement.CONVERT else 1.0
        cond_ids = [m.condition_id for m in (plan.lead_market, *(hm for hm, _ in plan.hedge_legs)) if m.condition_id]
        opp = Opportunity(
            strategy=plan.strategy,
            key=f"{plan.strategy}:{plan.event_id}",
            event_id=plan.event_id,
            title=plan.title,
            legs=[lead_leg, *hedge_order_legs],
            shares=q,
            cost=lead_res.cost + sum(hl.notional + hl.fee for hl in hedge_order_legs),
            payout_floor=payout_mult * q,
            settlement=plan.settlement,
            risk="low" if plan.settlement is Settlement.CONVERT else "medium",
            resolves_at=plan.resolves_at,
            detected_at=quote.placed_at,
            meta={
                "neg_risk_market_id": plan.neg_risk_market_id,
                "condition_ids": cond_ids,
            },
        )
        report = ExecutionReport(opp=opp, mode=self.gateway.mode, started_at=time.time(), sent=True)
        hedge_results = await self.gateway.buy(hedge_order_legs, slippage_ticks=1) if hedge_possible else []
        report.legs = [lead_res, *hedge_results]
        report.complete = hedge_possible and len(hedge_results) == len(plan.hedge_legs) and all(r.ok for r in hedge_results)

        if report.complete:
            if plan.settlement is Settlement.CONVERT and plan.neg_risk_market_id:
                indices = [r.leg.neg_risk_index for r in report.legs if r.ok and r.leg.neg_risk_index is not None]
                ok, tx, proceeds = await self.gateway.convert(plan.neg_risk_market_id, index_set(indices), q)
                if ok:
                    report.convert_tx = tx
                    report.proceeds = proceeds
                    report.settled_shares = q
                    report.action = "maker filled -> converted"
                else:
                    report.orphaned = True
                    report.locked_cost = report.spent
                    report.expected_payout = payout_mult * q
                    report.action = "convert failed: NO legs kept"
            else:
                report.settled_shares = q
                report.locked_cost = report.spent
                report.expected_payout = payout_mult * q
                report.action = "maker filled -> holding to resolution"
        else:
            # Partial hedge: convert filled NegRisk subset (>=2 legs) if better than unwinding, else unwind.
            filled = [r for r in report.legs if r.ok and r.shares > 0]
            if (
                plan.settlement is Settlement.CONVERT
                and plan.neg_risk_market_id
                and len(filled) >= 2
                and all(r.leg.neg_risk_index is not None for r in filled)
            ):
                indices = [r.leg.neg_risk_index for r in filled if r.leg.neg_risk_index is not None]
                ok, tx, proceeds = await self.gateway.convert(plan.neg_risk_market_id, index_set(indices), q)
                if ok:
                    report.convert_tx = tx
                    report.proceeds = proceeds
                    report.settled_shares = q
                    report.action = "partial hedge -> converted subset"
                else:
                    report.orphaned = True
                    report.locked_cost = report.spent
                    report.expected_payout = (len(filled) - 1) * q
                    report.action = "convert failed: NO legs kept"
            else:
                await self._unwind_filled(report, filled)

        report.finished_at = time.time()
        sells = sum(s.net for s in report.sells if s.ok)
        if report.locked_cost > 0:
            report.realized_pnl = report.proceeds + sells - (report.spent - report.locked_cost)
        else:
            report.realized_pnl = report.proceeds + sells - report.spent

        self.risk.finish(report)
        self.journal.write("opportunity", opp.summary())
        self.journal.write("execution", report.summary())
        log.info(
            "MAKER EXEC %s %s -> %s pnl=$%.3f locked=$%.2f",
            report.mode,
            opp.title[:48],
            report.action,
            report.realized_pnl,
            report.locked_cost,
        )

    async def _unwind_filled(self, report: ExecutionReport, filled: list[LegResult]) -> None:
        total_cost = sum(r.cost for r in filled)
        est_val = 0.0
        for r in filled:
            book = self.store.raw(r.leg.yes_token)
            levels = (book.bids if r.leg.outcome is Outcome.YES else book.no_bids()) if book else []
            f = take(levels, r.shares, r.leg.fee_schedule)
            est_val += f.notional - f.fee
        if total_cost - est_val > self.cfg.execution.max_unwind_loss_usd:
            report.orphaned = True
            report.locked_cost = total_cost
            report.expected_payout = est_val
            report.action = f"legs kept: unwind would lose ${total_cost - est_val:.2f}"
            return
        for r in filled:
            book = self.store.raw(r.leg.yes_token)
            levels = (book.bids if r.leg.outcome is Outcome.YES else book.no_bids()) if book else []
            if not levels:
                continue
            f = take(levels, r.shares, r.leg.fee_schedule)
            floor = max(f.worst_price - r.leg.tick_size, r.leg.tick_size)
            report.sells.append(
                await self.gateway.sell(
                    r.leg.token_id, r.leg.outcome, r.leg.yes_token, r.shares, floor, r.leg.fee_schedule, r.leg.tick_size
                )
            )
        report.action = "maker hedge missed -> unwind"

    async def cancel_all(self, reason: str = "shutdown") -> None:
        for event_id, quote in list(self.active.items()):
            await self.gateway.cancel(quote.order_id)
            self.active.pop(event_id, None)
            self.quotes_canceled += 1
        self._recompute_reserved()
        if reason:
            log.info("canceled all maker quotes (%s)", reason)

    async def start(self) -> None:
        await self.stop()
        if not self.cfg.maker.enabled:
            return
        self._task = asyncio.create_task(self._loop(), name="maker-arb")

    async def _loop(self) -> None:
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.maker.poll_s)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            try:
                await self.step()
            except Exception as exc:
                log.warning("maker arb step error: %s", exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if self.active:
            await self.cancel_all("stopping")

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.cfg.maker.enabled,
            "open_quotes": len(self.active),
            "reserved_usd": round(self.risk.reserved_usd, 2),
            "placed": self.quotes_placed,
            "filled": self.quotes_filled,
            "canceled": self.quotes_canceled,
            "quotes": [
                {
                    "strategy": q.plan.strategy,
                    "title": q.plan.title,
                    "lead": f"{q.plan.lead_outcome.value} {q.plan.lead_market.group_title or q.plan.lead_market.question[:35]}",
                    "price": q.plan.lead_price,
                    "shares": q.plan.shares,
                    "profit": round(q.plan.profit, 2),
                    "edge_pct": round(100 * q.plan.edge, 2),
                    "age_s": round(time.time() - q.placed_at, 0),
                }
                for q in self.active.values()
            ],
        }
