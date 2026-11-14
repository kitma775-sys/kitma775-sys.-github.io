from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, replace
from typing import Callable, Mapping

from polyarb.book import take
from polyarb.config import ExecutionConfig
from polyarb.data.clob import ClobClient
from polyarb.data.store import BookStore
from polyarb.execution.base import ExecutionReport, Gateway, SellResult, retain_unsold_shares
from polyarb.execution.settlement import index_set
from polyarb.models import FeeSchedule, Leg, Opportunity, Outcome, Settlement
from polyarb.sizing import floor_shares
from polyarb.strategies.base import Strategy, Target

log = logging.getLogger(__name__)


async def liquidate_surplus_yes(
    gateway: Gateway,
    store: BookStore,
    specs: list[dict] | tuple[dict, ...],
    shares: float,
    burned_market_ids: set[str] | None = None,
    min_net_usd: float = 0.05,
) -> list[SellResult]:
    """Sweep free YES shares minted on ``m not in S`` during ``convertPositions`` into resting bids."""
    if not specs or shares <= 0:
        return []
    burned = burned_market_ids or set()
    sells: list[SellResult] = []
    for spec in specs:
        mid = str(spec.get("market_id") or "")
        if mid and mid in burned:
            continue
        yes_token = str(spec.get("yes_token") or "")
        min_sz = float(spec.get("min_size") or 5.0)
        tick = float(spec.get("tick_size") or 0.01)
        if not yes_token or shares < min_sz - 1e-6:
            continue
        book = store.raw(yes_token)
        if book is None or not book.bids:
            continue
        fee = FeeSchedule(
            rate=float(spec.get("fee_rate") or 0.0),
            exponent=float(spec.get("fee_exponent") or 1.0),
        )
        est = take(book.bids, shares, fee)
        if est.shares <= 0.05 or (est.notional - est.fee) < min_net_usd:
            continue
        res = await gateway.sell(yes_token, Outcome.YES, yes_token, shares, tick, fee, tick)
        if res.ok and res.shares > 0:
            sells.append(res)
    return sells


@dataclass
class _Held:
    leg: Leg
    shares: float
    cost: float


