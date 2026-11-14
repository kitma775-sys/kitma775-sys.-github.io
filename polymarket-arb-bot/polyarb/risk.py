from __future__ import annotations

import dataclasses
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

import httpx

from polyarb.config import RiskConfig
from polyarb.models import Opportunity, Settlement

if TYPE_CHECKING:
    from polyarb.execution.base import ExecutionReport

GEOBLOCK_URL = "https://polymarket.com/api/geoblock"
# Close-only on polymarket.com only, "the API itself is not restricted" (docs.polymarket.com/api-reference/geoblock);
# the geoblock endpoint is on polymarket.com and still reports blocked=true for them.
API_UNRESTRICTED = frozenset({"IE", "JP", "KR", "MT", "NL"})
REGION_REJECTED = "region_rejected"
log = logging.getLogger(__name__)


def api_allowed(geo: Mapping[str, Any] | None) -> bool:
    """Whether Polymarket accepts API orders from this IP; a failed lookup counts as blocked."""
    if not geo or geo.get("error"):
        return False
    if geo.get("blocked") is False:
        return True
    return geo.get("blocked") is True and str(geo.get("country") or "").upper() in API_UNRESTRICTED


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
    condition_ids: list[str] = field(default_factory=list)
    extra_event_ids: list[str] = field(default_factory=list)
    exit_order_id: str | None = None
    exit_price: float = 0.0


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
        self.wallet_balance: float | None = None
        self.reserved_usd: float = 0.0

    def _load(self) -> RiskState:
        if self.state_path and self.state_path.exists():
            raw = json.loads(self.state_path.read_text())
            hold_fields = {f.name for f in dataclasses.fields(HoldPosition)}
            state_fields = {f.name for f in dataclasses.fields(RiskState)} - {"holds"}
            holds = [HoldPosition(**{k: v for k, v in h.items() if k in hold_fields}) for h in raw.pop("holds", [])]
            return RiskState(**{k: v for k, v in raw.items() if k in state_fields}, holds=holds)
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

    def formula_available(self) -> float:
        return self.cfg.bankroll_usd + self.state.realized_total - self.hold_exposure - self.reserved_usd

    def available(self) -> float:
        base = self.formula_available()
        if self.wallet_balance is None:
            return base
        return min(base, self.wallet_balance - self.reserved_usd)

    def budget_for(self, settlement: Settlement, event_id: str, fast_turnover: bool = False) -> float:
        """Exact spendable pUSD for this opportunity so strategies down-size instead of skipping."""
        avail = self.available()
        cap = min(self.cfg.max_trade_usd, avail)
        if settlement is Settlement.RESOLUTION:
            cap = min(cap, self.cfg.max_hold_exposure_usd - self.hold_exposure)
            cap = min(cap, self.cfg.max_event_exposure_usd - self.event_exposure(event_id))
            if self.cfg.liquid_reserve_usd > 0 and not fast_turnover:
                cap = min(cap, avail - self.cfg.liquid_reserve_usd)
        return max(0.0, cap)

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
        if cfg.require_geo_ok and self.geo is not None and not api_allowed(self.geo):
            return f"geoblocked ({self.geo.get('country') or self.geo.get('error')})"
        if self.state.realized_today <= -cfg.max_daily_loss_usd:
            return "daily loss limit"
        if opp.strategy in ("maker_crypto_pair", "maker_micro_merge", "maker_micro_corridor") or opp.meta.get("mode") in (
            "anchor_1h",
            "sync_5m_15m",
            "micro_corridor",
            "micro_merge",
            "micro_tail",
        ):
            ids = [opp.event_id, *(str(eid) for eid in (opp.meta.get("event_ids") or []))]
            if any(self.event_exposure(eid) > 0.5 for eid in ids):
                return "pair already open"
        if self.state.consecutive_failures >= cfg.max_consecutive_failures:
            return "consecutive failure limit"
        cooldown_keys = [opp.key, *(f"mkt:{leg.market_id}" for leg in opp.legs if leg.market_id)]
        if not opp.meta.get("independent_legs"):
            cooldown_keys.append(f"event:{opp.event_id}")
        for k in cooldown_keys:
            last = self.last_attempt.get(k)
            if last is not None and now - last < cfg.cooldown_s:
                return "cooldown"
        if opp.event_id in self.inflight_events:
            return "event in flight"
        return self.check_size(opp)

    def occupied_rounds(self, strategies: tuple[str, ...]) -> set[str]:
        """Event ids that already hold inventory for one of ``strategies``."""
        names = set(strategies)
        ids: set[str] = set()
        for hold in self.state.holds:
            if hold.strategy not in names:
                continue
            ids.add(hold.event_id)
            ids.update(str(eid) for eid in hold.extra_event_ids)
        return ids

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
            if cfg.liquid_reserve_usd > 0 and not opp.meta.get("fast_turnover"):
                if opp.cost > self.available() - cfg.liquid_reserve_usd + 1e-9:
                    return "liquid reserve protected"
        return None

    def begin(self, opp: Opportunity, now: float | None = None) -> None:
        now = now or time.time()
        self.last_attempt[opp.key] = now
        for leg in opp.legs:
            if leg.market_id:
                self.last_attempt[f"mkt:{leg.market_id}"] = now
        if not opp.meta.get("independent_legs"):
            self.last_attempt[f"event:{opp.event_id}"] = now
        self.inflight_events.add(opp.event_id)

    def finish(self, report: "ExecutionReport") -> None:
        opp = report.opp
        self.inflight_events.discard(opp.event_id)
        if not report.sent:
            return
        region_hit = any(r.status == REGION_REJECTED for r in report.legs + report.retries)
        if region_hit:
            self.kill("Polymarket refused orders from this server (HTTP 403, e.g. region restriction); check the geoblock policy before reset-kill")
        # Unmatched FOK / dust: the order was submitted and rejected with no inventory.
        # Booking it as a trade paints $0 fills on Telegram and trips the kill switch.
        if not report.materially_filled():
            if region_hit:
                self.save()
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
                    legs=[
                        {
                            "token": r.leg.token_id,
                            "yes_token": r.leg.yes_token,
                            "market_id": r.leg.market_id,
                            "outcome": r.leg.outcome.value,
                            "shares": r.shares,
                            "tick": r.leg.tick_size,
                            "min_size": r.leg.min_size,
                            "fee_rate": r.leg.fee_schedule.rate,
                            "fee_exponent": r.leg.fee_schedule.exponent,
                        }
                        for r in report.legs + report.retries
                        if r.ok and r.shares > 0
                    ],
                    condition_ids=list(opp.meta.get("condition_ids") or []),
                    extra_event_ids=[str(eid) for eid in (opp.meta.get("event_ids") or []) if str(eid) != opp.event_id],
                )
            )
        if report.orphaned:
            self.kill(f"{report.action or 'positions kept'} on {opp.key}; review positions before reset-kill")
        if self.state.consecutive_failures >= self.cfg.max_consecutive_failures:
            self.kill(f"{self.state.consecutive_failures} consecutive incomplete executions")
        if self.state.realized_today <= -self.cfg.max_daily_loss_usd:
            self.kill("daily loss limit reached")
        self._freeze_open_pair(opp, report.finished_at or time.time())
        self.save()

    def _freeze_open_pair(self, opp: Opportunity, now: float) -> None:
        """One 1h/5m cross-pair per window. A partial fill must not be re-tried for the rest of the hour."""
        if opp.strategy not in ("maker_crypto_pair", "maker_micro_corridor") and opp.meta.get("mode") not in (
            "anchor_1h",
            "sync_5m_15m",
            "micro_corridor",
        ):
            return
        if opp.resolves_at is not None:
            horizon = opp.resolves_at.timestamp()
        else:
            horizon = now + 3600.0
        stamp = horizon - max(self.cfg.cooldown_s, 1.0)
        keys = [opp.key, f"event:{opp.event_id}"]
        for eid in opp.meta.get("event_ids") or []:
            keys.append(f"event:{eid}")
        for leg in opp.legs:
            if leg.market_id:
                keys.append(f"mkt:{leg.market_id}")
        for key in keys:
            self.last_attempt[key] = max(self.last_attempt.get(key, 0.0), stamp)

    def close_hold(self, hold: HoldPosition, proceeds: float, now: float | None = None) -> float:
        """Close a held position (via fast flip sell or on-chain redemption) and book its exact PnL."""
        now = now or time.time()
        self._roll_day(now)
        pnl = proceeds - hold.cost
        self.state.holds = [h for h in self.state.holds if h is not hold]
        self.state.realized_today += pnl
        self.state.realized_total += pnl
        if self.state.realized_today <= -self.cfg.max_daily_loss_usd:
            self.kill("daily loss limit reached")
        self.save()
        return pnl

    async def reconcile_holds(
        self,
        position_fn: Callable[[str], Awaitable[float]],
        now: float | None = None,
        min_age_s: float = 120.0,
    ) -> list[HoldPosition]:
        """Drop phantom holds whose conditional tokens were already redeemed or sold outside the bot."""
        now = now or time.time()
        removed: list[HoldPosition] = []
        keep: list[HoldPosition] = []
        for h in self.state.holds:
            if not h.legs or now - h.opened_at < min_age_s:
                keep.append(h)
                continue
            still_held = False
            for leg in h.legs:
                tok = leg.get("token")
                if not tok:
                    still_held = True
                    break
                try:
                    bal = await position_fn(str(tok))
                except Exception:
                    still_held = True
                    break
                if bal >= 0.05:
                    still_held = True
                    break
            if still_held:
                keep.append(h)
            else:
                removed.append(h)
        if removed:
            self.state.holds = keep
            # A naked remainder was kept with no assumed payout. Once the tokens
            # are gone and nothing redeemed them, the premium is a real loss.
            for h in removed:
                if h.payout <= 0.01 and h.cost > 0.01:
                    self._roll_day(now)
                    pnl = -h.cost
                    self.state.realized_today += pnl
                    self.state.realized_total += pnl
            if self.state.realized_today <= -self.cfg.max_daily_loss_usd:
                self.kill("daily loss limit reached")
            self.save()
        return removed

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
