from __future__ import annotations

import asyncio
import gc
import logging
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
import inspect
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from polyarb.config import Config
from polyarb.data.binance import BinanceFeed
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.metar import MetarFeed
from polyarb.data.rtds import ChainlinkRtdsFeed, fetch_short_crypto_events
from polyarb.data.store import BookStore
from polyarb.data.ws import MarketStream
from polyarb.execution.executor import Executor
from polyarb.journal import Journal
from polyarb.models import Event, Opportunity, Settlement
from polyarb.risk import RiskManager, api_allowed
from polyarb.strategies.base import Strategy, Target
from polyarb.universe import Universe, build_universe

if TYPE_CHECKING:
    from polyarb.execution.lifecycle import LifecycleManager
    from polyarb.execution.maker_arb import MakerArbManager

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


async def load_events(cfg: Config, gamma: GammaClient, rtds_feed: ChainlinkRtdsFeed | None = None) -> list[Event]:
    base = await gamma.fetch_events(min_markets=cfg.universe.min_markets, **cfg.universe.gamma_filters)
    if cfg.crypto_short.enabled:
        short_evs = await fetch_short_crypto_events(gamma._http, rtds_feed=rtds_feed, base_url=gamma._base)
        by_id = {e.id: e for e in base}
        for e in short_evs:
            by_id[e.id] = e
        return list(by_id.values())
    return base