class Executor:
    """Turns an Opportunity into fills, then into collateral (convert) or a tracked hold.

    Leg risk is the only real risk of these strategies, so a partial fill is
    resolved by picking the best of: complete the missing legs at fresh prices,
    convert the filled NegRisk subset, or sell the filled legs back.
    """

    def __init__(
        self,
        gateway: Gateway,
        store: BookStore,
        strategies: Mapping[str, Strategy],
        cfg: ExecutionConfig,
        clob: ClobClient | None = None,
    ) -> None:
        self.gateway = gateway
        self.store = store
        self.strategies = strategies
        self.cfg = cfg
        self.clob = clob
        self._paper_used: dict[tuple[str, Outcome], tuple] = {}

    async def execute(
        self,
        opp: Opportunity,
        target: Target | None = None,
        approve: Callable[[Opportunity], str | None] | None = None,
        max_cost: float | None = None,
    ) -> ExecutionReport:
        """``approve`` re-checks limits on the final order, which a recheck may have re-sized."""
        report = ExecutionReport(opp=opp, mode=self.gateway.mode, started_at=time.time())
        try:
            if self._should_recheck(opp, target):
                fresh = await self._recheck(opp, target, max_cost=max_cost)
                if fresh is None:
                    report.action = "abort: vanished on recheck"
                    return self._done(report)
                report.opp = opp = fresh
            reason = approve(opp) if approve else None
            if reason is None and self.gateway.mode == "paper":
                reason = self._paper_reuse(opp)
            if reason:
                report.action = f"abort: {reason}"
                return self._done(report)
            report.sent = True
            report.legs = await self.gateway.buy(opp.legs, self.cfg.slippage_ticks)
            report.complete = all(r.ok for r in report.legs)
            subset = False
            if not report.complete:
                subset = await self._repair(report)
            if report.complete or subset:
                if opp.settlement is Settlement.CONVERT:
                    await self._convert(report)
                elif report.complete and opp.meta.get("mode") == "micro_merge":
                    await self._merge_binary(report)
                elif report.complete and opp.meta.get("fast_flip") and len(opp.legs) == 1:
                    if not await self._try_instant_flip(report):
                        self._lock_hold(report)
                else:
                    self._lock_hold(report)
        except Exception as exc:
            log.exception("execution failed for %s", opp.key)
            report.error = str(exc)
        return self._done(report)

    def _should_recheck(self, opp: Opportunity, target: Target | None) -> bool:
        if not (self.cfg.recheck_books and target is not None and self.clob is not None):
            return False
        now = time.time()
        books = [self.store.raw(t) for t in target.tokens]
        # 5m/15m books reprice inside a second. A two-second-old "fresh" WS book is what made
        # paper fill and the live FOK come back unmatched.
        if opp.strategy in ("crypto_short", "micro_arb"):
            if books and all(b is not None and b.age(now) <= 0.35 for b in books):
                return False
            return True
        if opp.meta.get("fast_turnover") and self.cfg.fast_ws_skip_recheck_s > 0:
            if books and all(b is not None and b.age(now) <= self.cfg.fast_ws_skip_recheck_s for b in books):
                return False
        return True

    def _done(self, report: ExecutionReport) -> ExecutionReport:
        report.finished_at = time.time()
        sells = sum(s.net for s in report.sells if s.ok)
        if report.locked_cost > 0:
            report.realized_pnl = report.proceeds + sells - (report.spent - report.locked_cost)
        else:
            report.realized_pnl = report.proceeds + sells - report.spent
        return report

    def _paper_reuse(self, opp: Opportunity) -> str | None:
        """Paper fills never leave the real book, so a book side a paper fill took from must change before reuse."""
        sides = {}
        for leg in opp.legs:
            book = self.store.raw(leg.yes_token)
            if book is not None:
                levels = book.asks if leg.outcome is Outcome.YES else book.bids
                sides[(leg.yes_token, leg.outcome)] = tuple((lvl.price, lvl.size) for lvl in levels[:3])
        if any(self._paper_used.get(side) == top for side, top in sides.items()):
            return "paper liquidity already used"
        self._paper_used.update(sides)
        return None

    async def _recheck(self, opp: Opportunity, target: Target, max_cost: float | None = None) -> Opportunity | None:
        assert self.clob is not None
        self.store.update_many(await self.clob.get_books(target.tokens, priority=opp.strategy in ("crypto_short", "micro_arb")))
        strategy = self.strategies[opp.strategy]
        for candidate in strategy.evaluate(target, self.store, time.time(), max_cost=max_cost):
            if candidate.key == opp.key:
                return candidate
        return None

    def _held(self, report: ExecutionReport) -> dict[str, _Held]:
        held: dict[str, _Held] = {}
        for r in report.legs + report.retries:
            if r.ok and r.shares > 0:
                h = held.get(r.leg.token_id)
                if h:
                    h.shares += r.shares
                    h.cost += r.cost
                else:
                    held[r.leg.token_id] = _Held(r.leg, r.shares, r.cost)
        return held

    def _sell_value(self, leg: Leg, shares: float) -> float:
        book = self.store.raw(leg.yes_token)
        if book is None:
            return 0.0
        levels = book.bids if leg.outcome is Outcome.YES else book.no_bids()
        fill = take(levels, shares, leg.fee_schedule)
        return fill.notional - fill.fee

    async def _repair(self, report: ExecutionReport) -> bool:
        """Handle a partial fill. Returns True when the filled NegRisk subset should be converted."""
        opp = report.opp
        held = self._held(report)
        if not held:
            report.action = "no fills"
            return False
        missing = [r.leg for r in report.legs if not r.ok]
        if self.clob is not None:
            self.store.update_many(await self.clob.get_books({leg.yes_token for leg in missing} | {h.leg.yes_token for h in held.values()}))
        q = floor_shares(min(h.shares for h in held.values()))
        filled_cost = sum(h.cost for h in held.values())
        per_unit = opp.payout_floor / opp.shares if opp.shares else 0.0

        retry_legs: list[Leg] | None = []
        retry_cost = 0.0
        for leg in missing:
            book = self.store.raw(leg.yes_token)
            levels = (book.asks if leg.outcome is Outcome.YES else book.no_asks()) if book else []
            fill = take(levels, q, leg.fee_schedule)
            if fill.shares < q - 1e-6 or fill.notional < 1.0:
                retry_legs = None
                break
            # The retry order is allowed one extra tick. Price that in before deciding to complete,
            # or a basket that looks flat on the touch locks in a loss once the order is sent.
            slip_pad = q * (self.cfg.slippage_ticks + 1) * leg.tick_size
            retry_cost += fill.notional + fill.fee + slip_pad
            retry_legs.append(replace(leg, shares=q, notional=fill.notional, fee=fill.fee, limit_price=fill.worst_price))
        complete_value = per_unit * q - filled_cost - retry_cost if retry_legs is not None else -math.inf
        # Never buy the missing leg once the pair costs more than its guaranteed payout.
        # The BTC 1h retry did this: YES filled at 21¢, DOWN was then lifted at 82¢, and the hold pays $1 against $1.03.
        if complete_value < 0.0:
            retry_legs = None
            complete_value = -math.inf

        unwind_value = sum(self._sell_value(h.leg, h.shares) for h in held.values()) - filled_cost

        subset_value = -math.inf
        if opp.settlement is Settlement.CONVERT and len(held) >= 2:
            leftovers = sum(self._sell_value(h.leg, h.shares - q) for h in held.values())
            subset_value = (len(held) - 1) * q + leftovers - filled_cost

        best = max(complete_value, unwind_value, subset_value)
        log.warning(
            "partial fill on %s: complete=%.3f subset=%.3f unwind=%.3f", opp.key, complete_value, subset_value, unwind_value
        )
        if best == complete_value and retry_legs:
            report.retries = await self.gateway.buy(retry_legs, self.cfg.slippage_ticks + 1)
            if all(r.ok for r in report.retries):
                report.complete = True
                report.action = "completed after retry"
                return False
            held = self._held(report)
            if opp.settlement is Settlement.CONVERT and len(held) >= 2:
                report.action = "retry incomplete -> convert subset"
                return True
        elif best == subset_value:
            report.action = "convert filled subset"
            return True
        held = self._held(report)
        cost = sum(h.cost for h in held.values())
        value = sum(self._sell_value(h.leg, h.shares) for h in held.values())
        # A naked 1h/5m pair leg is a directional bet that can go to zero. Selling it back
        # loses the spread; keeping it can lose the whole premium. Always sell it back.
        naked_pair = opp.meta.get("mode") in ("anchor_1h", "sync_5m_15m", "micro_corridor", "micro_merge")
        if not naked_pair and cost - value > self.cfg.max_unwind_loss_usd:
            # Dumping into a thin book would crystallise a large loss; keep the legs for manual review.
            report.orphaned = True
            report.locked_cost = cost
            report.expected_payout = value
            report.action = f"legs kept: unwind would lose ${cost - value:.2f}"
            return False
        report.action = report.action or "unwind"
        await self._unwind(report, held)
        return False

    async def _unwind(self, report: ExecutionReport, held: dict[str, _Held]) -> None:
        for h in held.values():
            book = self.store.raw(h.leg.yes_token)
            levels = (book.bids if h.leg.outcome is Outcome.YES else book.no_bids()) if book else []
            if not levels:
                continue
            fill = take(levels, h.shares, h.leg.fee_schedule)
            floor = max(fill.worst_price - h.leg.tick_size, h.leg.tick_size)
            report.sells.append(
                await self.gateway.sell(
                    h.leg.token_id, h.leg.outcome, h.leg.yes_token, h.shares, floor, h.leg.fee_schedule, h.leg.tick_size
                )
            )
        report.action = report.action if report.action.startswith("unwind") else f"unwind ({report.action})"
        retain_unsold_shares(report)

    async def _convert(self, report: ExecutionReport) -> None:
        opp = report.opp
        held = self._held(report)
        market_id = opp.meta.get("neg_risk_market_id")
        indices = [h.leg.neg_risk_index for h in held.values()]
        if not market_id or any(i is None for i in indices) or len(held) < 2:
            report.action = "convert impossible -> unwind"
            await self._unwind(report, held)
            return
        q = floor_shares(min(h.shares for h in held.values()))
        ok, tx, proceeds = await self.gateway.convert(market_id, index_set(indices), q)
        if not ok:
            report.orphaned = True
            report.locked_cost = sum(h.cost for h in held.values())
            report.expected_payout = (len(held) - 1) * q + sum(self._sell_value(h.leg, h.shares - q) for h in held.values())
            report.error = "convert transaction failed; NO positions retained"
            report.action = "convert failed: NO legs kept"
            return
        report.convert_tx = tx
        report.proceeds += proceeds
        report.settled_shares = q
        burned_ids = {h.leg.market_id for h in held.values()}
        surplus_sells = await liquidate_surplus_yes(
            self.gateway, self.store, opp.meta.get("surplus_yes") or [], q, burned_market_ids=burned_ids
        )
        if surplus_sells:
            report.sells.extend(surplus_sells)
        report.action = report.action or ("converted + surplus sold" if surplus_sells else "converted")
        for h in held.values():
            rest = h.shares - q
            if rest >= h.leg.min_size and self._sell_value(h.leg, rest) >= 1.0:
                book = self.store.raw(h.leg.yes_token)
                levels = (book.bids if h.leg.outcome is Outcome.YES else book.no_bids()) if book else []
                if levels:
                    report.sells.append(
                        await self.gateway.sell(
                            h.leg.token_id, h.leg.outcome, h.leg.yes_token, rest, max(levels[0].price - h.leg.tick_size, h.leg.tick_size),
                            h.leg.fee_schedule, h.leg.tick_size,
                        )
                    )

    async def _merge_binary(self, report: ExecutionReport) -> None:
        """Turn a filled UP+DOWN pair into pUSD. A failed merge keeps both legs, which still pay $1."""
        opp = report.opp
        held = [r for r in report.legs + report.retries if r.ok and r.shares > 0]
        q = floor_shares(min((r.shares for r in held), default=0.0))
        cid = next((str(c) for c in (opp.meta.get("condition_ids") or []) if c), "")
        merge = getattr(self.gateway, "merge", None)
        ok, tx = (False, None)
        if merge is not None and cid and q >= 1.0:
            ok, tx = await merge(cid, q)
        if ok:
            report.convert_tx = tx
            report.proceeds = float(q)
            report.settled_shares = float(q)
            report.action = "merged both legs to pUSD"
            return
        self._lock_hold(report)
        report.action = "merge failed -> holding both legs"

    async def _try_instant_flip(self, report: ExecutionReport) -> bool:
        """Sell immediately into 0.99+ bonding bids when doing so already locks positive profit."""
        opp = report.opp
        held = self._held(report)
        if len(held) != 1:
            return False
        h = next(iter(held.values()))
        min_bid = float(opp.meta.get("flip_min_bid") or 0.99)
        min_profit = float(opp.meta.get("flip_min_profit_usd") or 0.08)
        book = self.store.raw(h.leg.yes_token)
        levels = (book.bids if h.leg.outcome is Outcome.YES else book.no_bids()) if book else []
        if not levels or levels[0].price < min_bid - 1e-9:
            return False
        good = [lvl for lvl in levels if lvl.price >= min_bid - 1e-9]
        fill = take(good, h.shares, h.leg.fee_schedule)
        if fill.shares < h.shares - 0.05 or (fill.notional - fill.fee) < h.cost + min_profit:
            return False
        sell = await self.gateway.sell(
            h.leg.token_id, h.leg.outcome, h.leg.yes_token, h.shares, min_bid, h.leg.fee_schedule, h.leg.tick_size
        )
        if not sell.ok or sell.shares <= 0:
            return False
        report.sells.append(sell)
        rem = h.shares - sell.shares
        if rem < 0.05:
            report.settled_shares = sell.shares
            report.locked_cost = 0.0
            report.expected_payout = 0.0
            report.action = "instant flip"
            return True
        frac = rem / h.shares
        report.settled_shares = floor_shares(rem)
        report.locked_cost = h.cost * frac
        report.expected_payout = (opp.payout_floor / opp.shares if opp.shares else 1.0) * rem
        report.action = "partial flip -> holding remainder"
        for r in report.legs + report.retries:
            if r.ok and r.leg.token_id == h.leg.token_id:
                r.shares = max(0.0, r.shares - sell.shares)
                break
        return True

    def _lock_hold(self, report: ExecutionReport) -> None:
        opp = report.opp
        held = self._held(report)
        q = floor_shares(min(h.shares for h in held.values()))
        per_unit = opp.payout_floor / opp.shares if opp.shares else 0.0
        report.settled_shares = q
        report.locked_cost = sum(h.cost for h in held.values())
        report.expected_payout = per_unit * q
        report.action = report.action or "holding to resolution"
