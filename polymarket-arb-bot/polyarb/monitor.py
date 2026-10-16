"""Live view of a running engine for the dashboard: ring buffers, time series, near-miss ranking, SSE fan-out."""

from __future__ import annotations

import asyncio
import heapq
import logging
import os
import resource
import time
from bisect import bisect_left, bisect_right
from collections import Counter, deque
from dataclasses import dataclass, field
from typing import Any

from polyarb import __version__
from polyarb.engine import Engine
from polyarb.fastjson import dumps
from polyarb.risk import api_allowed

log = logging.getLogger(__name__)

# Top-of-book edge per unit; bins are (<-10c, -10..-5, -5..-3, -3..-2, -2..-1, -1..-0.5, -0.5..0, 0..0.5, 0.5..1, >=1c).
EDGE_BINS = (-0.10, -0.05, -0.03, -0.02, -0.01, -0.005, 0.0, 0.005, 0.01)
SERIES_FIELDS = ("t", "pnl", "today", "locked", "exposure", "ws", "evals", "fresh", "cpu", "rss", "opps")
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _rss_mb() -> float:
    try:
        with open("/proc/self/statm") as fh:
            return int(fh.read().split()[1]) * _PAGE / 1e6
    except (OSError, ValueError, IndexError):
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def _cpu_seconds() -> float:
    ru = resource.getrusage(resource.RUSAGE_SELF)
    return ru.ru_utime + ru.ru_stime


@dataclass
class RuntimeInfo:
    """How this process was started; shown on the dashboard and refreshed by the app (geo, wallet)."""

    mode: str
    requested_mode: str
    live_blocked: list[str] = field(default_factory=list)
    readiness: list[dict[str, Any]] = field(default_factory=list)
    wallet: dict[str, Any] | None = None
    geo: dict[str, Any] | None = None
    data_dir: str = "data"
    password_generated: bool = False
    started_at: float = field(default_factory=time.time)


class _LogTap(logging.Handler):
    def __init__(self, monitor: "Monitor") -> None:
        super().__init__(logging.INFO)
        self.monitor = monitor

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith("aiohttp.access"):
            return
        try:
            msg = record.getMessage()
        except Exception:
            return
        self.monitor.push_log({"ts": round(record.created, 3), "level": record.levelname, "name": record.name, "msg": msg[:600]})


