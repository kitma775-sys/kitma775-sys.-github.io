from datetime import timedelta

import httpx
import pytest

from polyarb.config import RiskConfig
from polyarb.execution.base import ExecutionReport, LegResult
from polyarb.models import Leg, Opportunity, Outcome, Settlement, Side
from polyarb.risk import RiskManager

from conftest import NOW

T0 = NOW.timestamp()


def opp(cost=50.0, event="E1", key="k1", settlement=Settlement.RESOLUTION, payout=None, resolves_days=10):
    leg = Leg("T", "m", Outcome.YES, Side.BUY, 10, cost, 0.0, 0.5, 0.01, 5, False, "leg")
    return Opportunity(
        strategy="test",
        key=key,
        event_id=event,
        title="t",
        legs=[leg],
        shares=10,
        cost=cost,
        payout_floor=payout if payout is not None else cost * 1.05,
        settlement=settlement,
        risk="medium",
        resolves_at=NOW + timedelta(days=resolves_days),
        detected_at=T0,
    )


def report(o, complete=True, pnl=0.0, locked=0.0, payout=0.0, sent=True, orphaned=False):
    r = ExecutionReport(opp=o, mode="paper", started_at=T0, finished_at=T0 + 1, sent=sent, complete=complete)
    r.legs = [LegResult(o.legs[0], True, shares=10, spent=o.cost)]
    r.realized_pnl, r.locked_cost, r.expected_payout, r.orphaned = pnl, locked, payout, orphaned
    return r


@pytest.fixture
def risk(tmp_path):
    cfg = RiskConfig(kill_file=str(tmp_path / "KILL"), cooldown_s=30)
    return RiskManager(cfg, tmp_path / "state.json")


def test_clean_opportunity_passes(risk):
    assert risk.check(opp(), T0) is None


def test_kill_switch_and_kill_file(risk, tmp_path):
    (tmp_path / "KILL").write_text("")
    assert risk.check(opp(), T0) == "kill file present"
    (tmp_path / "KILL").unlink()
    risk.kill("manual")
    risk.kill("second reason")
    assert risk.check(opp(), T0) == "killed: manual"
    risk.reset()
    assert risk.check(opp(), T0) is None


def test_geoblock(risk):
    risk.geo = {"blocked": True, "country": "US"}
    assert risk.check(opp(), T0) == "geoblocked (US)"
    risk.cfg.require_geo_ok = False
    assert risk.check(opp(), T0) is None


def test_daily_loss_limit_resets_next_day(risk):
    risk.check(opp(), T0)
    risk.state.realized_today = -25.0
    assert risk.check(opp(), T0) == "daily loss limit"
    assert risk.check(opp(), T0 + 86400) is None


def test_consecutive_failures(risk):
    risk.state.consecutive_failures = 3
    assert risk.check(opp(), T0) == "consecutive failure limit"


def test_cooldown_and_inflight(risk):
    o = opp()
    risk.begin(o, T0)
    assert risk.check(o, T0 + 1) == "cooldown"
    assert risk.check(opp(key="other"), T0 + 1) == "cooldown"  # same event
    assert risk.check_size(o) is None  # the executor's pre-send re-approval ignores its own attempt
    risk.finish(report(o, sent=False))
    assert risk.check(o, T0 + 31) is None
    risk.inflight_events.add("E1")
    assert risk.check(o, T0 + 31) == "event in flight"


def test_size_and_capital_limits(risk):
    assert risk.check(opp(cost=150), T0) == "trade above max_trade_usd"
    risk.cfg.bankroll_usd = 40
    assert risk.check(opp(cost=50), T0) == "insufficient capital"


def test_exposure_caps_apply_to_holds_only(risk):
    risk.cfg.max_hold_exposure_usd = 120
    risk.cfg.max_event_exposure_usd = 80
    risk.finish(report(opp(cost=60), locked=60, payout=63))
    assert risk.check(opp(cost=50, event="E2"), T0) is None
    assert risk.check(opp(cost=30), T0) == "event exposure cap"
    assert risk.check(opp(cost=70, event="E2"), T0) == "hold exposure cap"
    assert risk.check(opp(cost=70, event="E2", settlement=Settlement.CONVERT), T0) is None


def test_finish_tracks_pnl_failures_and_kills(risk):
    risk.finish(report(opp(), pnl=1.5))
    assert risk.state.trades == 1 and risk.state.realized_total == pytest.approx(1.5)
    for _ in range(3):
        risk.finish(report(opp(), complete=False, pnl=-0.5))
    assert risk.state.killed and "consecutive" in risk.state.kill_reason
    assert risk.state.realized_today == pytest.approx(0.0)


def test_daily_loss_trips_kill(risk):
    risk.finish(report(opp(), complete=False, pnl=-30))
    assert risk.state.killed and risk.state.kill_reason == "daily loss limit reached"


def test_orphaned_legs_trip_kill_and_are_tracked(risk):
    risk.finish(report(opp(settlement=Settlement.CONVERT), complete=False, locked=9.0, payout=0.2, orphaned=True))
    assert risk.state.killed and "unhedged" in risk.state.kill_reason
    assert risk.hold_exposure == pytest.approx(9.0)


def test_state_persists_and_holds_release(risk, tmp_path):
    o = opp(cost=40, resolves_days=2)
    risk.finish(report(o, locked=40, payout=42))
    reloaded = RiskManager(risk.cfg, tmp_path / "state.json")
    assert len(reloaded.state.holds) == 1 and reloaded.available() == pytest.approx(960)
    assert reloaded.release_matured(T0 + 4 * 86400) == []  # still inside the 3-day grace
    released = reloaded.release_matured(T0 + 6 * 86400)
    assert [h.key for h in released] == ["k1"]
    assert reloaded.state.realized_total == pytest.approx(2.0)
    assert reloaded.hold_exposure == 0


async def test_check_geo_parses_and_fails_closed(risk):
    ok = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"blocked": False, "country": "HK"})))
    assert (await risk.check_geo(ok))["blocked"] is False
    down = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503)))
    geo = await risk.check_geo(down)
    assert geo["blocked"] is True and "error" in geo