async def scan_once(
    cfg: Config,
    strategies: list[Strategy],
    gamma: GammaClient,
    clob: ClobClient,
    events: list[Event] | None = None,
) -> ScanResult:
    t0 = time.time()
    rtds_in_strats = next((getattr(s, "rtds_feed", None) for s in strategies if getattr(s, "rtds_feed", None) is not None), None)
    if events is None:
        events = await load_events(cfg, gamma, rtds_feed=rtds_in_strats)
    t1 = time.time()
    universe = build_universe(events, strategies, cfg.universe)
    t2 = time.time()
    for strat in strategies:
        feed = getattr(strat, "feed", None)
        if isinstance(feed, MetarFeed):
            feed.set_stations(t.event.metar_icao for t in universe.targets if t.strategy == "metar_lock" and t.event.metar_icao)
            await feed.poll_once(hours=36)
        elif isinstance(feed, BinanceFeed):
            feed.set_symbols(
                t.payload.symbol for t in universe.targets if t.strategy == "binance_lock" and hasattr(t.payload, "symbol")
            )
            await feed.poll_once(bootstrap=True)
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
        *,
        all_strategies: list[Strategy] | None = None,
        initial_events: list[Event] | None = None,
        metar_feed: MetarFeed | None = None,
        binance_feed: BinanceFeed | None = None,
        rtds_feed: ChainlinkRtdsFeed | None = None,
        maker_arb: "MakerArbManager | None" = None,
        lifecycle: "LifecycleManager | None" = None,
    ) -> None:
        self.cfg = cfg
        self.all_strategies = list(all_strategies) if all_strategies is not None else list(strategies)
        self.strategies = strategies
        self.by_name = {s.name: s for s in self.all_strategies}
        self.gamma = gamma
        self.clob = clob
        self.store = store
        self.executor = executor
        self.risk = risk
        self.journal = journal
        self.metar_feed = metar_feed
        self.binance_feed = binance_feed
        self.rtds_feed = rtds_feed
        self.maker_arb = maker_arb
        if self.maker_arb is not None:
            self.maker_arb.set_strategies(self.by_name)
        self.lifecycle = lifecycle
        self._initial_events = initial_events
        self._raw_events: list[Event] | None = None
        self._short_events: dict[str, Event] = {}
        if self.metar_feed is not None:
            self.metar_feed.on_station_update = self._on_metar_station
        if self.binance_feed is not None:
            self.binance_feed.on_symbol_update = self._on_binance_symbol
        if self.rtds_feed is not None:
            self.rtds_feed.on_asset_tick = self._on_rtds_asset
        self.universe: Universe | None = None
        self.targets: dict[str, Target] = {}
        self._by_station: dict[str, list[str]] = defaultdict(list)
        self._by_symbol: dict[str, list[str]] = defaultdict(list)
        self._by_short_asset: dict[str, list[str]] = defaultdict(list)
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
        trade_hook = getattr(getattr(executor, "gateway", None), "on_market_trade", None)
        self.stream = (
            MarketStream(store, on_change=self._on_change, on_trade=trade_hook, assets_per_conn=cfg.engine.ws_assets_per_conn)
            if cfg.engine.use_websocket
            else None
        )

    def _on_change(self, token: str) -> None:
        if self.universe is None:
            return
        for t in self.universe.by_token.get(token, ()):
            self.dirty.add(t.key)
        if self.maker_arb is not None:
            self.maker_arb.notify_token(token)
        self._wake.set()

    def _on_metar_station(self, icao: str) -> None:
        keys = self._by_station.get(icao.upper())
        if keys:
            self.dirty.update(keys)
            self._wake.set()
            if self.maker_arb is not None:
                self.maker_arb._wake.set()

    def _on_binance_symbol(self, symbol: str) -> None:
        keys = self._by_symbol.get(symbol.upper())
        if keys:
            self.dirty.update(keys)
            self._wake.set()
            if self.maker_arb is not None:
                self.maker_arb._wake.set()

    def _on_rtds_asset(self, asset: str) -> None:
        keys = self._by_short_asset.get(asset.lower())
        if keys:
            self.dirty.update(keys)
            self._wake.set()
            if self.maker_arb is not None:
                self.maker_arb._wake.set()

    async def refresh_universe(self, events: list[Event] | None = None) -> None:
        if events is None:
            if self._initial_events is not None:
                events, self._initial_events = self._initial_events, None
            else:
                events = await load_events(self.cfg, self.gamma, rtds_feed=self.rtds_feed)
        self._raw_events = events
        merged = self._merged_events(events)
        self._apply_universe(merged)
        self.last_refresh_at = time.time()
        info = {"events": len(self.universe.events), "tokens": len(self.universe.tokens), "targets": self.universe.counts()}
        self.journal.write("universe", info)
        log.info("universe: %s", info)

    def _merged_events(self, base_events: list[Event]) -> list[Event]:
        if not self._short_events:
            return base_events
        now_s = time.time()
        # Prune expired ephemeral short-crypto events (> 180s past end_date)
        for eid, sev in list(self._short_events.items()):
            if sev.end_date and (now_s - sev.end_date.timestamp()) > 180.0:
                del self._short_events[eid]
        by_id = {ev.id: ev for ev in base_events}
        for eid, sev in self._short_events.items():
            by_id[eid] = sev
        return list(by_id.values())

    def _apply_universe(self, events: list[Event]) -> None:
        self.universe = build_universe(events, self.strategies, self.cfg.universe)
        self.targets = {t.key: t for t in self.universe.targets}
        self.store.prune(set(self.universe.tokens))
        if self.executor is not None and hasattr(self.executor.gateway, "warm_markets"):
            self.executor.gateway.warm_markets(m for ev in self.universe.events for m in ev.tradeable_markets)
        by_st: dict[str, list[str]] = defaultdict(list)
        by_sym: dict[str, list[str]] = defaultdict(list)
        by_sa: dict[str, list[str]] = defaultdict(list)
        for t in self.universe.targets:
            if t.strategy == "metar_lock" and t.event.metar_icao:
                by_st[t.event.metar_icao.upper()].append(t.key)
            elif t.strategy == "binance_lock" and hasattr(t.payload, "symbol"):
                by_sym[str(t.payload.symbol).upper()].append(t.key)
            elif t.strategy == "crypto_short" and hasattr(t.payload, "asset"):
                a_code = str(t.payload.asset).lower()
                by_sa[a_code].append(t.key)
                by_sym[f"{a_code.upper()}USDT"].append(t.key)
        self._by_station = by_st
        self._by_symbol = by_sym
        self._by_short_asset = by_sa
        if self.metar_feed is not None:
            self.metar_feed.set_stations(by_st.keys())
        if self.binance_feed is not None:
            self.binance_feed.set_symbols(by_sym.keys())
        if self.maker_arb is not None:
            self.maker_arb.set_strategies(self.by_name)
            self.maker_arb.set_targets(self.universe.targets)

    async def apply_config_update(self) -> None:
        """Hot-reload strategy caps, enable/disable flags, book TTL, METAR polling, and maker quotes in place."""
        self.store.max_age_s = self.cfg.universe.max_book_age_s
        prev_enabled = {s.name for s in self.strategies}
        for s in self.all_strategies:
            if hasattr(s, "cfg") and hasattr(s.cfg, "enabled"):
                s.enabled = bool(s.cfg.enabled)
            if hasattr(s, "max_trade_usd"):
                s.max_trade_usd = self.cfg.risk.max_trade_usd
            if s.name in ("binance_lock", "crypto_short"):
                s.flip_ask_price = self.cfg.metar.flip_ask_price
                s.flip_min_instant_bid = self.cfg.metar.flip_min_instant_bid
                s.flip_min_profit_usd = (
                    min(0.05, self.cfg.metar.flip_min_profit_usd)
                    if s.name == "crypto_short"
                    else self.cfg.metar.flip_min_profit_usd
                )
        self.strategies = [s for s in self.all_strategies if s.enabled]
        self.by_name = {s.name: s for s in self.all_strategies}
        if self.executor is not None:
            self.executor.strategies = self.by_name
        if self.metar_feed is not None:
            self.metar_feed.fast_poll_s = self.cfg.metar.fast_poll_s
            self.metar_feed.idle_poll_s = self.cfg.metar.idle_poll_s
            self.metar_feed.glitch_delta_c = self.cfg.metar.glitch_delta_c
            if self.cfg.metar.enabled and self.metar_feed._task is None and self.phase == "running":
                await self.metar_feed.start()
            elif not self.cfg.metar.enabled and self.metar_feed._task is not None:
                await self.metar_feed.stop()
        if self.binance_feed is not None:
            self.binance_feed.burst_poll_s = self.cfg.binance.burst_poll_s
            self.binance_feed.idle_poll_s = self.cfg.binance.idle_poll_s
            if self.cfg.binance.enabled and self.binance_feed._task is None and self.phase == "running":
                await self.binance_feed.start()
            elif not self.cfg.binance.enabled and self.binance_feed._task is not None:
                await self.binance_feed.stop()
        if self.rtds_feed is not None:
            if self.cfg.crypto_short.enabled and self.rtds_feed._task is None and self.phase == "running":
                await self.rtds_feed.start()
            elif not self.cfg.crypto_short.enabled and self.rtds_feed._task is not None:
                await self.rtds_feed.stop()
        now_enabled = {s.name for s in self.strategies}
        if now_enabled != prev_enabled and self._raw_events is not None:
            self._apply_universe(self._merged_events(self._raw_events))
        if self.maker_arb is not None:
            if not self.cfg.maker.enabled:
                if self.maker_arb.active:
                    await self.maker_arb.cancel_all("maker disabled")
            elif self.maker_arb._task is None and self.phase == "running":
                await self.maker_arb.start()
            else:
                self.maker_arb._wake.set()
        if self.universe is not None:
            self.dirty.update(self.targets.keys())
            self._wake.set()

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
        for idx, (token, book) in enumerate(books.items()):
            if idx and idx % 350 == 0:
                await asyncio.sleep(0)
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
        tokens: dict[str, None] = {}
        # Put active maker quotes and 5m/15m/1h short-crypto FIRST so they occupy the dedicated ws[0] fast lane!
        if self.maker_arb is not None:
            for token in self.maker_arb.quoted_tokens():
                tokens[token] = None
        for t in self.universe.targets:
            if t.strategy == "crypto_short":
                for token in t.tokens:
                    tokens[token] = None
        for h in self.risk.state.holds:
            for leg in h.legs:
                yt = str(leg.get("yes_token") or "")
                if yt:
                    tokens[yt] = None
        now_utc = datetime.now(timezone.utc)
        lo_date = (now_utc - timedelta(days=2)).strftime("%Y-%m-%d")
        hi_date = (now_utc + timedelta(days=1)).strftime("%Y-%m-%d")
        for t in self.universe.targets:
            if t.strategy == "metar_lock" and t.event.metar_obs_date and lo_date <= t.event.metar_obs_date <= hi_date:
                for token in t.tokens:
                    tokens[token] = None
            elif t.strategy == "binance_lock" and t.event.end_date:
                if (t.event.end_date - now_utc).total_seconds() <= 36 * 3600:
                    for token in t.tokens:
                        tokens[token] = None
        max_ws_tokens = max(900, self.cfg.engine.ws_max_targets * 2)
        ranked = []
        for t in self.universe.targets:
            edge = self.by_name[t.strategy].top_edge(t, self.store)
            if edge is not None:
                ranked.append((edge, t))
        ranked.sort(key=lambda pair: -pair[0])
        for _, t in ranked[: self.cfg.engine.ws_max_targets]:
            if len(tokens) >= max_ws_tokens:
                break
            for token in t.tokens:
                tokens[token] = None
        return list(tokens)

    def _dynamic_budget(self, strategy_name: str, event_id: str, include_preemptible: bool = False) -> float | None:
        saved_res = self.risk.reserved_usd
        if include_preemptible and self.maker_arb is not None and self.maker_arb.active:
            self.risk.reserved_usd = 0.0
        try:
            if strategy_name == "negrisk_convert":
                b = self.risk.budget_for(Settlement.CONVERT, event_id, fast_turnover=True)
            elif strategy_name in ("metar_lock", "binance_lock", "crypto_short"):
                b = self.risk.budget_for(Settlement.RESOLUTION, event_id, fast_turnover=True)
            else:
                b = self.risk.budget_for(Settlement.RESOLUTION, event_id, fast_turnover=False)
        finally:
            self.risk.reserved_usd = saved_res
        # When budget is >= $1.50 and below max_trade_usd, size down to fit instead of skipping!
        if 1.5 <= b < self.cfg.risk.max_trade_usd:
            return b
        return None

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
        if (
            reason in ("insufficient capital", "liquid reserve protected")
            and self.maker_arb is not None
            and self.maker_arb.active
        ):
            reserve_pad = (
                self.cfg.risk.liquid_reserve_usd
                if (opp.settlement is Settlement.RESOLUTION and not opp.meta.get("fast_turnover"))
                else 0.0
            )
            needed = opp.cost + reserve_pad
            if self.risk.available() + self.risk.reserved_usd >= needed - 1e-6:
                await self.maker_arb.preempt_for_taker(needed)
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
        max_cost = self._dynamic_budget(target.strategy, target.event.id)
        if "max_cost" in inspect.signature(self.executor.execute).parameters:
            report = await self.executor.execute(opp, target, approve=self.risk.check_size, max_cost=max_cost)
        else:
            report = await self.executor.execute(opp, target, approve=self.risk.check_size)
        self.risk.finish(report)
        if self.lifecycle is not None and report.sent and report.materially_filled():
            await self.lifecycle.sync_wallet_balance()
        if not report.sent or not report.materially_filled():
            self.stats[report.action or "no fills"] += 1
            log.info("MISS %s %s -> %s (no inventory, not booked)", report.mode, opp.title[:50], report.action or "no fills")
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
            for idx, key in enumerate(keys):
                if idx and idx % 60 == 0:
                    await asyncio.sleep(0)
                    now = time.time()
                target = self.targets.get(key)
                if target is None:
                    continue
                self.stats["evaluations"] += 1
                try:
                    budget = self._dynamic_budget(target.strategy, target.event.id, include_preemptible=True)
                    for opp in self.by_name[target.strategy].evaluate(target, self.store, now, max_cost=budget):
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

    async def _short_crypto_loop(self) -> None:
        """Keep rolling 5m / 15m / 1h short-crypto targets & WS subscriptions fresh across every 5m rotation."""
        while True:
            now_s = time.time()
            # Sleep shorter right around 5-minute (:00 / :05) boundaries so new slots & finalPrice benchmarks load immediately
            rem_5m = 300.0 - (now_s % 300.0)
            # Gamma publishes priceToBeat / finalPrice late, and the live book tightens to a 0.001 tick
            # in the same window. Poll through the last 45s and the first 90s after each 5m boundary.
            near_boundary = rem_5m <= 45.0 or (now_s % 300.0) <= 90.0
            wait_s = 2.0 if near_boundary else max(8.0, self.cfg.crypto_short.poll_slugs_s)
            await asyncio.sleep(wait_s)
            if not self.cfg.crypto_short.enabled or self.gamma is None or self._raw_events is None:
                continue
            try:
                short_evs = await fetch_short_crypto_events(
                    self.gamma._http, rtds_feed=self.rtds_feed, base_url=self.gamma._base
                )
                prev_tokens = set(self.universe.tokens) if self.universe else set()
                changed_ev = False
                for sev in short_evs:
                    old = self._short_events.get(sev.id)
                    if old is None or old.price_to_beat != sev.price_to_beat or old.final_price != sev.final_price or len(old.tradeable_markets) != len(sev.tradeable_markets):
                        changed_ev = True
                    self._short_events[sev.id] = sev
                if changed_ev and self._raw_events is not None:
                    self._apply_universe(self._merged_events(self._raw_events))
                    new_tokens = [t for t in self.universe.tokens if t not in prev_tokens]  # type: ignore[union-attr]
                    if new_tokens and self.clob is not None:
                        self.store.update_many(await self.clob.get_books(new_tokens))
                    if new_tokens and self.stream is not None and self.stream.running:
                        await self.stream.start(self.hot_tokens())
                    for k_list in self._by_short_asset.values():
                        self.dirty.update(k_list)
                    self._wake.set()
                    if self.maker_arb is not None:
                        self.maker_arb._wake.set()
                # WS 1013 drops leave the local 5m book looking fresh while the real ask is gone.
                # Pull the handful of tokens inside the decision window directly, ahead of the 8s sweep.
                if self.clob is not None and self.universe is not None:
                    now_hot = time.time()
                    horizon = self.cfg.crypto_short.postclose_window_s
                    hot: list[str] = []
                    for t in self.universe.targets:
                        if t.strategy != "crypto_short":
                            continue
                        end = getattr(t.payload, "end_ts", None)
                        if end is not None and end - 45.0 <= now_hot <= end + horizon:
                            hot.extend(t.tokens)
                    if hot:
                        fresh = await self.clob.get_books(list(dict.fromkeys(hot))[:48], priority=True)
                        if fresh:
                            self.store.update_many(fresh)
                            for tok in fresh:
                                for tgt in self.universe.by_token.get(tok, ()):
                                    self.dirty.add(tgt.key)
                            self._wake.set()
                            if self.maker_arb is not None:
                                self.maker_arb._wake.set()
            except Exception as exc:
                log.debug("short crypto refresh skipped: %s", exc)

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
            is_paper = self.executor is None or self.executor.gateway.mode == "paper"
            if is_paper:
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
        """Refuse new executions, cancel resting maker quotes, and wait for in-flight executions."""
        self.stopping = True
        if self.maker_arb is not None:
            await self.maker_arb.stop()
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
            if self.metar_feed is not None and self.cfg.metar.enabled:
                await self.metar_feed.start()
            if self.binance_feed is not None and self.cfg.binance.enabled:
                await self.binance_feed.start()
            if self.rtds_feed is not None and self.cfg.crypto_short.enabled:
                await self.rtds_feed.start()
            if self.lifecycle is not None:
                await self.lifecycle.start()
            self.phase = "sweeping"
            await self.sweep()
            settle_heap()
            if self.stream:
                await self.stream.start(self.hot_tokens())
            if self.maker_arb is not None:
                await self.maker_arb.start()
            tasks = [
                asyncio.create_task(self._sweep_loop()),
                asyncio.create_task(self._eval_loop()),
                asyncio.create_task(self._refresh_loop()),
                asyncio.create_task(self._short_crypto_loop()),
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
            if self.metar_feed is not None:
                await self.metar_feed.stop()
            if self.binance_feed is not None:
                await self.binance_feed.stop()
            if self.rtds_feed is not None:
                await self.rtds_feed.stop()
            if self.lifecycle is not None:
                await self.lifecycle.stop()
            if self.stream:
                await self.stream.stop()
            self.risk.save()
            self.phase = "stopped"
