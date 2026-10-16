from __future__ import annotations

import asyncio
import logging
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from polyarb.book import OrderBook
from polyarb.config import Config
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.store import BookStore
from polyarb.data.ws import MarketStream
from polyarb.execution.executor import Executor
from polyarb.journal import Journal
from polyarb.models import Event, Opportunity
from polyarb.risk import RiskManager
from polyarb.strategies.base import Strategy, Target
from polyarb.universe import Universe, build_universe

log = logging.getLogger(__name__)


@dataclass
class ScanResult:
    universe: Universe
    store: BookStore
    opportunities: list[tuple[Opportunity, Target]]
    edges: dict[str, list[tuple[float, Target]]]
    timings: dict[str, float]

    def edge_stats(self) -> dict[str, dict]:
        out = {}
        for name, rows in self.edges.items():
            vals = sorted(e for e, _ in rows)
            if not vals:
                continue
            out[name] = {
                "targets_priced": len(vals),
                "edge>0": sum(1 for v in vals if v > 0),
                "edge>-0.5c": sum(1 for v in vals if v > -0.005),
                "edge>-1c": sum(1 for v in vals if v > -0.01),
                "edge>-2c": sum(1 for v in vals if v > -0.02),
                "median": round(statistics.median(vals), 4),
                "best": round(vals[-1], 4),
            }
        return out


async def load_events(cfg: Config, gamma: GammaClient) -> list[Event]:
    return await gamma.fetch_events(min_markets=cfg.universe.min_markets, **cfg.universe.gamma_filters)


async def scan_once(
    cfg: Config,
    strategies: list[Strategy],
    gamma: GammaClient,
    clob: ClobClient,
    events: list[Event] | None = None,
) -> ScanResult:
    t0 = time.time()
    if events is None:
        events = await load_events(cfg, gamma)
    t1 = time.time()
    universe = build_universe(events, strategies, cfg.universe)
    t2 = time.time()
    # A full sweep takes a few seconds; every book in this pass counts as fresh.
    store = BookStore(max_age_s=max(cfg.universe.max_book_age_s, 300.0))
    store.update_many(await clob.get_books(universe.tokens))
    t3 = time.time()
    by_name = {s.name: s for s in strategies}
    opportunities: list[tuple[Opportunity, Target]] = []
    edges: dict[str, list[tuple[float, Target]]] = defaultdict(list)
    now = time.time()
    for target in universe.targets:
        strategy = by_name[target.strategy]
        edge = strategy.top_edge(target, store)
        if edge is not None:
            edges[target.strategy].append((edge, target))
        for opp in strategy.evaluate(target, store, now):
            opportunities.append((opp, target))
    t4 = time.time()
    opportunities.sort(key=lambda pair: -pair[0].profit)
    timings = {"gamma_s": t1 - t0, "universe_s": t2 - t1, "books_s": t3 - t2, "evaluate_s": t4 - t3}
    return ScanResult(universe, store, opportunities, dict(edges), timings)


def _fingerprint(book: OrderBook) -> tuple:
    return (tuple((l.price, l.size) for l in book.bids[:3]), tuple((l.price, l.size) for l in book.asks[:3]))


