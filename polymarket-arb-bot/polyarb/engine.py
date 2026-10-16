from __future__ import annotations

import asyncio
import gc
import logging
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass

from polyarb.config import Config
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.store import BookStore
from polyarb.data.ws import MarketStream
from polyarb.execution.executor import Executor
from polyarb.journal import Journal
from polyarb.models import Event, Opportunity
from polyarb.risk import RiskManager, api_allowed
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


def settle_heap() -> None:
    """Collect once, then park the long-lived universe and books outside the cyclic GC.

    Full collections over the ~35k books and the market universe cost several
    percent of a core; replaced books are still freed by reference counting.
    """
    gc.unfreeze()
    gc.collect()
    gc.freeze()


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
        self._last_logged: dict[str, tuple] = {}
        self._inflight: set[asyncio.Task] = set()
        self.stats: Counter = Counter()
        self.phase = "idle"
        self.stopping = False
        self.started_at = time.time()
        self.last_sweep_s = 0.0
        self.last_sweep_at = 0.0
        self.last_refresh_at = 0.0
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
        self.last_refresh_at = time.time()
        info = {"events": len(self.universe.events), "tokens": len(self.universe.tokens), "targets": self.universe.counts()}
        self.journal.write("universe", info)
        log.info("universe: %s", info)

    async def sweep(self) -> int:
        """Refresh every universe book over REST, parsing only books whose server hash moved."""
        assert self.universe is not None
        started = time.time()
        ws_tokens = set(self.stream.tokens) if self.stream and self.stream.running else set()
        max_age = self.store.max_age_s

        def skip(token: str, server_hash: str | None) -> bool:
            current = self.store.raw(token)
            if current is None:
                return False
            if token in ws_tokens and current.age() < max_age:
                return True
            if server_hash is not None and current.hash == server_hash:
                current.received_at = time.time()
                return True
            return False

        books = await self.clob.get_books(self.universe.tokens, skip=skip)
        changed = 0
        for token, book in books.items():
            current = self.store.raw(token)
            if token in ws_tokens and current is not None and current.age() < max_age:
                continue
            self.store.put(book)
            if current is None or current.fingerprint() != book.fingerprint():
                self._on_change(token)
                changed += 1
        self.stats["sweeps"] += 1
        self.stats["books_parsed"] += len(books)
        self.last_sweep_at = time.time()
        self.last_sweep_s = self.last_sweep_at - started
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
        if self.stopping:
            self.stats["skip:shutting down"] += 1
            return
        reason = self.risk.check(opp)
        if reason:
            self.stats[f"skip:{reason}"] += 1
            return
        self.risk.begin(opp)
        task = asyncio.create_task(self._execute(opp, target))
        self._inflight.add(task)
        task.add_done_callback(self._inflight.discard)
        # Stopping the engine must never cut an execution between its orders and its convert / unwind.
        await asyncio.shield(task)

    async def _execute(self, opp: Opportunity, target: Target) -> None:
        report = await self.executor.execute(opp, target, approve=self.risk.check_size)
        self.risk.finish(report)
        if not report.sent:
            self.stats[report.action] += 1
            log.debug("not sent %s: %s", opp.key, report.action)
            return
        self.stats["executions"] += 1
        self.journal.write("execution", report.summary())
        log.info(
            "EXEC %s %s -> %s pnl=$%.3f locked=$%.2f",
            report.mode, opp.title[:50], report.action, report.realized_pnl, report.locked_cost,
        )

    async def _eval_loop(self) -> None:
        settle = self.cfg.engine.eval_interval_ms / 1000.0
        while True:
            await self._wake.wait()
            if settle > 0:
                # Lets a burst of websocket deltas for one target collapse into a single evaluation.
                await asyncio.sleep(settle)
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
                settle_heap()
                if self.stream:
                    await self.stream.start(self.hot_tokens())
            except Exception as exc:
                log.warning("universe refresh failed: %s", exc)

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.engine.report_every_s)
            matured = self.risk.release_matured()
            for h in matured:
                self.journal.write("hold_released", {"key": h.key, "title": h.title, "strategy": h.strategy, "pnl": h.payout - h.cost})
            snapshot = {
                **self.store.stats(),
                "ws_msgs": self.stream.messages if self.stream else 0,
                "pnl_total": round(self.risk.state.realized_total, 4),
                "pnl_today": round(self.risk.state.realized_today, 4),
                "locked": round(sum(h.payout - h.cost for h in self.risk.state.holds), 4),
                "holds": len(self.risk.state.holds),
                "hold_exposure": round(self.risk.hold_exposure, 2),
                **{k: v for k, v in self.stats.items()},
            }
            self.journal.write("heartbeat", snapshot)
            log.info("heartbeat %s", snapshot)

    async def drain(self, timeout: float = 60.0) -> None:
        """Refuse new executions and wait for the ones already sending orders."""
        self.stopping = True
        pending = set(self._inflight)
        if not pending:
            return
        log.warning("waiting for %d in-flight execution(s) before stopping", len(pending))
        _, still = await asyncio.wait(pending, timeout=timeout)
        if still:
            log.error("%d execution(s) still running after %.0fs; check positions", len(still), timeout)

    async def run(self, duration_s: float | None = None) -> None:
        tasks: list[asyncio.Task] = []
        try:
            self.phase = "geo"
            geo = await self.risk.check_geo()
            log.info("geoblock check: %s (API orders %s)", geo, "allowed" if api_allowed(geo) else "not allowed")
            gc.set_threshold(50_000, 20, 100)
            self.phase = "loading"
            await self.refresh_universe()
            self.phase = "sweeping"
            await self.sweep()
            settle_heap()
            if self.stream:
                await self.stream.start(self.hot_tokens())
            tasks = [
                asyncio.create_task(self._sweep_loop()),
                asyncio.create_task(self._eval_loop()),
                asyncio.create_task(self._refresh_loop()),
                asyncio.create_task(self._report_loop()),
            ]
            self.phase = "running"
            self._wake.set()
            if duration_s:
                done, _ = await asyncio.wait(tasks, timeout=duration_s, return_when=asyncio.FIRST_EXCEPTION)
                for t in done:
                    if t.exception():
                        log.error("engine task crashed: %r", t.exception())
            else:
                await asyncio.gather(*tasks)
        finally:
            self.phase = "stopping"
            self.stopping = True
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.drain()
            if self.stream:
                await self.stream.stop()
            self.risk.save()
            self.phase = "stopped"