class Monitor:
    def __init__(
        self,
        engine: Engine,
        info: RuntimeInfo,
        history: int = 200,
        sample_s: float = 10.0,
        rank_s: float = 20.0,
        tick_s: float = 2.0,
        series_points: int = 8640,
    ) -> None:
        self.engine = engine
        self.info = info
        self.sample_s = sample_s
        self.rank_s = rank_s
        self.tick_s = tick_s
        self.opportunities: deque[dict] = deque(maxlen=history)
        self.executions: deque[dict] = deque(maxlen=history)
        self.logs: deque[dict] = deque(maxlen=300)
        self.series: deque[tuple] = deque(maxlen=series_points)
        self.ranking: dict[str, Any] = {"at": 0.0, "bins": list(EDGE_BINS), "strategies": {}}
        self.exec_counts: Counter = Counter()
        self.opps_seen: set[str] = set()
        self._subscribers: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_sample: tuple[float, float, int, int] | None = None
        self._opps_since_sample = 0
        self._cpu_pct = 0.0
        self._event_slugs: dict[str, str] = {}
        self._slugs_universe: object = None
        self._tap = _LogTap(self)
        engine.journal.listeners.append(self._on_record)

    # ------------------------------------------------------------------ inputs

    def attach_logging(self, root: logging.Logger | None = None) -> None:
        (root or logging.getLogger()).addHandler(self._tap)

    def detach_logging(self, root: logging.Logger | None = None) -> None:
        (root or logging.getLogger()).removeHandler(self._tap)

    def restore(self, records: list[dict], horizon_s: float = 86400.0) -> None:
        """Rebuild feeds, counters and the PnL curve from the journal so a restart keeps its history."""
        cutoff = time.time() - horizon_s
        for rec in records:
            kind = rec.get("kind")
            if kind == "opportunity":
                self.opportunities.append(rec)
                self.opps_seen.add(rec.get("key", ""))
            elif kind == "execution":
                self.executions.append(rec)
                self._count_execution(rec)
            elif kind == "heartbeat" and rec.get("ts", 0) >= cutoff:
                point = dict.fromkeys(SERIES_FIELDS)
                point.update(
                    t=rec["ts"], pnl=rec.get("pnl_total"), today=rec.get("pnl_today"), locked=rec.get("locked"),
                    exposure=rec.get("hold_exposure"), fresh=rec.get("fresh"),
                )
                self.series.append(tuple(point[f] for f in SERIES_FIELDS))

    def _slug(self, record: dict) -> None:
        if not record.get("slug"):
            slug = self.slug_for(record.get("key", ""), record.get("event_id"))
            if slug:
                record["slug"] = slug

    def slug_for(self, key: str, event_id: str | None = None) -> str | None:
        """Event slug for a target or opportunity key (ladder keys append ``:strong>weak`` to the target key)."""
        targets = self.engine.targets
        k = key
        while k:
            target = targets.get(k)
            if target is not None:
                return target.event.slug
            k = k.rpartition(":")[0]
        universe = self.engine.universe
        if universe is None:
            return None
        if self._slugs_universe is not universe:
            self._event_slugs = {e.id: e.slug for e in universe.events}
            self._slugs_universe = universe
        if event_id is None:
            parts = key.split(":")
            event_id = parts[1] if len(parts) > 1 else None
        return self._event_slugs.get(event_id) if event_id else None

    def _count_execution(self, rec: dict) -> None:
        self.exec_counts["sent"] += 1
        self.exec_counts["complete"] += bool(rec.get("complete"))
        self.exec_counts["orphaned"] += bool(rec.get("orphaned"))

    def _on_record(self, record: dict) -> None:
        kind = record.get("kind")
        if kind == "opportunity":
            self._slug(record)
            self.opportunities.append(record)
            self.opps_seen.add(record.get("key", ""))
            self._opps_since_sample += 1
        elif kind == "execution":
            self._slug(record)
            self.executions.append(record)
            self._count_execution(record)
        elif kind not in ("hold_released", "universe"):
            return
        self.emit(kind, record)

    def push_log(self, entry: dict) -> None:
        self.logs.append(entry)
        self.emit("log", entry)

    # ------------------------------------------------------------------ fan-out

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)

    @property
    def subscribers(self) -> int:
        return len(self._subscribers)

    def emit(self, kind: str, data: Any) -> None:
        if not self._subscribers:
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if self._loop is None or running is self._loop:
            self._broadcast(kind, data)
        else:
            self._loop.call_soon_threadsafe(self._broadcast, kind, data)

    def _broadcast(self, kind: str, data: Any) -> None:
        payload = dumps(data)
        for queue in list(self._subscribers):
            try:
                queue.put_nowait((kind, payload))
            except asyncio.QueueFull:
                pass  # a slow client catches up on the next tick, which carries full state

    # ------------------------------------------------------------------ periodic work

    async def run(self) -> None:
        self._loop = asyncio.get_running_loop()
        next_sample = next_rank = 0.0
        while True:
            now = time.monotonic()
            if now >= next_sample:
                point = self.sample()
                self.emit("sample", point)
                next_sample = now + self.sample_s
            if now >= next_rank and self.engine.phase == "running":
                await self.rank()
                self.emit("ranking", self.ranking)
                next_rank = time.monotonic() + self.rank_s
            if self._subscribers:
                self.emit("tick", self.tick())
            await asyncio.sleep(self.tick_s)

    def sample(self) -> dict[str, Any]:
        engine, risk = self.engine, self.engine.risk
        now = time.time()
        cpu = _cpu_seconds()
        msgs = engine.stream.messages if engine.stream else 0
        evals = engine.stats["evaluations"]
        ws_rate = eval_rate = None
        if self._last_sample is not None:
            t0, cpu0, msgs0, evals0 = self._last_sample
            dt = max(now - t0, 1e-6)
            self._cpu_pct = 100.0 * (cpu - cpu0) / dt
            ws_rate = max(msgs - msgs0, 0) / dt
            eval_rate = max(evals - evals0, 0) / dt
        self._last_sample = (now, cpu, msgs, evals)
        point = {
            "t": round(now, 1),
            "pnl": round(risk.state.realized_total, 4),
            "today": round(risk.state.realized_today, 4),
            "locked": round(self._locked_profit(), 4),
            "exposure": round(risk.hold_exposure, 2),
            "ws": None if ws_rate is None else round(ws_rate, 1),
            "evals": None if eval_rate is None else round(eval_rate, 1),
            "fresh": engine.store.stats()["fresh"],
            "cpu": round(self._cpu_pct, 1),
            "rss": round(_rss_mb(), 1),
            "opps": self._opps_since_sample,
        }
        self._opps_since_sample = 0
        self.series.append(tuple(point[f] for f in SERIES_FIELDS))
        return point

    async def rank(self, chunk: int = 250, per_strategy: int = 40) -> None:
        """Top-of-book edge of every target, computed in slices so evaluation never stalls behind it."""
        engine = self.engine
        universe = engine.universe
        if universe is None:
            return
        rows: dict[str, list] = {name: [] for name in engine.by_name}
        hist: dict[str, list[int]] = {name: [0] * (len(EDGE_BINS) + 1) for name in engine.by_name}
        for i, target in enumerate(universe.targets):
            if i and i % chunk == 0:
                await asyncio.sleep(0)
            strategy = engine.by_name.get(target.strategy)
            if strategy is None:
                continue
            edge = strategy.top_edge(target, engine.store)
            if edge is None:
                continue
            rows[target.strategy].append((edge, target))
            hist[target.strategy][bisect_right(EDGE_BINS, edge)] += 1
        strategies = {}
        for name, entries in rows.items():
            best = heapq.nlargest(per_strategy, entries, key=lambda e: e[0])
            strategies[name] = {
                "priced": len(entries),
                "positive": sum(1 for e, _ in entries if e > 0),
                "hist": hist[name],
                "top": [
                    {"edge": round(e, 5), "title": t.event.title, "slug": t.event.slug, "key": t.key, "legs": len(t.tokens)}
                    for e, t in best
                ],
            }
        self.ranking = {"at": round(time.time(), 1), "bins": list(EDGE_BINS), "strategies": strategies}

    # ------------------------------------------------------------------ views

    def _locked_profit(self) -> float:
        return sum(h.payout - h.cost for h in self.engine.risk.state.holds)

    def series_view(self, recent_s: float = 3600.0, max_older: int = 360) -> dict[str, list]:
        """The last ``recent_s`` at full resolution, everything older thinned to ``max_older`` points."""
        points = list(self.series)
        if points:
            split = bisect_left([p[0] for p in points], points[-1][0] - recent_s)
            older, recent = points[:split], points[split:]
            if len(older) > max_older:
                step = len(older) / max_older
                older = [older[min(int((i + 1) * step) - 1, len(older) - 1)] for i in range(max_older)]
            points = older + recent
        return {name: [p[i] for p in points] for i, name in enumerate(SERIES_FIELDS)}

    def tick(self) -> dict[str, Any]:
        engine, risk = self.engine, self.engine.risk
        state, rcfg = risk.state, risk.cfg
        universe = engine.universe
        stream = engine.stream
        store_stats = engine.store.stats()
        last = self.series[-1] if self.series else None
        skips = Counter({k: v for k, v in engine.stats.items() if k.startswith(("skip:", "abort"))})
        paused = bool(rcfg.kill_file) and os.path.exists(rcfg.kill_file)
        sent = self.exec_counts["sent"]
        return {
            "now": round(time.time(), 3),
            "mode": self.info.mode,
            "requested_mode": self.info.requested_mode,
            "phase": engine.phase,
            "kpi": {
                "pnl_total": round(state.realized_total, 4),
                "pnl_today": round(state.realized_today, 4),
                "locked_profit": round(self._locked_profit(), 4),
                "available": round(risk.available(), 2),
                "bankroll": rcfg.bankroll_usd,
                "hold_exposure": round(risk.hold_exposure, 2),
                "trades": state.trades,
                "executions": sent,
                "complete_rate": round(self.exec_counts["complete"] / sent, 4) if sent else None,
                "opportunities": engine.stats["opportunities"],
                "unique_opportunities": len(self.opps_seen),
            },
            "engine": {
                "uptime_s": round(time.time() - engine.started_at, 1),
                "events": len(universe.events) if universe else 0,
                "tokens": len(universe.tokens) if universe else 0,
                "targets": universe.counts() if universe else {},
                "books": store_stats["books"],
                "fresh": store_stats["fresh"],
                "ws_running": bool(stream and stream.running),
                "ws_tokens": len(stream.tokens) if stream else 0,
                "ws_reconnects": stream.reconnects if stream else 0,
                "ws_rate": last[SERIES_FIELDS.index("ws")] if last else None,
                "eval_rate": last[SERIES_FIELDS.index("evals")] if last else None,
                "sweeps": engine.stats["sweeps"],
                "last_sweep_s": round(engine.last_sweep_s, 2),
                "last_sweep_at": round(engine.last_sweep_at, 1),
                "last_refresh_at": round(engine.last_refresh_at, 1),
                "sweep_every_s": engine.cfg.engine.rest_sweep_s,
                "refresh_minutes": engine.cfg.universe.refresh_minutes,
                "cpu_pct": round(self._cpu_pct, 1),
                "rss_mb": round(_rss_mb(), 1),
                "inflight": len(engine._inflight),
                "eval_errors": engine.stats["eval_errors"],
                "clients": self.subscribers,
            },
            "risk": {
                "killed": state.killed,
                "kill_reason": state.kill_reason,
                "paused": paused,
                "max_trade_usd": rcfg.max_trade_usd,
                "max_daily_loss_usd": rcfg.max_daily_loss_usd,
                "max_hold_exposure_usd": rcfg.max_hold_exposure_usd,
                "max_event_exposure_usd": rcfg.max_event_exposure_usd,
                "consecutive_failures": state.consecutive_failures,
                "max_consecutive_failures": rcfg.max_consecutive_failures,
                "skips": skips.most_common(8),
            },
            "holds": [
                {
                    "title": h.title,
                    "slug": self.slug_for(h.key, h.event_id),
                    "strategy": h.strategy,
                    "cost": round(h.cost, 2),
                    "payout": round(h.payout, 2),
                    "profit": round(h.payout - h.cost, 2),
                    "resolves_at": h.resolves_at,
                    "opened_at": h.opened_at,
                }
                for h in sorted(state.holds, key=lambda h: h.resolves_at or "")
            ],
            "readiness": self.info.readiness,
            "wallet": self.info.wallet,
            "live_blocked": self.info.live_blocked,
            "geo": self._geo(),
        }

    def _geo(self) -> dict[str, Any]:
        geo = self.info.geo
        if not geo:
            return {}
        public = {k: v for k, v in geo.items() if k in ("blocked", "country", "region", "error")}
        return {**public, "api_ok": api_allowed(geo)}

    def snapshot(self) -> dict[str, Any]:
        cfg = self.engine.cfg
        opportunities = list(self.opportunities)[-80:]
        executions = list(self.executions)[-80:]
        for record in (*opportunities, *executions):
            self._slug(record)
        return {
            **self.tick(),
            "meta": {
                "version": __version__,
                "mode": self.info.mode,
                "requested_mode": self.info.requested_mode,
                "started_at": self.info.started_at,
                "data_dir": self.info.data_dir,
                "password_generated": self.info.password_generated,
            },
            "config": {
                "strategies": {s.name: s.enabled for s in self.engine.strategies},
                "convert_min_profit": cfg.convert.min_profit_usd,
                "yes_basket_min_apr": cfg.yes_basket.min_apr,
                "ladder_min_apr": cfg.ladder.min_apr,
                "slippage_ticks": cfg.execution.slippage_ticks,
                "recheck_books": cfg.execution.recheck_books,
                "ws_max_targets": cfg.engine.ws_max_targets,
            },
            "opportunities": opportunities,
            "executions": executions,
            "logs": list(self.logs)[-150:],
            "series": self.series_view(),
            "ranking": self.ranking,
        }