class Engine:
    """Continuous loop: REST sweeps for the whole universe, websocket for the hot set,
    event-driven evaluation of only the targets whose books changed."""

    def __init__(
        self,
        cfg: Config,
        strategies: list[Strategy],
        gamma: GammaClient,
        clob: ClobClient,
        store: BookStore,
        executor: Executor,
        risk: RiskManager,
        journal: Journal,
    ) -> None:
        self.cfg = cfg
        self.strategies = strategies
        self.by_name = {s.name: s for s in strategies}
        self.gamma = gamma
        self.clob = clob
        self.store = store
        self.executor = executor
        self.risk = risk
        self.journal = journal
        self.universe: Universe | None = None
        self.targets: dict[str, Target] = {}
        self.dirty: set[str] = set()
        self._wake = asyncio.Event()
        self._fp: dict[str, tuple] = {}
        self._last_logged: dict[str, tuple] = {}
        self.stats: Counter = Counter()
        self.stream = (
            MarketStream(store, on_change=self._on_change, assets_per_conn=cfg.engine.ws_assets_per_conn)
            if cfg.engine.use_websocket
            else None
        )

    def _on_change(self, token: str) -> None:
        if self.universe is None:
            return
        for t in self.universe.by_token.get(token, ()):
            self.dirty.add(t.key)
        self._wake.set()

    async def refresh_universe(self) -> None:
        events = await load_events(self.cfg, self.gamma)
        self.universe = build_universe(events, self.strategies, self.cfg.universe)
        self.targets = {t.key: t for t in self.universe.targets}
        info = {"events": len(self.universe.events), "tokens": len(self.universe.tokens), "targets": self.universe.counts()}
        self.journal.write("universe", info)
        log.info("universe: %s", info)

    async def sweep(self) -> int:
        assert self.universe is not None
        books = await self.clob.get_books(self.universe.tokens)
        ws_tokens = set(self.stream.tokens) if self.stream and self.stream.running else set()
        changed = 0
        for token, book in books.items():
            current = self.store.raw(token)
            if token in ws_tokens and current is not None and current.age() < self.store.max_age_s:
                continue
            fp = _fingerprint(book)
            if self._fp.get(token) == fp and current is not None:
                current.received_at = book.received_at
                continue
            self._fp[token] = fp
            self.store.put(book)
            self._on_change(token)
            changed += 1
        self.stats["sweeps"] += 1
        return changed

    def hot_tokens(self) -> list[str]:
        assert self.universe is not None
        ranked = []
        for t in self.universe.targets:
            edge = self.by_name[t.strategy].top_edge(t, self.store)
            if edge is not None:
                ranked.append((edge, t))
        ranked.sort(key=lambda pair: -pair[0])
        tokens: dict[str, None] = {}
        for _, t in ranked[: self.cfg.engine.ws_max_targets]:
            for token in t.tokens:
                tokens[token] = None
        return list(tokens)

    async def _handle(self, opp: Opportunity, target: Target) -> None:
        self.stats["opportunities"] += 1
        sig = (round(opp.profit, 2), round(opp.shares))
        if self._last_logged.get(opp.key) != sig:
            self._last_logged[opp.key] = sig
            self.journal.write("opportunity", opp.summary())
            log.info("OPP %s %s profit=$%.2f edge=%.2f%%", opp.strategy, opp.title[:60], opp.profit, 100 * opp.edge)
        reason = self.risk.check(opp)
        if reason:
            self.stats[f"skip:{reason}"] += 1
            return
        self.risk.begin(opp)
        report = await self.executor.execute(opp, target)
        self.risk.finish(report)
        self.stats["executions"] += 1
        self.journal.write("execution", report.summary())
        log.info(
            "EXEC %s %s -> %s pnl=$%.3f locked=$%.2f",
            report.mode, opp.title[:50], report.action, report.realized_pnl, report.locked_cost,
        )

    async def _eval_loop(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            keys, self.dirty = self.dirty, set()
            now = time.time()
            for key in keys:
                target = self.targets.get(key)
                if target is None:
                    continue
                self.stats["evaluations"] += 1
                try:
                    for opp in self.by_name[target.strategy].evaluate(target, self.store, now):
                        await self._handle(opp, target)
                except Exception:
                    self.stats["eval_errors"] += 1
                    log.exception("evaluation failed for %s", key)
            await asyncio.sleep(0)

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.sweep()
            except Exception as exc:
                log.warning("sweep failed: %s", exc)
            await asyncio.sleep(self.cfg.engine.rest_sweep_s)

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.universe.refresh_minutes * 60)
            try:
                await self.refresh_universe()
                await self.sweep()
                if self.stream:
                    await self.stream.start(self.hot_tokens())
            except Exception as exc:
                log.warning("universe refresh failed: %s", exc)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.engine.report_every_s)
            matured = self.risk.release_matured()
            for h in matured:
                self.journal.write("hold_released", {"key": h.key, "pnl": h.payout - h.cost})
            snapshot = {
                **self.store.stats(),
                "ws_msgs": self.stream.messages if self.stream else 0,
                "pnl_total": round(self.risk.state.realized_total, 4),
                "holds": len(self.risk.state.holds),
                "hold_exposure": round(self.risk.hold_exposure, 2),
                **{k: v for k, v in self.stats.items()},
            }
            self.journal.write("heartbeat", snapshot)
            log.info("heartbeat %s", snapshot)

    async def run(self, duration_s: float | None = None) -> None:
        geo = await self.risk.check_geo()
        log.info("geoblock check: %s", geo)
        await self.refresh_universe()
        await self.sweep()
        if self.stream:
            await self.stream.start(self.hot_tokens())
        tasks = [
            asyncio.create_task(self._sweep_loop()),
            asyncio.create_task(self._eval_loop()),
            asyncio.create_task(self._refresh_loop()),
            asyncio.create_task(self._report_loop()),
        ]
        self._wake.set()
        try:
            if duration_s:
                done, _ = await asyncio.wait(tasks, timeout=duration_s, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    if t.exception():
                        log.error("engine task crashed: %r", t.exception())
            else:
                await asyncio.gather(*tasks)
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.stream:
                await self.stream.stop()
            self.risk.save()
