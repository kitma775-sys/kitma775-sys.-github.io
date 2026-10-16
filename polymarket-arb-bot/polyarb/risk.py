from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from polyarb.config import RiskConfig
from polyarb.models import Opportunity, Settlement

if TYPE_CHECKING:
    from polyarb.execution.base import ExecutionReport

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
log = logging.getLogger(__name__)


@dataclass
class HoldPosition:
    key: str
    event_id: str
    strategy: str
    title: str
    cost: float
    payout: float
    shares: float
    resolves_at: str | None
    opened_at: float
    legs: list[dict] = field(default_factory=list)


@dataclass
class RiskState:
    day: str = ""
    realized_today: float = 0.0
    realized_total: float = 0.0
    consecutive_failures: int = 0
    killed: bool = False
    kill_reason: str = ""
    trades: int = 0
    holds: list[HoldPosition] = field(default_factory=list)


class RiskManager:
    """Pre-trade gate and post-trade bookkeeping. Every rejection has a reason string."""

    def __init__(self, cfg: RiskConfig, state_path: str | Path | None = None) -> None:
        self.cfg = cfg
        self.state_path = Path(state_path) if state_path else None
        self.state = self._load()
        self.last_attempt: dict[str, float] = {}
        self.inflight_events: set[str] = set()
        self.geo: dict | None = None

    def _load(self) -> RiskState:
        if self.state_path and self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            holds = [HoldPosition(**h) for h in raw.pop("holds", [])]
            return RiskState(**raw, holds=holds)
        return RiskState()

    def save(self) -> None:
        if not self.state_path:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), indent=2))
        tmp.replace(self.state_path)

    @property
    def hold_exposure(self) -> float:
        return sum(h.cost for h in self.state.holds)

    def event_exposure(self, event_id: str) -> float:
        return sum(h.cost for h in self.state.holds if h.event_id == event_id)

    def available(self) -> float:
        return self.cfg.bankroll_usd + self.state.realized_total - self.hold_exposure

    def _roll_day(self, now: float) -> None:
        day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y-%m-%d")
        if day != self.state.day:
            self.state.day = day
            self.state.realized_today = 0.0

    def check(self, opp: Opportunity, now: float | None = None) -> str | None:
        now = now or time.time()
        self._roll_day(now)
        cfg = self.cfg
        if self.state.killed:
            return f"killed: {self.state.kill_reason}"
        if cfg.kill_file and os.path.exists(cfg.kill_file):
            return "kill file present"
        if cfg.require_geo_ok and self.geo is not None and self.geo.get("blocked"):
            return f"geoblocked ({self.geo.get('country')})"
        if self.state.realized_today <= -cfg.max_daily_loss_usd:
            return "daily loss limit"
        if self.state.consecutive_failures >= cfg.max_consecutive_failures:
            return "consecutive failure limit"
        for k in (opp.key, f"event:{opp.event_id}"):
            last = self.last_attempt.get(k)
            if last is not None and now - last < cfg.cooldown_s:
                return "cooldown"
        if opp.event_id in self.inflight_events:
            return "event in flight"
        return self.check_size(opp)

    def check_size(self, opp: Opportunity) -> str | None:
        """Capital and exposure limits; re-run on the exact (re-sized) order right before sending."""
        cfg = self.cfg
        if opp.cost > cfg.max_trade_usd + 1e-9:
            return "trade above max_trade_usd"
        if opp.cost > self.available() + 1e-9:
            return "insufficient capital"
        if opp.settlement is Settlement.RESOLUTION:
            if self.hold_exposure + opp.cost > cfg.max_hold_exposure_usd + 1e-9:
                return "hold exposure cap"
            if self.event_exposure(opp.event_id) + opp.cost > cfg.max_event_exposure_usd + 1e-9:
                return "event exposure cap"
        return None

    def begin(self, opp: Opportunity, now: float | None = None) -> None:
        now = now or time.time()
        self.last_attempt[opp.key] = now
        self.last_attempt[f"event:{opp.event_id}"] = now
        self.inflight_events.add(opp.event_id)

    def finish(self, report: "ExecutionReport") -> None:
        opp = report.opp
        self.inflight_events.discard(opp.event_id)
        if not report.sent:
            return
        self._roll_day(report.finished_at or time.time())
        self.state.trades += 1
        self.state.realized_today += report.realized_pnl
        self.state.realized_total += report.realized_pnl
        if report.complete:
            self.state.consecutive_failures = 0
        else:
            self.state.consecutive_failures += 1
        if report.locked_cost > 0:
            self.state.holds.append(
                HoldPosition(
                    key=opp.key,
                    event_id=opp.event_id,
                    strategy=opp.strategy,
                    title=opp.title,
                    cost=report.locked_cost,
                    payout=report.expected_payout,
                    shares=report.settled_shares,
                    resolves_at=opp.resolves_at.isoformat() if opp.resolves_at else None,
                    opened_at=report.started_at,
                    legs=[{"token": r.leg.token_id, "outcome": r.leg.outcome.value, "shares": r.shares} for r in report.legs if r.ok],
                )
            )
        if report.orphaned:
            self.kill(f"{report.action or 'positions kept'} on {opp.key}; review positions before reset-kill")
        if self.state.consecutive_failures >= self.cfg.max_consecutive_failures:
            self.kill(f"{self.state.consecutive_failures} consecutive incomplete executions")
        if self.state.realized_today <= -self.cfg.max_daily_loss_usd:
            self.kill("daily loss limit reached")
        self.save()

    def release_matured(self, now: float | None = None, grace_days: float = 3.0) -> list[HoldPosition]:
        """Paper bookkeeping: credit the guaranteed payout once a hold is past resolution + grace."""
        now = now or time.time()
        done, keep = [], []
        for h in self.state.holds:
            if h.resolves_at and datetime.fromisoformat(h.resolves_at).timestamp() + grace_days * 86400 < now:
                done.append(h)
            else:
                keep.append(h)
        if done:
            self.state.holds = keep
            for h in done:
                self.state.realized_total += h.payout - h.cost
            self.save()
        return done

    def kill(self, reason: str) -> None:
        if self.state.killed:
            return
        log.error("KILL SWITCH: %s", reason)
        self.state.killed = True
        self.state.kill_reason = reason
        self.save()

    def reset(self) -> None:
        self.state.killed = False
        self.state.kill_reason = ""
        self.state.consecutive_failures = 0
        self.save()

    async def check_geo(self, http: httpx.AsyncClient | None = None) -> dict:
        own = http is None
        client = http or httpx.AsyncClient(timeout=10)
        try:
            resp = await client.get(GEOBLOCK_URL)
            resp.raise_for_status()
            self.geo = resp.json()
        except Exception as exc:
            self.geo = {"blocked": True, "error": str(exc)}
        finally:
            if own:
                await client.aclose()
        return self.geo
