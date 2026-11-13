"""Lead-leg postOnly zero-fee Maker Arbitrage 3.0 + Deterministic Bonding + Instant FOK Hedge.

Capabilities:
1. Covers 6 structural & deterministic families with 100% payout-floor preservation:
   * ``maker_convert``    : NegRisk NO subset (including concentrated converts with
                            surplus-YES auto-liquidation) -> instant on-chain pUSD.
   * ``maker_metar``      : **0-Hedge-Leg** deterministic NOAA METAR lock bonding
                            (quotes even when ``asks == []``, stepping 1 tick ahead
                            of bonding walls with ZERO leg risk + 0.995 fast flip).
   * ``maker_binance``    : **0-Hedge-Leg** deterministic closed Binance Spot candle
                            bonding (hourly, noon, and hit barriers) with ZERO leg risk.
   * ``maker_ladder``     : Dual-orientation monotone implication ladder (``YES_weak``
                            maker + ``NO_strong`` hedge AND ``NO_strong`` maker + ``YES_weak`` hedge).
   * ``maker_yes_basket`` : Short-dated exhaustive YES baskets AND METAR-pruned
                            surviving weather baskets.
   * ``maker_twin``       : Cross-event same-oracle partition & implication locks.
2. **Polymarket Maker Rebate Pricing**: Credits ``rebate_credit_ratio`` of expected
   USDC Maker Rebates (``fee.rebate_rate * taker_fee``) toward gates & ranking while
   strictly guaranteeing Day-0 cash profit > 0 on every trade.
3. **Pooled Hedge Buffer + Multi-Lead Self-Hedging Co-Fills + 3s Outbid Re-Pegging**.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, Mapping, Sequence

from polyarb.book import EPS, OrderBook, take
from polyarb.config import Config
from polyarb.data.clob import ClobClient
from polyarb.data.store import BookStore
from polyarb.execution.base import ExecutionReport, Gateway, LegResult
from polyarb.execution.executor import liquidate_surplus_yes
from polyarb.execution.settlement import index_set
from polyarb.fees import fee_per_share, maker_rebate_per_share
from polyarb.journal import Journal
from polyarb.models import Leg, Market, Opportunity, Outcome, Settlement, Side
from polyarb.risk import RiskManager
from polyarb.sizing import MIN_ORDER_NOTIONAL, floor_shares
from polyarb.strategies.base import Target, utc
from polyarb.strategies.crypto_short import ShortSingleSpec
from polyarb.strategies.ladder_parse import Ladder, valid_implication_window
from polyarb.strategies.negrisk import haircut_surplus_proceeds, surplus_yes_specs
from polyarb.strategies.twin import TwinSlice

log = logging.getLogger(__name__)
ZERO_LEG_STRATS = frozenset({"maker_metar", "maker_binance", "maker_crypto_short"})


def _round_tick_down(price: float, tick: float) -> float:
    decimals = max(0, -int(math.floor(math.log10(tick) + 1e-9)))
    steps = math.floor(price / tick + 1e-9)
    return round(steps * tick, decimals)


def _best_maker_bid(
    best_bid: float,
    best_ask: float,
    tick: float,
    max_lead: float,
    allow_join_bid: bool,
    max_step_ticks: float = 3.0,
) -> float | None:
    """Optimal non-crossing postOnly BUY price inside or at top of spread."""
    ceil_lead = _round_tick_down(best_ask - tick, tick)
    if ceil_lead <= 0.0 or max_lead <= 0.0:
        return None
    spread = best_ask - best_bid
    if spread >= tick * 1.9:
        floor_lead = _round_tick_down(best_bid + tick, tick)
        if max_lead < floor_lead - EPS or floor_lead >= best_ask - EPS:
            return None
        mid_lead = _round_tick_down((best_bid + best_ask) / 2.0, tick)
        step_cap = _round_tick_down(best_bid + max_step_ticks * tick, tick)
        return max(floor_lead, min(mid_lead, step_cap, max_lead, ceil_lead))
    if allow_join_bid and spread >= tick * 0.95 and best_bid >= tick - EPS:
        join_px = _round_tick_down(best_bid, tick)
        if join_px <= max_lead + EPS and join_px <= ceil_lead + EPS:
            return join_px
    return None


@dataclass
class MakerQuotePlan:
    strategy: str
    target_key: str
    event_id: str
    title: str
    settlement: Settlement
    lead_market: Market
    lead_outcome: Outcome
    lead_price: float  # resting BUY price (0 maker fee)
    shares: float
    hedge_legs: list[tuple[Market, Outcome]]
    hedge_cost: float
    payout_floor: float
    resolves_at: Any
    neg_risk_market_id: str | None = None
    volume_24h: float = 0.0
    surplus_yes: list[dict] = field(default_factory=list)
    extra_event_ids: list[str] = field(default_factory=list)
    est_rebate_usd: float = 0.0
    fast_turnover: bool = False
    ask_gap: float = 0.01

    @property
    def lead_cost(self) -> float:
        return self.lead_price * self.shares

    @property
    def total_cost(self) -> float:
        return self.lead_cost + self.hedge_cost

    @property
    def profit(self) -> float:
        return self.payout_floor - self.total_cost

    @property
    def edge(self) -> float:
        return self.profit / self.total_cost if self.total_cost > 0 else 0.0

    def effective_profit(self, rebate_credit_ratio: float = 0.0) -> float:
        return self.profit + max(0.0, rebate_credit_ratio) * self.est_rebate_usd

    def effective_edge(self, rebate_credit_ratio: float = 0.0) -> float:
        return self.effective_profit(rebate_credit_ratio) / self.total_cost if self.total_cost > 0 else 0.0

    def velocity_score(self, rebate_credit_ratio: float = 0.0, now_s: float | None = None) -> float:
        """Expected fill-rate & capital-velocity score: E[PnL/hr] ~ P(fill | ask_gap) * profit * log(vol) / sqrt(hours)."""
        fill_w = math.exp(-22.0 * max(0.0, self.ask_gap - 0.01))
        if self.settlement is Settlement.CONVERT:
            lock_h = 0.10
        elif self.fast_turnover:
            lock_h = 0.35
        elif self.resolves_at is not None and hasattr(self.resolves_at, "timestamp"):
            cur = time.time() if now_s is None else now_s
            lock_h = max(0.25, (self.resolves_at.timestamp() - cur) / 3600.0)
        else:
            lock_h = 6.0
        return fill_w * min(self.effective_profit(rebate_credit_ratio), 1.60) * math.log1p(max(self.volume_24h, 1.0)) / math.sqrt(lock_h)


@dataclass
class ActiveQuote:
    key: str
    order_id: str
    plan: MakerQuotePlan
    reserved_usd: float
    placed_at: float

    @property
    def lead_cost(self) -> float:
        return self.plan.lead_cost

    @property
    def hedge_cost(self) -> float:
        return self.plan.hedge_cost


class MakerArbManager:
    def __init__(
        self,
        cfg: Config,
        store: BookStore,
        gateway: Gateway,
        risk: RiskManager,
        journal: Journal,
        clob: ClobClient | None = None,
        strategies: Mapping[str, Any] | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.gateway = gateway
        self.risk = risk
        self.journal = journal
        self.clob = clob
        self.strategies: dict[str, Any] = dict(strategies or {})
        self.targets: list[Target] = []
        self._target_map: dict[str, Target] = {}
        self.active: dict[str, ActiveQuote] = {}
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._last_scan_at: float = 0.0
        self.quotes_placed = 0
        self.quotes_canceled = 0
        self.quotes_filled = 0
        self.quotes_repegged = 0
        self.quotes_preempted = 0
        self.rebates_est_usd = 0.0

    def set_strategies(self, strategies: Mapping[str, Any]) -> None:
        self.strategies = dict(strategies)

    def set_targets(self, targets: list[Target]) -> None:
        self.targets = [
            t
            for t in targets
            if t.strategy in (
                "negrisk_convert",
                "metar_lock",
                "binance_lock",
                "crypto_short",
                "ladder",
                "negrisk_yes_basket",
                "twin_arb",
            )
        ]
        self._target_map = {t.key: t for t in self.targets}

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

    def _pooled_reserved_for(self, quotes: Sequence[ActiveQuote]) -> float:
        if not quotes:
            return 0.0
        slots = max(1, int(self.cfg.maker.pool_hedge_slots))
        lead_sum = sum(q.lead_cost for q in quotes)
        top_hedges = sorted((q.hedge_cost for q in quotes if q.hedge_cost > 0), reverse=True)[:slots]
        return round(lead_sum + sum(top_hedges), 4)

    def _recompute_reserved(self) -> None:
        self.risk.reserved_usd = self._pooled_reserved_for(list(self.active.values()))

    def _active_for_event(self, event_id: str) -> list[ActiveQuote]:
        return [q for q in self.active.values() if q.plan.event_id == event_id]

    def _quote_key_for(self, plan: MakerQuotePlan) -> str:
        if plan.event_id not in self.active:
            return plan.event_id
        return f"{plan.event_id}:{plan.lead_market.id}:{plan.lead_outcome.value}"

    def _unreserved_gross_cash(self) -> float:
        return self.risk.available() + self.risk.reserved_usd

    def _maker_pool_ceiling(self, settlement: Settlement, fast_turnover: bool = False) -> float:
        raw_gross = self._unreserved_gross_cash()
        frac = min(1.0, max(0.1, float(self.cfg.maker.max_reserve_fraction)))
        ceil_cash = raw_gross * frac
        if settlement is Settlement.RESOLUTION and self.cfg.risk.liquid_reserve_usd > 0 and not fast_turnover:
            ceil_cash = min(ceil_cash, max(0.0, raw_gross - self.cfg.risk.liquid_reserve_usd))
        return max(0.0, ceil_cash)

    def _max_shares_by_pool(
        self, lead_price: float, unit_hedge: float, settlement: Settlement, fast_turnover: bool = False
    ) -> float:
        """Exact closed-form share cap so pooled reservation never exceeds maker pool ceiling."""
        gross = self._maker_pool_ceiling(settlement, fast_turnover=fast_turnover)
        cur_res = self._pooled_reserved_for(list(self.active.values()))
        delta = max(0.0, gross - cur_res)
        if delta <= EPS or lead_price <= 0:
            return 0.0
        if unit_hedge <= EPS:
            return floor_shares(delta / lead_price)
        slots = max(1, int(self.cfg.maker.pool_hedge_slots))
        unit_total = lead_price + unit_hedge
        hedged_active = [q for q in self.active.values() if q.hedge_cost > 0]
        if len(hedged_active) < slots:
            return floor_shares(delta / unit_total)
        top_hedges = sorted((q.hedge_cost for q in hedged_active), reverse=True)[:slots]
        min_top_h = top_hedges[-1] if top_hedges else 0.0
        return floor_shares(min(delta / lead_price, (delta + min_top_h) / unit_total))

    def _planning_budget(self, settlement: Settlement, event_id: str, fast_turnover: bool = False) -> float:
        rcfg = self.cfg.risk
        locked_leads = sum(q.lead_cost for q in self.active.values())
        free_cash = max(0.0, self._unreserved_gross_cash() - locked_leads)
        cap = min(rcfg.max_trade_usd, free_cash)
        if settlement is Settlement.RESOLUTION:
            active_res_cost = sum(
                q.plan.total_cost for q in self.active.values() if q.plan.settlement is Settlement.RESOLUTION
            )
            active_ev_cost = sum(
                q.plan.total_cost for q in self.active.values() if q.plan.event_id == event_id
            )
            cap = min(cap, rcfg.max_hold_exposure_usd - self.risk.hold_exposure - active_res_cost)
            cap = min(cap, rcfg.max_event_exposure_usd - self.risk.event_exposure(event_id) - active_ev_cost)
            if rcfg.liquid_reserve_usd > 0 and not fast_turnover:
                cap = min(cap, free_cash - rcfg.liquid_reserve_usd)
        return max(0.0, cap)

    def _can_reserve_plan(self, plan: MakerQuotePlan) -> bool:
        hypothetical = [*self.active.values(), ActiveQuote("_", "_", plan, plan.total_cost, 0.0)]
        new_reserved = self._pooled_reserved_for(hypothetical)
        gross = self._unreserved_gross_cash()
        pool_ceil = self._maker_pool_ceiling(plan.settlement, fast_turnover=plan.fast_turnover)
        if new_reserved > pool_ceil + EPS:
            return False
        other_leads = sum(q.lead_cost for q in self.active.values())
        if other_leads + plan.total_cost > gross + EPS:
            return False
        if plan.settlement is Settlement.RESOLUTION:
            rcfg = self.cfg.risk
            active_res_cost = sum(
                q.plan.total_cost for q in self.active.values() if q.plan.settlement is Settlement.RESOLUTION
            )
            active_ev_cost = sum(
                q.plan.total_cost for q in self.active.values() if q.plan.event_id == plan.event_id
            )
            if self.risk.hold_exposure + active_res_cost + plan.total_cost > rcfg.max_hold_exposure_usd + EPS:
                return False
            if self.risk.event_exposure(plan.event_id) + active_ev_cost + plan.total_cost > rcfg.max_event_exposure_usd + EPS:
                return False
        return True

    def _passes_maker_gates(
        self,
        plan: MakerQuotePlan,
        mcfg: Any,
        min_edge_override: float | None = None,
        min_profit_override: float | None = None,
    ) -> bool:
        """Strictly require Day-0 positive cash profit while crediting maker rebate toward gates."""
        if plan.profit <= 0.02:
            return False
        req_edge = mcfg.min_edge if min_edge_override is None else min_edge_override
        req_profit = mcfg.min_profit_usd if min_profit_override is None else min_profit_override
        rc = mcfg.rebate_credit_ratio
        return plan.effective_profit(rc) >= req_profit and plan.effective_edge(rc) >= req_edge

    def _hedge_cost_and_depth(
        self, hedge_legs: list[tuple[Market, Outcome]], shares: float
    ) -> tuple[float, float] | None:
        if not hedge_legs:
            return 0.0, math.inf
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
            total_cost += fill.notional + fill.fee
        return total_cost, min_ratio

    def _max_quotes_for_strategy(self, strategy_or_settlement: Any) -> int:
        mcfg = self.cfg.maker
        if strategy_or_settlement is Settlement.CONVERT or strategy_or_settlement in (
            "negrisk_convert",
            "maker_convert",
            "metar_lock",
            "binance_lock",
            "crypto_short",
            "maker_metar",
            "maker_binance",
            "maker_crypto_short",
        ):
            return max(1, mcfg.max_quotes_per_event)
        return 1

    def _scan_target_subset(
        self, targets: Sequence[Target], now: float
    ) -> tuple[list[MakerQuotePlan], list[Target]]:
        mcfg = self.cfg.maker
        plans_by_event: dict[str, list[MakerQuotePlan]] = {}
        matched_targets: list[Target] = []
        now_dt = utc(now)

        for target in targets:
            ev = target.event
            if ev.id in self.risk.inflight_events:
                continue
            last_ev = self.risk.last_attempt.get(f"event:{ev.id}")
            if last_ev is not None and now - last_ev < self.cfg.risk.cooldown_s:
                continue
            existing = self._active_for_event(ev.id)
            max_per_ev = self._max_quotes_for_strategy(target.strategy)
            if len(existing) >= max_per_ev:
                continue
            quoted_leads = {(q.plan.lead_market.id, q.plan.lead_outcome) for q in existing}

            new_plans: list[MakerQuotePlan] = []
            if target.strategy == "negrisk_convert":
                new_plans = self._plan_convert(target, mcfg, exclude_leads=quoted_leads)
            elif target.strategy == "metar_lock":
                new_plans = self._plan_metar(target, mcfg, now_dt, exclude_leads=quoted_leads)
            elif target.strategy == "binance_lock":
                new_plans = self._plan_binance(target, mcfg, now_dt, exclude_leads=quoted_leads)
            elif target.strategy == "crypto_short":
                new_plans = self._plan_crypto_short(target, mcfg, now_dt, now, exclude_leads=quoted_leads)
            elif target.strategy == "ladder":
                p = self._plan_ladder(target, mcfg, now_dt)
                if p is not None and (p.lead_market.id, p.lead_outcome) not in quoted_leads:
                    new_plans = [p]
            elif target.strategy == "negrisk_yes_basket":
                p = self._plan_yes_basket(target, mcfg, now_dt)
                if p is not None and (p.lead_market.id, p.lead_outcome) not in quoted_leads:
                    new_plans = [p]
            elif target.strategy == "twin_arb":
                p = self._plan_twin(target, mcfg, now_dt)
                if p is not None and (p.lead_market.id, p.lead_outcome) not in quoted_leads:
                    new_plans = [p]

            if new_plans:
                matched_targets.append(target)
                plans_by_event.setdefault(ev.id, []).extend(new_plans)

        rc = mcfg.rebate_credit_ratio
        candidates: list[MakerQuotePlan] = []
        for ev_id, ev_plans in plans_by_event.items():
            by_lead: dict[tuple[str, Outcome], MakerQuotePlan] = {}
            for p in ev_plans:
                lk = (p.lead_market.id, p.lead_outcome)
                if lk not in by_lead or p.effective_profit(rc) > by_lead[lk].effective_profit(rc):
                    by_lead[lk] = p
            ranked = sorted(by_lead.values(), key=lambda p: p.effective_profit(rc), reverse=True)
            multi_ok = any(p.settlement is Settlement.CONVERT or p.strategy in ZERO_LEG_STRATS for p in ranked)
            existing_n = len(self._active_for_event(ev_id))
            cap_n = max(0, (max(1, mcfg.max_quotes_per_event) if multi_ok else 1) - existing_n)
            candidates.extend(ranked[:cap_n])

        candidates.sort(
            key=lambda p: (
                p.settlement is Settlement.CONVERT or p.strategy in ZERO_LEG_STRATS,
                p.velocity_score(rc, now),
            ),
            reverse=True,
        )
        return candidates, matched_targets

    def find_candidates(self, now: float | None = None) -> list[MakerQuotePlan]:
        cands, _ = self._scan_target_subset(self.targets, now or time.time())
        return cands

    async def _find_candidates_async(self, now: float, chunk: int = 80) -> tuple[list[MakerQuotePlan], list[Target]]:
        """Cooperative full-universe scan that yields every ``chunk`` targets so HTTP /healthz stays <10ms."""
        all_plans: list[MakerQuotePlan] = []
        shortlist_targets: list[Target] = []
        targets = self.targets
        for start in range(0, len(targets), chunk):
            if start > 0:
                await asyncio.sleep(0)
            part_plans, part_targets = self._scan_target_subset(targets[start : start + chunk], now)
            if part_plans:
                all_plans.extend(part_plans)
                shortlist_targets.extend(part_targets)

        rc = self.cfg.maker.rebate_credit_ratio
        # Deduplicate across multi-target events (e.g. convert + metar on same event)
        by_ev: dict[str, list[MakerQuotePlan]] = {}
        for p in all_plans:
            by_ev.setdefault(p.event_id, []).append(p)
        merged: list[MakerQuotePlan] = []
        for ev_id, ev_plans in by_ev.items():
            by_lead: dict[tuple[str, Outcome], MakerQuotePlan] = {}
            for p in ev_plans:
                lk = (p.lead_market.id, p.lead_outcome)
                if lk not in by_lead or p.effective_profit(rc) > by_lead[lk].effective_profit(rc):
                    by_lead[lk] = p
            ranked = sorted(by_lead.values(), key=lambda p: p.effective_profit(rc), reverse=True)
            multi_ok = any(p.settlement is Settlement.CONVERT or p.strategy in ZERO_LEG_STRATS for p in ranked)
            existing_n = len(self._active_for_event(ev_id))
            cap_n = max(0, (max(1, self.cfg.maker.max_quotes_per_event) if multi_ok else 1) - existing_n)
            merged.extend(ranked[:cap_n])
        merged.sort(
            key=lambda p: (
                p.settlement is Settlement.CONVERT or p.strategy in ZERO_LEG_STRATS,
                p.velocity_score(rc, now),
            ),
            reverse=True,
        )
        return merged, shortlist_targets

    def _plan_zero_leg_locks(
        self,
        *,
        strategy_name: str,
        target: Target,
        locked_items: list[tuple[Market, Outcome, Any]],
        mcfg: Any,
        exclude_leads: set[tuple[str, Outcome]],
        now_s: float | None = None,
    ) -> list[MakerQuotePlan]:
        """Plan 0-hedge-leg postOnly quotes on mathematically decided METAR / Binance / Short-Crypto markets."""
        if not locked_items:
            return []
        ev = target.event
        ev_vol = sum(m.volume_24h for m in ev.tradeable_markets)
        budget = self._planning_budget(Settlement.RESOLUTION, ev.id, fast_turnover=True)
        if budget < 2.0:
            return []

        plans: list[MakerQuotePlan] = []
        req_edge = mcfg.deterministic_min_edge
        if strategy_name == "maker_crypto_short":
            det_floor = self.cfg.crypto_short.min_profit_usd
        elif strategy_name == "maker_binance":
            det_floor = self.cfg.binance.min_profit_usd
        else:
            det_floor = self.cfg.metar.min_profit_usd
        req_profit = min(0.10, mcfg.min_profit_usd, det_floor)
        cur_now = time.time() if now_s is None else now_s
        for m, out, resolves_at in locked_items:
            if (m.id, out) in exclude_leads:
                continue
            last_m = self.risk.last_attempt.get(f"mkt:{m.id}")
            if last_m is not None and cur_now - last_m < self.cfg.risk.cooldown_s:
                continue
            b = self.store.get(m.yes_token)
            if b is None:
                continue
            asks = b.asks if out is Outcome.YES else b.no_asks()
            bids = b.bids if out is Outcome.YES else b.no_bids()
            if not bids:
                continue
            # Gamma keeps reporting 0.01 after the CLOB has tightened the live book to 0.001.
            # Quoting on the coarse grid joins the back of the 0.99 wall; the fine grid steps in front of it.
            tick = m.tick_size
            book_tick = b.tick_size
            if book_tick and 0 < book_tick < tick - 1e-12:
                tick = float(book_tick)
                m.tick_size = tick
            best_bid = bids[0].price
            # Decided markets often have NO remaining sellers (asks == []), so implicit ceiling is 1.0!
            best_ask = asks[0].price if asks else 1.0
            if best_bid < mcfg.deterministic_min_bid - EPS:
                continue
            vol_score = m.volume_24h + ev_vol * 0.1 + bids[0].size * best_bid
            min_vol_req = 10.0 if strategy_name == "maker_crypto_short" else max(50.0, mcfg.min_volume_24h * 0.2)
            if vol_score < min_vol_req:
                continue

            # Account for potential maker rebate when choosing max_lead, capped strictly <= 0.993 for flip headroom
            fine = strategy_name == "maker_crypto_short" and tick <= 0.0015
            px_cap = 0.996 if fine else 0.993
            rebate_u = mcfg.rebate_credit_ratio * maker_rebate_per_share(m.fee, min(best_bid + tick, px_cap))
            raw_max = (1.0 + rebate_u) / (1.0 + req_edge)
            max_lead = _round_tick_down(min(px_cap, raw_max), tick)
            our_bid = _best_maker_bid(
                best_bid, best_ask, tick, max_lead, mcfg.allow_join_bid, max_step_ticks=4.0 if fine else 2.0
            )
            if our_bid is None or our_bid >= 0.995 - EPS:
                continue

            min_sh = max(m.min_size, math.ceil(MIN_ORDER_NOTIONAL / our_bid * 100) / 100)
            shares = min(
                floor_shares(budget / our_bid),
                self._max_shares_by_pool(our_bid, 0.0, Settlement.RESOLUTION, fast_turnover=True),
            )
            if shares < min_sh - EPS or our_bid * shares > budget + EPS:
                continue
            est_reb = shares * maker_rebate_per_share(m.fee, our_bid)
            plan = MakerQuotePlan(
                strategy=strategy_name,
                target_key=target.key,
                event_id=ev.id,
                title=ev.title,
                settlement=Settlement.RESOLUTION,
                lead_market=m,
                lead_outcome=out,
                lead_price=our_bid,
                shares=shares,
                hedge_legs=[],
                hedge_cost=0.0,
                payout_floor=shares,
                resolves_at=resolves_at,
                volume_24h=vol_score,
                est_rebate_usd=est_reb,
                fast_turnover=True,
                ask_gap=max(0.0, best_ask - our_bid) if asks else 0.01,
            )
            if self._passes_maker_gates(plan, mcfg, min_edge_override=req_edge, min_profit_override=req_profit):
                plans.append(plan)

        plans.sort(key=lambda p: p.effective_profit(mcfg.rebate_credit_ratio), reverse=True)
        return plans

    def _plan_metar(
        self, target: Target, mcfg: Any, now_dt: Any, exclude_leads: set[tuple[str, Outcome]]
    ) -> list[MakerQuotePlan]:
        strat = self.strategies.get("metar_lock")
        if strat is None or not hasattr(strat, "locked_markets"):
            return []
        plans = self._plan_zero_leg_locks(
            strategy_name="maker_metar",
            target=target,
            locked_items=strat.locked_markets(target, now_dt),
            mcfg=mcfg,
            exclude_leads=exclude_leads,
        )
        # Also evaluate METAR-pruned surviving YES basket if 2..max_legs brackets remain!
        surv = strat.surviving_basket(target, now_dt) if hasattr(strat, "surviving_basket") else None
        if surv is not None:
            surviving_mkts, resolves_at = surv
            if len(surviving_mkts) <= mcfg.max_legs:
                ev_vol = sum(m.volume_24h for m in target.event.tradeable_markets)
                if ev_vol >= mcfg.min_volume_24h:
                    budget = self._planning_budget(Settlement.RESOLUTION, target.event.id, fast_turnover=True)
                    if budget >= 4.0:
                        b_plan = self._plan_multi_leg_resolution(
                            strategy_name="maker_yes_basket",
                            target_key=target.key,
                            event_id=target.event.id,
                            title=target.event.title,
                            leg_specs=[(sm, Outcome.YES) for sm in surviving_mkts],
                            resolves_at=resolves_at,
                            budget=budget,
                            ev_vol=ev_vol,
                            mcfg=mcfg,
                            fast_turnover=True,
                        )
                        if b_plan is not None and (b_plan.lead_market.id, b_plan.lead_outcome) not in exclude_leads:
                            plans.append(b_plan)
        return plans

    def _plan_binance(
        self, target: Target, mcfg: Any, now_dt: Any, exclude_leads: set[tuple[str, Outcome]]
    ) -> list[MakerQuotePlan]:
        strat = self.strategies.get("binance_lock")
        if strat is None or not hasattr(strat, "locked_markets"):
            return []
        return self._plan_zero_leg_locks(
            strategy_name="maker_binance",
            target=target,
            locked_items=strat.locked_markets(target, now_dt),
            mcfg=mcfg,
            exclude_leads=exclude_leads,
            now_s=now_dt.timestamp(),
        )

    def _plan_crypto_short(
        self, target: Target, mcfg: Any, now_dt: Any, now_s: float, exclude_leads: set[tuple[str, Outcome]]
    ) -> list[MakerQuotePlan]:
        strat = self.strategies.get("crypto_short")
        if strat is None:
            return []
        if isinstance(target.payload, ShortSingleSpec):
            return self._plan_zero_leg_locks(
                strategy_name="maker_crypto_short",
                target=target,
                locked_items=strat.locked_markets(target, now_dt),
                mcfg=mcfg,
                exclude_leads=exclude_leads,
                now_s=now_s,
            )
        if hasattr(strat, "active_pair_legs"):
            pair = strat.active_pair_legs(target.payload, now_s)
            if pair is None:
                return []
            leg_specs, resolves_at, pair_title, ev_ids = pair
            budget = self._planning_budget(Settlement.RESOLUTION, target.event.id, fast_turnover=True)
            if budget < 4.0:
                return []
            ev_vol = max(sum(m.volume_24h for m, _ in leg_specs), mcfg.min_volume_24h)
            p = self._plan_multi_leg_resolution(
                strategy_name="maker_crypto_pair",
                target_key=target.key,
                event_id=target.event.id,
                title=pair_title,
                leg_specs=leg_specs,
                resolves_at=resolves_at,
                budget=budget,
                ev_vol=ev_vol,
                mcfg=mcfg,
                extra_event_ids=[eid for eid in ev_ids if eid != target.event.id],
                fast_turnover=True,
            )
            if p is not None and (p.lead_market.id, p.lead_outcome) not in exclude_leads:
                return [p]
        return []

    def _plan_convert(
        self,
        target: Target,
        mcfg: Any,
        exclude_leads: set[tuple[str, Outcome]] | None = None,
    ) -> list[MakerQuotePlan]:
        ev = target.event
        markets: tuple[Market, ...] = target.payload
        ev_vol = sum(m.volume_24h for m in markets)
        if ev_vol < mcfg.min_volume_24h:
            return []
        budget = self._planning_budget(Settlement.CONVERT, ev.id)
        if budget < 4.0:
            return []
        exclude = exclude_leads or set()

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
            return []
        scored.sort(key=lambda x: -x[0])
        pool = scored[: mcfg.max_legs]
        haircut = self.cfg.convert.surplus_haircut
        surplus_depth_ratio = self.cfg.convert.surplus_min_depth_ratio
        min_lead_vol = min(50.0, mcfg.min_volume_24h * 0.05) if mcfg.min_volume_24h > 0 else 0.0

        plans: list[MakerQuotePlan] = []
        for idx, (_, lead_m, lead_b) in enumerate(pool):
            if (lead_m.id, Outcome.NO) in exclude:
                continue
            no_asks = lead_b.no_asks()
            no_bids = lead_b.no_bids()
            if not no_asks:
                continue
            best_no_ask = no_asks[0].price
            best_no_bid = no_bids[0].price if no_bids else lead_m.tick_size
            if (best_no_ask - best_no_bid) > mcfg.max_lead_spread + EPS:
                continue
            if lead_m.volume_24h < min_lead_vol and (best_no_ask - best_no_bid) > 0.04:
                continue

            others = [(g, m, b) for j, (g, m, b) in enumerate(pool) if j != idx]
            best_for_lead: MakerQuotePlan | None = None

            for k in range(1, len(others) + 1):
                subset = others[:k]
                chosen_ids = {lead_m.id, *(m.id for _, m, _ in subset)}
                surplus_mkts = [m for m in markets if m.id not in chosen_ids]

                unit_surplus = 0.0
                if haircut > 0.0 and surplus_mkts and (k + 1) <= 6:
                    for sm in surplus_mkts:
                        sb = self.store.get(sm.yes_token)
                        if sb and sb.bids:
                            tb = sb.bids[0].price
                            eff_h = min(0.95, haircut + 0.08) if tb >= 0.15 else haircut
                            unit_surplus += max(0.0, tb - fee_per_share(sm.fee, tb) - sm.tick_size) * eff_h

                unit_hedge_cost = sum(
                    (1.0 - b.bids[0].price) + fee_per_share(m.fee, 1.0 - b.bids[0].price) for _, m, b in subset
                )
                rebate_u = mcfg.rebate_credit_ratio * maker_rebate_per_share(lead_m.fee, best_no_bid)
                for use_surplus in ((False, True) if unit_surplus > 0.0 else (False,)):
                    base_payout_u = float(k) + (unit_surplus if use_surplus else 0.0)
                    # Cap lead price strictly below cash break-even AND within rebate-adjusted min_edge
                    cash_ceil = base_payout_u - unit_hedge_cost - max(lead_m.tick_size * 0.5, 0.0005)
                    edge_ceil = (base_payout_u + rebate_u) / (1.0 + mcfg.min_edge) - unit_hedge_cost
                    max_lead = _round_tick_down(min(cash_ceil, edge_ceil), lead_m.tick_size)
                    our_no_bid = _best_maker_bid(
                        best_no_bid, best_no_ask, lead_m.tick_size, max_lead, mcfg.allow_join_bid
                    )
                    if our_no_bid is None:
                        continue
                    unit_total = our_no_bid + unit_hedge_cost
                    min_sh = max(
                        [
                            lead_m.min_size,
                            *(m.min_size for _, m, _ in subset),
                            math.ceil(MIN_ORDER_NOTIONAL / our_no_bid * 100) / 100,
                        ]
                    )
                    max_by_budget = floor_shares(budget / unit_total)
                    max_by_pool = self._max_shares_by_pool(our_no_bid, unit_hedge_cost, Settlement.CONVERT)
                    max_by_depth = floor_shares(
                        min(hb.no_asks()[0].size for _, _, hb in subset) / mcfg.min_hedge_depth_ratio
                    )
                    shares = min(max_by_budget, max_by_pool, max_by_depth)
                    if shares < min_sh - EPS:
                        continue
                    hedge_legs = [(m, Outcome.NO) for _, m, _ in subset]
                    hc = self._hedge_cost_and_depth(hedge_legs, shares)
                    if hc is None or hc[1] < mcfg.min_hedge_depth_ratio:
                        continue
                    hedge_cost, _ = hc
                    if our_no_bid * shares + hedge_cost > budget + EPS:
                        continue
                    verified_surplus = haircut_surplus_proceeds(
                        surplus_mkts, shares, self.store, haircut, surplus_depth_ratio
                    )
                    payout_floor = float(k) * shares + verified_surplus
                    est_reb = shares * maker_rebate_per_share(lead_m.fee, our_no_bid)
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
                        payout_floor=payout_floor,
                        resolves_at=None,
                        neg_risk_market_id=ev.neg_risk_market_id,
                        volume_24h=lead_m.volume_24h + ev_vol * 0.05,
                        surplus_yes=surplus_yes_specs(markets, chosen_ids),
                        est_rebate_usd=est_reb,
                        fast_turnover=True,
                        ask_gap=max(0.0, best_no_ask - our_no_bid),
                    )
                    if self._passes_maker_gates(plan, mcfg):
                        rc = mcfg.rebate_credit_ratio
                        if best_for_lead is None or plan.effective_profit(rc) > best_for_lead.effective_profit(rc):
                            best_for_lead = plan

            if best_for_lead is not None:
                plans.append(best_for_lead)

        plans.sort(key=lambda p: p.effective_profit(mcfg.rebate_credit_ratio), reverse=True)
        return plans

    def _plan_multi_leg_resolution(
        self,
        *,
        strategy_name: str,
        target_key: str,
        event_id: str,
        title: str,
        leg_specs: Sequence[tuple[Market, Outcome]],
        resolves_at: Any,
        budget: float,
        ev_vol: float,
        mcfg: Any,
        extra_event_ids: list[str] | None = None,
        fast_turnover: bool = False,
    ) -> MakerQuotePlan | None:
        books_levels: list[tuple[Market, Outcome, list, list]] = []
        for m, out in leg_specs:
            b = self.store.get(m.yes_token)
            if b is None:
                return None
            asks = b.asks if out is Outcome.YES else b.no_asks()
            bids = b.bids if out is Outcome.YES else b.no_bids()
            if not asks:
                return None
            books_levels.append((m, out, asks, bids))

        min_lead_vol = min(50.0, mcfg.min_volume_24h * 0.05) if mcfg.min_volume_24h > 0 else 0.0
        best: MakerQuotePlan | None = None
        for idx, (lead_m, lead_out, asks, bids) in enumerate(books_levels):
            best_ask = asks[0].price
            best_bid = bids[0].price if bids else lead_m.tick_size
            if (best_ask - best_bid) > mcfg.max_lead_spread + EPS:
                continue
            if lead_m.volume_24h < min_lead_vol and (best_ask - best_bid) > 0.04:
                continue
            hedge_entries = [entry for j, entry in enumerate(books_levels) if j != idx]
            if not hedge_entries:
                continue
            unit_hedge = sum(a[0].price + fee_per_share(hm.fee, a[0].price) for hm, _, a, _ in hedge_entries)
            rebate_u = mcfg.rebate_credit_ratio * maker_rebate_per_share(lead_m.fee, best_bid)
            cash_ceil = 1.0 - unit_hedge - max(lead_m.tick_size * 0.5, 0.0005)
            edge_ceil = (1.0 + rebate_u) / (1.0 + mcfg.min_edge) - unit_hedge
            max_lead = _round_tick_down(min(cash_ceil, edge_ceil), lead_m.tick_size)
            our_bid = _best_maker_bid(best_bid, best_ask, lead_m.tick_size, max_lead, mcfg.allow_join_bid)
            if our_bid is None:
                continue
            unit_total = our_bid + unit_hedge
            min_sh = max(
                [
                    lead_m.min_size,
                    *(hm.min_size for hm, _, _, _ in hedge_entries),
                    math.ceil(MIN_ORDER_NOTIONAL / our_bid * 100) / 100,
                ]
            )
            min_top_hedge = min(a[0].size for _, _, a, _ in hedge_entries)
            shares = min(
                floor_shares(budget / unit_total),
                self._max_shares_by_pool(our_bid, unit_hedge, Settlement.RESOLUTION, fast_turnover=fast_turnover),
                floor_shares(min_top_hedge / mcfg.min_hedge_depth_ratio),
            )
            if shares < min_sh - EPS:
                continue
            hedge_legs = [(hm, hout) for hm, hout, _, _ in hedge_entries]
            hc = self._hedge_cost_and_depth(hedge_legs, shares)
            if hc is None or hc[1] < mcfg.min_hedge_depth_ratio:
                continue
            if our_bid * shares + hc[0] > budget + EPS:
                continue
            est_reb = shares * maker_rebate_per_share(lead_m.fee, our_bid)
            plan = MakerQuotePlan(
                strategy=strategy_name,
                target_key=target_key,
                event_id=event_id,
                title=title,
                settlement=Settlement.RESOLUTION,
                lead_market=lead_m,
                lead_outcome=lead_out,
                lead_price=our_bid,
                shares=shares,
                hedge_legs=hedge_legs,
                hedge_cost=hc[0],
                payout_floor=shares,
                resolves_at=resolves_at,
                volume_24h=lead_m.volume_24h + ev_vol * 0.05,
                extra_event_ids=list(extra_event_ids or []),
                est_rebate_usd=est_reb,
                fast_turnover=fast_turnover,
                ask_gap=max(0.0, best_ask - our_bid),
            )
            if self._passes_maker_gates(plan, mcfg):
                rc = mcfg.rebate_credit_ratio
                if best is None or plan.effective_profit(rc) > best.effective_profit(rc):
                    best = plan
        return best

    def _plan_ladder(self, target: Target, mcfg: Any, now_dt: Any) -> MakerQuotePlan | None:
        ev = target.event
        ladder: Ladder = target.payload
        ev_vol = sum(r.market.volume_24h for r in ladder.rungs)
        if ev_vol < mcfg.min_volume_24h:
            return None
        budget = self._planning_budget(Settlement.RESOLUTION, ev.id)
        if budget < 4.0:
            return None

        best: MakerQuotePlan | None = None
        rungs = ladder.rungs
        rc = mcfg.rebate_credit_ratio
        for i, strong in enumerate(rungs):
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
                plan = self._plan_multi_leg_resolution(
                    strategy_name="maker_ladder",
                    target_key=target.key,
                    event_id=ev.id,
                    title=ev.title,
                    leg_specs=[(weak.market, Outcome.YES), (strong.market, Outcome.NO)],
                    resolves_at=resolves_at,
                    budget=budget,
                    ev_vol=max(weak.market.volume_24h + strong.market.volume_24h, ev_vol * 0.2),
                    mcfg=mcfg,
                )
                if plan is not None and (best is None or plan.effective_profit(rc) > best.effective_profit(rc)):
                    best = plan
        return best

    def _plan_yes_basket(self, target: Target, mcfg: Any, now_dt: Any) -> MakerQuotePlan | None:
        ev = target.event
        markets, _ = target.payload
        if len(markets) > mcfg.max_legs:
            return None
        ev_vol = sum(m.volume_24h for m in markets)
        if ev_vol < mcfg.min_volume_24h:
            return None
        ends = [m.end_date for m in markets if m.end_date]
        if not ends:
            return None
        resolves_at = max(ends) + timedelta(days=self.cfg.yes_basket.resolution_lag_days)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > mcfg.max_ladder_days:
            return None
        budget = self._planning_budget(Settlement.RESOLUTION, ev.id)
        if budget < 4.0:
            return None
        return self._plan_multi_leg_resolution(
            strategy_name="maker_yes_basket",
            target_key=target.key,
            event_id=ev.id,
            title=ev.title,
            leg_specs=[(m, Outcome.YES) for m in markets],
            resolves_at=resolves_at,
            budget=budget,
            ev_vol=ev_vol,
            mcfg=mcfg,
        )

    def _plan_twin(self, target: Target, mcfg: Any, now_dt: Any) -> MakerQuotePlan | None:
        sl: TwinSlice = target.payload
        specs = sl.leg_specs
        if len(specs) > mcfg.max_legs:
            return None
        ev_vol = sum(m.volume_24h for m, _ in specs)
        if ev_vol < mcfg.min_volume_24h:
            return None
        ends = [m.end_date for m, _ in specs if m.end_date]
        if not ends:
            return None
        resolves_at = max(ends) + timedelta(days=self.cfg.twin.resolution_lag_days)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > mcfg.max_ladder_days:
            return None
        budget = self._planning_budget(Settlement.RESOLUTION, sl.primary_event.id)
        if budget < 4.0:
            return None
        return self._plan_multi_leg_resolution(
            strategy_name="maker_twin",
            target_key=target.key,
            event_id=sl.primary_event.id,
            title=sl.label or f"{sl.primary_event.title} ⇄ >{sl.strike:g}",
            leg_specs=specs,
            resolves_at=resolves_at,
            budget=budget,
            ev_vol=ev_vol,
            mcfg=mcfg,
            extra_event_ids=[sl.secondary_event.id],
        )

    def _has_pool_headroom(self) -> bool:
        """Fast O(1) check: skip scanning 4,800+ targets when pooled reservation is already within $1 of cap."""
        gross = self._maker_pool_ceiling(Settlement.CONVERT, fast_turnover=True)
        cur_res = self._pooled_reserved_for(list(self.active.values()))
        return (gross - cur_res) >= MIN_ORDER_NOTIONAL - EPS

    async def preempt_for_taker(self, required_available_usd: float) -> float:
        """Evict lowest-velocity unfilled resting maker quotes so a guaranteed Taker snipe never starves."""
        if not self.active or self.risk.available() >= required_available_usd - EPS:
            return self.risk.available()
        rc = self.cfg.maker.rebate_credit_ratio
        now = time.time()
        ordered = sorted(
            list(self.active.items()),
            key=lambda kv: kv[1].plan.velocity_score(rc, now),
        )
        for qkey, quote in ordered:
            if self.risk.available() >= required_available_usd - EPS:
                break
            await self.gateway.cancel(quote.order_id)
            final = await self.gateway.get_order_state(quote.order_id)
            self.active.pop(qkey, None)
            is_zero_leg = not quote.plan.hedge_legs
            if final is not None and final.size_matched >= 0.5:
                co_fills = await self._collect_sibling_fills(quote.plan.event_id, qkey, cancel_siblings=not is_zero_leg)
                self._recompute_reserved()
                await self._hedge_fill(quote, final.size_matched, final.price or quote.plan.lead_price, co_fills=co_fills)
            else:
                self._recompute_reserved()
                self.quotes_canceled += 1
                self.quotes_preempted += 1
                log.info("PREEMPTED maker quote on %s to fund instant Taker snipe", quote.plan.title[:40])
        return self.risk.available()

    async def step(self, scan_candidates: bool = True) -> None:
        now = time.time()
        before_count = len(self.active)
        for qkey in list(self.active):
            quote = self.active.get(qkey)
            if quote is not None:
                await self._check_active(qkey, quote, now)

        if self._paused_or_blocked():
            if self.active:
                await self.cancel_all("paused or risk gate active")
            return

        while len(self.active) > self.cfg.maker.max_open_quotes:
            qkey, oldest = min(self.active.items(), key=lambda item: item[1].placed_at)
            await self.gateway.cancel(oldest.order_id)
            self.active.pop(qkey, None)
            self._recompute_reserved()
            self.quotes_canceled += 1

        # If a quote just filled or repegged, allow immediate replacement if >=0.5s since last scan.
        freed_slot = len(self.active) < before_count
        if not scan_candidates and not (freed_slot and now - self._last_scan_at >= 0.5):
            return
        if len(self.active) >= self.cfg.maker.max_open_quotes or not self._has_pool_headroom():
            return

        self._last_scan_at = now
        candidates, shortlist_targets = await self._find_candidates_async(now)
        if not candidates or not shortlist_targets:
            return

        first_iter = True
        while len(self.active) < self.cfg.maker.max_open_quotes and self._has_pool_headroom():
            if not first_iter:
                # Re-size only the tiny shortlist (<=20 targets) against updated pooled headroom!
                candidates, _ = self._scan_target_subset(shortlist_targets, now)
            first_iter = False
            placed_one = False
            for plan in candidates:
                max_per_ev = self._max_quotes_for_strategy(plan.strategy)
                existing_ev = self._active_for_event(plan.event_id)
                if len(existing_ev) >= max_per_ev:
                    continue
                if any(
                    q.plan.lead_market.id == plan.lead_market.id and q.plan.lead_outcome is plan.lead_outcome
                    for q in existing_ev
                ):
                    continue
                if not self._can_reserve_plan(plan):
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
                qkey = self._quote_key_for(plan)
                self.active[qkey] = ActiveQuote(
                    key=qkey,
                    order_id=res.order_id,
                    plan=plan,
                    reserved_usd=plan.total_cost,
                    placed_at=now,
                )
                self._recompute_reserved()
                self.quotes_placed += 1
                placed_one = True
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
                break
            if not placed_one:
                break

    async def _collect_sibling_fills(
        self, event_id: str, primary_key: str, cancel_siblings: bool = True
    ) -> list[tuple[ActiveQuote, float, float]]:
        if not cancel_siblings:
            return []
        co_fills: list[tuple[ActiveQuote, float, float]] = []
        for skey, sib in list(self.active.items()):
            if skey == primary_key or sib.plan.event_id != event_id:
                continue
            if sib.plan.strategy in ZERO_LEG_STRATS:
                continue  # Independent 0-hedge-leg locks on different brackets do not interfere!
            await self.gateway.cancel(sib.order_id)
            st = await self.gateway.get_order_state(sib.order_id)
            self.active.pop(skey, None)
            if st is not None and st.size_matched >= 0.5:
                co_fills.append((sib, st.size_matched, st.price or sib.plan.lead_price))
            else:
                self.quotes_canceled += 1
        return co_fills

    async def _check_active(self, qkey: str, quote: ActiveQuote, now: float) -> None:
        state = await self.gateway.get_order_state(quote.order_id)
        is_zero_leg = not quote.plan.hedge_legs

        if state is not None and state.size_matched >= 0.5:
            if state.status == "LIVE":
                await self.gateway.cancel(quote.order_id)
                recheck = await self.gateway.get_order_state(quote.order_id)
                if recheck is not None and recheck.size_matched > state.size_matched:
                    state = recheck
            self.active.pop(qkey, None)
            co_fills = await self._collect_sibling_fills(quote.plan.event_id, qkey, cancel_siblings=not is_zero_leg)
            self._recompute_reserved()
            await self._hedge_fill(quote, state.size_matched, state.price or quote.plan.lead_price, co_fills=co_fills)
            return

        if state is not None and state.status in ("CANCELED", "MATCHED") and state.size_matched < 0.5:
            self.active.pop(qkey, None)
            self._recompute_reserved()
            return

        should_cancel = False
        is_repeg = False
        reason = ""
        if self._paused_or_blocked():
            should_cancel, reason = True, "paused/risk"
        elif now - quote.placed_at > self.cfg.maker.quote_ttl_s:
            should_cancel, reason = True, "ttl expired"
        elif is_zero_leg:
            if quote.plan.strategy == "maker_crypto_short":
                strat = self.strategies.get("crypto_short")
                tgt = self._target_map.get(quote.plan.target_key)
                if strat is not None and tgt is not None and hasattr(strat, "still_safely_locked"):
                    if not strat.still_safely_locked(tgt, quote.plan.lead_outcome, now):
                        should_cancel, reason = True, "short-window lock expired or cushion narrowed"
            if not should_cancel and self.cfg.maker.repeg_after_s > 0 and now - quote.placed_at >= self.cfg.maker.repeg_after_s:
                lb = self.store.get(quote.plan.lead_market.yes_token)
                if lb is not None:
                    bids = lb.bids if quote.plan.lead_outcome is Outcome.YES else lb.no_bids()
                    if bids and bids[0].price > quote.plan.lead_price + EPS:
                        should_cancel, is_repeg, reason = True, True, "outbid -> repeg"
        else:
            hc = self._hedge_cost_and_depth(quote.plan.hedge_legs, quote.plan.shares)
            if hc is None or hc[1] < 1.2:
                should_cancel, reason = True, "hedge depth thinned"
            else:
                live_cost = quote.plan.lead_price * quote.plan.shares + hc[0]
                reb = self.cfg.maker.rebate_credit_ratio * quote.plan.est_rebate_usd
                live_edge = (quote.plan.payout_floor + reb - live_cost) / live_cost if live_cost > 0 else -1.0
                if (quote.plan.payout_floor - live_cost) <= 0.0 or live_edge < self.cfg.maker.min_keep_edge:
                    should_cancel, reason = True, f"edge drifted to {100 * live_edge:.2f}%"
                elif self.cfg.maker.repeg_after_s > 0 and now - quote.placed_at >= self.cfg.maker.repeg_after_s:
                    lb = self.store.get(quote.plan.lead_market.yes_token)
                    if lb is not None:
                        bids = lb.bids if quote.plan.lead_outcome is Outcome.YES else lb.no_bids()
                        asks = lb.asks if quote.plan.lead_outcome is Outcome.YES else lb.no_asks()
                        if bids and asks and bids[0].price > quote.plan.lead_price + EPS:
                            should_cancel, is_repeg, reason = True, True, "outbid -> repeg"

        if should_cancel:
            await self.gateway.cancel(quote.order_id)
            final = await self.gateway.get_order_state(quote.order_id)
            self.active.pop(qkey, None)
            if final is not None and final.size_matched >= 0.5:
                co_fills = await self._collect_sibling_fills(quote.plan.event_id, qkey, cancel_siblings=not is_zero_leg)
                self._recompute_reserved()
                await self._hedge_fill(quote, final.size_matched, final.price or quote.plan.lead_price, co_fills=co_fills)
            else:
                self._recompute_reserved()
                self.quotes_canceled += 1
                if is_repeg:
                    self.quotes_repegged += 1
                log.debug("canceled maker quote on %s (%s)", quote.plan.title[:40], reason)

    async def _hedge_fill(
        self,
        quote: ActiveQuote,
        filled_shares: float,
        fill_price: float,
        co_fills: list[tuple[ActiveQuote, float, float]] | None = None,
    ) -> None:
        plan = quote.plan
        q = floor_shares(filled_shares)
        # A cancelled 5m quote can report a sub-dollar "match". That is not inventory.
        if q < 1.0 or q * fill_price < 1.0:
            log.info(
                "ignore dust maker fill on %s (%.4f sh @ %.3f)",
                plan.title[:42],
                filled_shares,
                fill_price,
            )
            return
        is_zero_leg = not plan.hedge_legs
        self.quotes_filled += 1 + len(co_fills or ())
        est_reb = q * maker_rebate_per_share(plan.lead_market.fee, fill_price)
        for sib, sib_sh, sib_px in co_fills or ():
            est_reb += floor_shares(min(sib_sh, q)) * maker_rebate_per_share(sib.plan.lead_market.fee, sib_px)
        self.rebates_est_usd = round(self.rebates_est_usd + est_reb, 4)

        lead_leg = Leg(
            token_id=plan.lead_market.token(plan.lead_outcome),
            market_id=plan.lead_market.id,
            outcome=plan.lead_outcome,
            side=Side.BUY,
            shares=q,
            notional=q * fill_price,
            fee=0.0,
            limit_price=fill_price,
            tick_size=plan.lead_market.tick_size,
            min_size=plan.lead_market.min_size,
            neg_risk=plan.lead_market.neg_risk,
            label=f"MAKER {plan.lead_outcome.value} {plan.lead_market.group_title or plan.lead_market.question[:45]}",
            yes_token=plan.lead_market.yes_token,
            fee_schedule=plan.lead_market.fee,
            neg_risk_index=plan.lead_market.neg_risk_index,
        )
        self.risk.begin(
            Opportunity(
                strategy=plan.strategy,
                key=f"{plan.strategy}:{plan.event_id}:{plan.lead_market.id}" if is_zero_leg else f"{plan.strategy}:{plan.event_id}",
                event_id=plan.event_id,
                title=plan.title,
                legs=[lead_leg],
                shares=q,
                cost=q * fill_price,
                payout_floor=q,
                settlement=plan.settlement,
                risk="low",
                resolves_at=plan.resolves_at,
                detected_at=time.time(),
                meta={"independent_legs": is_zero_leg},
            )
        )
        maker_results: list[LegResult] = [
            LegResult(
                leg=lead_leg,
                ok=True,
                shares=q,
                spent=q * fill_price,
                fee=0.0,
                order_id=quote.order_id,
                status="matched",
            )
        ]

        co_by_mkt: dict[tuple[str, Outcome], float] = {}
        for sib, sib_sh_raw, sib_px in co_fills or ():
            sib_q = floor_shares(min(sib_sh_raw, q))
            if sib_q <= 0:
                continue
            sm = sib.plan.lead_market
            sout = sib.plan.lead_outcome
            co_by_mkt[(sm.id, sout)] = co_by_mkt.get((sm.id, sout), 0.0) + sib_q
            sib_leg = Leg(
                token_id=sm.token(sout),
                market_id=sm.id,
                outcome=sout,
                side=Side.BUY,
                shares=sib_q,
                notional=sib_q * sib_px,
                fee=0.0,
                limit_price=sib_px,
                tick_size=sm.tick_size,
                min_size=sm.min_size,
                neg_risk=sm.neg_risk,
                label=f"MAKER {sout.value} {sm.group_title or sm.question[:45]}",
                yes_token=sm.yes_token,
                fee_schedule=sm.fee,
                neg_risk_index=sm.neg_risk_index,
            )
            maker_results.append(
                LegResult(
                    leg=sib_leg,
                    ok=True,
                    shares=sib_q,
                    spent=sib_q * sib_px,
                    fee=0.0,
                    order_id=sib.order_id,
                    status="matched",
                )
            )

        hedge_order_legs: list[Leg] = []
        hedge_possible = True
        for hm, h_out in plan.hedge_legs:
            already = co_by_mkt.get((hm.id, h_out), 0.0)
            need_q = floor_shares(max(0.0, q - already))
            if need_q < 0.01:
                continue
            book = self.store.raw(hm.yes_token)
            levels = (book.asks if h_out is Outcome.YES else book.no_asks()) if book else []
            fill = take(levels, need_q, hm.fee)
            if fill.shares < need_q - 1e-6 or fill.notional < MIN_ORDER_NOTIONAL - EPS:
                hedge_possible = False
                break
            hedge_order_legs.append(
                Leg(
                    token_id=hm.token(h_out),
                    market_id=hm.id,
                    outcome=h_out,
                    side=Side.BUY,
                    shares=need_q,
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

        distinct_markets = {r.leg.market_id for r in maker_results} | {hm.id for hm, _ in plan.hedge_legs}
        payout_mult = float(len(distinct_markets) - 1) if plan.settlement is Settlement.CONVERT else 1.0
        cond_ids = [
            m.condition_id
            for m in (
                plan.lead_market,
                *(sib.plan.lead_market for sib, _, _ in (co_fills or ())),
                *(hm for hm, _ in plan.hedge_legs),
            )
            if m.condition_id
        ]
        opp = Opportunity(
            strategy=plan.strategy,
            key=f"{plan.strategy}:{plan.event_id}:{plan.lead_market.id}" if is_zero_leg else f"{plan.strategy}:{plan.event_id}",
            event_id=plan.event_id,
            title=plan.title,
            legs=[*(r.leg for r in maker_results), *hedge_order_legs],
            shares=q,
            cost=sum(r.cost for r in maker_results) + sum(hl.notional + hl.fee for hl in hedge_order_legs),
            payout_floor=payout_mult * q,
            settlement=plan.settlement,
            risk="low" if (plan.settlement is Settlement.CONVERT or is_zero_leg) else "medium",
            resolves_at=plan.resolves_at,
            detected_at=quote.placed_at,
            meta={
                "neg_risk_market_id": plan.neg_risk_market_id,
                "condition_ids": list(dict.fromkeys(cond_ids)),
                "surplus_yes": plan.surplus_yes,
                "est_rebate_usd": round(est_reb, 5),
                "fast_turnover": plan.fast_turnover,
                "independent_legs": is_zero_leg,
                **({"event_ids": [plan.event_id, *plan.extra_event_ids]} if plan.extra_event_ids else {}),
            },
        )
        report = ExecutionReport(opp=opp, mode=self.gateway.mode, started_at=time.time(), sent=True)
        hedge_results = await self.gateway.buy(hedge_order_legs, slippage_ticks=1) if (hedge_possible and hedge_order_legs) else []
        report.legs = [*maker_results, *hedge_results]
        report.complete = hedge_possible and len(hedge_results) == len(hedge_order_legs) and all(r.ok for r in hedge_results)

        if report.complete:
            if plan.settlement is Settlement.CONVERT and plan.neg_risk_market_id:
                by_idx: dict[int, float] = {}
                burned_ids: set[str] = set()
                for r in report.legs:
                    if r.ok and r.leg.neg_risk_index is not None:
                        by_idx[r.leg.neg_risk_index] = by_idx.get(r.leg.neg_risk_index, 0.0) + r.shares
                        burned_ids.add(r.leg.market_id)
                conv_q = floor_shares(min(by_idx.values())) if len(by_idx) >= 2 else 0.0
                ok, tx, proceeds = (
                    await self.gateway.convert(plan.neg_risk_market_id, index_set(list(by_idx)), conv_q)
                    if conv_q > 0
                    else (False, None, 0.0)
                )
                if ok:
                    report.convert_tx = tx
                    report.proceeds = proceeds
                    report.settled_shares = conv_q
                    surplus_sells = await liquidate_surplus_yes(
                        self.gateway, self.store, plan.surplus_yes, conv_q, burned_market_ids=burned_ids
                    )
                    if surplus_sells:
                        report.sells.extend(surplus_sells)
                    co_tag = " (co-fill 0-fee!)" if co_fills else ""
                    sur_tag = " + surplus sold" if surplus_sells else ""
                    report.action = f"maker filled -> converted{co_tag}{sur_tag}"
                else:
                    report.orphaned = True
                    report.locked_cost = report.spent
                    report.expected_payout = payout_mult * q
                    report.action = "convert failed: NO legs kept"
            else:
                report.settled_shares = q
                report.locked_cost = report.spent
                report.expected_payout = payout_mult * q
                report.action = "maker lock filled -> 0.995 flip" if is_zero_leg else "maker filled -> holding to resolution"
        else:
            filled = [r for r in report.legs if r.ok and r.shares > 0]
            by_idx_p: dict[int, float] = {}
            burned_p: set[str] = set()
            for r in filled:
                if r.leg.neg_risk_index is not None:
                    by_idx_p[r.leg.neg_risk_index] = by_idx_p.get(r.leg.neg_risk_index, 0.0) + r.shares
                    burned_p.add(r.leg.market_id)
            if plan.settlement is Settlement.CONVERT and plan.neg_risk_market_id and len(by_idx_p) >= 2:
                conv_q = floor_shares(min(by_idx_p.values()))
                ok, tx, proceeds = await self.gateway.convert(plan.neg_risk_market_id, index_set(list(by_idx_p)), conv_q)
                if ok:
                    report.convert_tx = tx
                    report.proceeds = proceeds
                    report.settled_shares = conv_q
                    surplus_sells = await liquidate_surplus_yes(
                        self.gateway, self.store, plan.surplus_yes, conv_q, burned_market_ids=burned_p
                    )
                    if surplus_sells:
                        report.sells.extend(surplus_sells)
                    report.action = "partial hedge -> converted subset"
                else:
                    report.orphaned = True
                    report.locked_cost = report.spent
                    report.expected_payout = (len(by_idx_p) - 1) * conv_q
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
        if not report.materially_filled():
            log.info("MISS maker %s -> %s (no inventory, not booked)", opp.title[:48], report.action)
            return
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
        for qkey, quote in list(self.active.items()):
            await self.gateway.cancel(quote.order_id)
            self.active.pop(qkey, None)
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
            timed_out = False
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.cfg.maker.poll_s)
            except asyncio.TimeoutError:
                timed_out = True
            self._wake.clear()
            do_scan = timed_out or (time.time() - self._last_scan_at >= self.cfg.maker.poll_s)
            try:
                await self.step(scan_candidates=do_scan)
            except Exception as exc:
                log.warning("maker arb step error: %s", exc)
            if not timed_out:
                # Coalesce high-frequency WS bursts on quoted tokens into clean 35ms micro-ticks
                await asyncio.sleep(0.035)

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
            "repegged": self.quotes_repegged,
            "preempted": self.quotes_preempted,
            "rebates_est_usd": round(self.rebates_est_usd, 3),
            "quotes": [
                {
                    "strategy": q.plan.strategy,
                    "title": q.plan.title,
                    "lead": f"{q.plan.lead_outcome.value} {q.plan.lead_market.group_title or q.plan.lead_market.question[:35]}",
                    "price": q.plan.lead_price,
                    "shares": q.plan.shares,
                    "profit": round(q.plan.profit, 2),
                    "rebate": round(q.plan.est_rebate_usd, 3),
                    "edge_pct": round(100 * q.plan.edge, 2),
                    "age_s": round(time.time() - q.placed_at, 0),
                }
                for q in self.active.values()
            ],
        }
