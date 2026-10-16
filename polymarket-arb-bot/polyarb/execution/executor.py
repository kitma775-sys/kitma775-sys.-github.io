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
from polyarb.execution.base import ExecutionReport, Gateway
from polyarb.execution.settlement import index_set
from polyarb.models import Leg, Opportunity, Outcome, Settlement
from polyarb.sizing import floor_shares
from polyarb.strategies.base import Strategy, Target

log = logging.getLogger(__name__)


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
        self._paper_fp: dict[str, tuple] = {}

    async def execute(
        self,
        opp: Opportunity,
        target: Target | None = None,
        approve: Callable[[Opportunity], str | None] | None = None,
    ) -> ExecutionReport:
        """``approve`` re-checks limits on the final order, which a recheck may have re-sized."""
        report = ExecutionReport(opp=opp, mode=self.gateway.mode, started_at=time.time())
        try:
            if self.cfg.recheck_books and target is not None and self.clob is not None:
                fresh = await self._recheck(opp, target)
                if fresh is None:
                    report.action = "abort: vanished on recheck"
                    return self._done(report)
                report.opp = opp = fresh
            reason = approve(opp) if approve else None
            if reason is None and self.gateway.mode == "paper" and target is not None:
                reason = self._paper_reuse(opp, target)
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
                else:
                    self._lock_hold(report)
        except Exception as exc:
            log.exception("execution failed for %s", opp.key)
            report.error = str(exc)
        return self._done(report)

    def _done(self, report: ExecutionReport) -> ExecutionReport:
        report.finished_at = time.time()
        sells = sum(s.net for s in report.sells if s.ok)
        if report.locked_cost > 0:
            report.realized_pnl = report.proceeds + sells - (report.spent - report.locked_cost)
        else:
            report.realized_pnl = report.proceeds + sells - report.spent
        return report

    def _paper_reuse(self, opp: Opportunity, target: Target) -> str | None:
        """Paper fills never leave the real book, so unchanged liquidity must not be traded twice."""
        fp = tuple(b.fingerprint() if (b := self.store.raw(t)) is not None else None for t in target.tokens)
        if self._paper_fp.get(opp.key) == fp:
            return "paper liquidity already used"
        self._paper_fp[opp.key] = fp
        return None

    async def _recheck(self, opp: Opportunity, target: Target) -> Opportunity | None:
        assert self.clob is not None
        self.store.update_many(await self.clob.get_books(target.tokens))
        strategy = self.strategies[opp.strategy]
        for candidate in strategy.evaluate(target, self.store, time.time()):
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
            retry_cost += fill.notional + fill.fee
            retry_legs.append(replace(leg, shares=q, notional=fill.notional, fee=fill.fee, limit_price=fill.worst_price))
        complete_value = per_unit * q - filled_cost - retry_cost if retry_legs is not None else -math.inf

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
        if cost - value > self.cfg.max_unwind_loss_usd:
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
            report.error = "convert transaction failed; NO positions retained"
            report.action = "convert failed"
            return
        report.convert_tx = tx
        report.proceeds += proceeds
        report.settled_shares = q
        report.action = report.action or "converted"
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

    def _lock_hold(self, report: ExecutionReport) -> None:
        opp = report.opp
        held = self._held(report)
        q = floor_shares(min(h.shares for h in held.values()))
        per_unit = opp.payout_floor / opp.shares if opp.shares else 0.0
        report.settled_shares = q
        report.locked_cost = sum(h.cost for h in held.values())
        report.expected_payout = per_unit * q
        report.action = report.action or "holding to resolution"
