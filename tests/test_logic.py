from __future__ import annotations

from app.fees import taker_net
from app.hunter import Level, hunt
from app.risk import approve
from app.store import Store


def test_mid_taker_is_negative():
    assert taker_net(100, 0.55, 0.42, 0.07) < 0


def test_tail_taker_is_positive():
    assert taker_net(100, 0.97, 0.01, 0.07) > 1


def _L(*pairs):
    return [Level(p, s) for p, s in pairs]


def test_hunter_finds_tail_taker():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 80)),
        down_asks=_L((0.01, 80)),
        up_bids=_L((0.96, 10)),
        down_bids=_L((0.005, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=True,
    )
    assert setup is not None
    assert setup.kind == "taker"
    assert setup.tail is True
    assert setup.net > 0


def test_hunter_skips_expensive_asks():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.70, 100)),
        down_asks=_L((0.40, 100)),
        up_bids=_L((0.50, 10)),
        down_bids=_L((0.48, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
    )
    assert setup is None


def test_risk_blocks_stale_and_kill():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 80)),
        down_asks=_L((0.01, 80)),
        up_bids=_L((0.96, 10)),
        down_bids=_L((0.005, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=True,
    )
    assert setup is not None
    dead = approve(
        setup,
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=0,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=0,
        max_open_markets=8,
        killed=True,
        engine_running=True,
        auto_execute=True,
    )
    assert dead.ok is False
    ok = approve(
        setup,
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=0,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=0,
        max_open_markets=8,
        killed=False,
        engine_running=True,
        auto_execute=True,
    )
    assert ok.ok is True


def test_maker_rejects_cheap_leg():
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="maker",
        up_price=0.97,
        down_price=0.01,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0,
        net=0.2,
        tail=True,
    )
    d = approve(
        setup,
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=0,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=0,
        max_open_markets=8,
        killed=False,
        engine_running=True,
        auto_execute=True,
    )
    assert d.ok is False
    assert d.reason == "maker_unbalanced"


def test_store_merge(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.add_inventory("c1", "btc", 10, 10)
    out = st.merge_inventory("c1", 10)
    assert out["merged"] == 10
    assert st.inventory_one("c1")["up"] == 0
    assert st.inventory_one("c1")["down"] == 0


def test_setup_cost_is_shares_minus_net():
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.01,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0.02,
        net=0.18,
        tail=True,
    )
    assert setup.cost == 9.82


def test_risk_blocks_insufficient_cash():
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.01,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0.02,
        net=0.18,
        tail=True,
    )
    kwargs = dict(
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=0,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=0,
        max_open_markets=8,
        killed=False,
        engine_running=True,
        auto_execute=True,
    )
    blocked = approve(setup, cash=5.0, cost=setup.cost, **kwargs)
    assert blocked.ok is False
    assert blocked.reason == "insufficient_cash"
    ok = approve(setup, cash=500.0, cost=setup.cost, **kwargs)
    assert ok.ok is True


def test_paper_ledger_buy_merge_pnl(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    book = st.ensure_paper(500)
    assert book["cash"] == 500
    assert book["equity"] == 500
    assert book["total_pnl"] == 0

    st.paper_apply_buy(9.82)
    st.add_inventory("c1", "btc", 10, 10)
    mid = st.paper_state()
    assert round(mid["cash"], 2) == 490.18
    assert mid["inventory_value"] == 10
    assert round(mid["equity"], 2) == 500.18
    assert round(mid["total_pnl"], 2) == 0.18

    merged = st.merge_inventory("c1", 10)
    assert merged["merged"] == 10
    end = st.paper_apply_merge(10, 0.18)
    assert round(end["cash"], 2) == 500.18
    assert end["inventory_value"] == 0
    assert round(end["equity"], 2) == 500.18
    assert round(end["total_pnl"], 2) == 0.18
    assert round(end["realized_pnl"], 2) == 0.18


def test_paper_apply_buy_rejects_overdraft(tmp_path):
    st = Store(tmp_path / "t.sqlite")
    st.ensure_paper(5)
    try:
        st.paper_apply_buy(9.82)
        raise AssertionError("expected insufficient_cash")
    except ValueError as exc:
        assert "insufficient_cash" in str(exc)


def test_geo_japan_website_block_api_open():
    from app.geo import interpret, telegram_line

    g = interpret({"blocked": True, "ip": "43.153.168.189", "country": "JP", "region": "13"})
    assert g["website_blocked"] is True
    assert g["frontend_only"] is True
    assert g["api_open"] is True
    assert g["blocked"] is False
    assert "CLOB API" in telegram_line(g)


def test_geo_us_api_close_only():
    from app.geo import interpret

    g = interpret({"blocked": True, "ip": "1.1.1.1", "country": "US", "region": "NY"})
    assert g["api_open"] is False
    assert g["api_status"] == "close_only"
    assert g["blocked"] is True



def test_paper_maker_does_not_instant_fill():
    from app.broker import paper_execute
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="maker",
        up_price=0.50,
        down_price=0.49,
        shares=10,
        fillable=10,
        gross=0.01,
        fees=0.0,
        net=0.10,
        tail=False,
    )
    result = paper_execute(setup)
    assert result.ok is True
    assert result.status == "paper_resting"
    assert result.payload["assumed_fill"] is False


def test_paper_taker_fills_at_quote():
    from app.broker import paper_execute
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.01,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0.02,
        net=0.18,
        tail=True,
        extra={"fee_rate": 0.07},
    )
    result = paper_execute(setup)
    assert result.status == "paper_filled"
    assert result.payload["assumed_fill"] is False
    assert result.payload["net"] > 0


def test_paper_taker_slip_can_kill_edge():
    from app.broker import paper_execute
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.48,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0.0,
        net=0.2,
        tail=False,
        extra={"fee_rate": 0.07, "paper_slip_ticks": 1},
    )
    result = paper_execute(setup)
    assert result.ok is False
    assert result.status == "paper_missed"


def test_asks_cross_bid_requires_size_through():
    from app.hunter import Level
    from app.paper_sim import asks_cross_bid

    asks = [Level(0.50, 2.0), Level(0.51, 100.0)]
    assert asks_cross_bid(asks, 0.50, 5) is False
    asks = [Level(0.49, 5.0)]
    assert asks_cross_bid(asks, 0.50, 5) is True
    asks = [Level(0.51, 100.0)]
    assert asks_cross_bid(asks, 0.50, 5) is False


def test_paper_resting_no_pnl_until_both_legs(tmp_path):
    st = Store(tmp_path / "rest.sqlite")
    st.ensure_paper(500)
    row = st.add_resting(
        slug="btc",
        condition_id="c1",
        title="btc",
        up_token="u",
        down_token="d",
        shares=10,
        up_price=0.50,
        down_price=0.49,
        net=0.10,
    )
    mid = st.paper_state()
    assert round(mid["cash"], 2) == 490.10
    assert round(mid["reserved"], 2) == 9.90
    assert round(mid["equity"], 2) == 500.00
    assert round(mid["total_pnl"], 2) == 0.00

    one = st.fill_resting_leg(row["id"], "up")
    assert one["up_filled"] is True
    assert one["down_filled"] is False
    inv = st.inventory_one("c1")
    assert inv["up"] == 10
    assert inv["down"] == 0
    after_one = st.paper_state()
    # unmatched inventory marked $0; spent the up leg
    assert round(after_one["equity"], 2) == 495.00
    assert after_one["inventory_value"] == 0

    both = st.fill_resting_leg(row["id"], "down")
    assert both["status"] == "filled"
    matched = st.paper_state()
    assert matched["inventory_value"] == 10
    merged = st.merge_inventory("c1", 10)
    assert merged["merged"] == 10
    end = st.paper_apply_merge(10, 0.10)
    assert round(end["cash"], 2) == 500.10
    assert round(end["total_pnl"], 2) == 0.10
    assert round(end["realized_pnl"], 2) == 0.10


def test_clamp_paper_cash_bounds():
    from app.config import clamp_paper_cash

    assert clamp_paper_cash(10) == 50
    assert clamp_paper_cash(200000) == 100000
    assert clamp_paper_cash(1500) == 1500


def test_reset_paper_custom_bankroll(tmp_path):
    st = Store(tmp_path / "bank.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(20)
    st.add_inventory("c1", "btc", 5, 0)
    st.patch_settings(paper_starting_cash=2000)
    out = st.reset_paper(2000)
    assert out["starting"] == 2000
    assert out["cash"] == 2000
    assert out["equity"] == 2000
    assert out["total_pnl"] == 0
    assert out["reserved"] == 0
    assert st.inventory() == []
    assert st.resting_open() == []


def test_paper_bankroll_reads_settings(tmp_path):
    from app.config import Env
    from app.runtime import Runtime

    st = Store(tmp_path / "bank2.sqlite")
    st.ensure_paper(500)
    st.patch_settings(paper_starting_cash=750)
    rt = Runtime(st, Env(paper_starting_cash=500))
    assert rt.paper_bankroll() == 750


def test_set_bankroll_does_not_wipe_book(tmp_path):
    st = Store(tmp_path / "keep.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(20)
    st.add_inventory("c1", "btc", 5, 0)
    st.patch_settings(paper_starting_cash=2000)
    p = st.paper_state()
    assert p["starting"] == 500
    assert p["cash"] == 480
    assert st.inventory_one("c1")["up"] == 5


def test_reset_paper_keeps_trade_history(tmp_path):
    st = Store(tmp_path / "hist.sqlite")
    st.ensure_paper(500)
    st.add_trade(slug="btc", kind="taker", shares=10, up_price=0.97, down_price=0.01, net=0.2, mode="paper", status="paper_filled")
    st.reset_paper(1000)
    trades = st.recent_trades()
    assert len(trades) == 1
    assert trades[0]["slug"] == "btc"
    assert st.paper_state()["starting"] == 1000


def test_paper_state_uses_settings_if_uninitialized(tmp_path):
    st = Store(tmp_path / "uninit.sqlite")
    st.patch_settings(paper_starting_cash=800)
    p = st.paper_state()
    assert p["starting"] == 800
    assert p["cash"] == 800


def test_dashboard_paper_bankroll_actions(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Env
    from app.dashboard import create_app
    from app.runtime import Runtime

    st = Store(tmp_path / "dash.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(20)
    st.add_trade(slug="keep", kind="taker", shares=5, up_price=0.9, down_price=0.1, net=0.0, mode="paper", status="paper_filled")
    rt = Runtime(st, Env(dashboard_token="tok", paper_starting_cash=500))
    client = TestClient(create_app(rt))
    saved = client.post("/api/action/set_paper_cash?amount=2000&t=tok")
    assert saved.status_code == 200
    assert saved.json()["settings"]["paper_starting_cash"] == 2000
    assert saved.json()["paper"]["cash"] == 480
    reset = client.post("/api/action/reset_paper?amount=1500&t=tok")
    assert reset.status_code == 200
    book = reset.json()["paper"]
    assert book["starting"] == 1500
    assert book["cash"] == 1500
    assert book["equity"] == 1500
    assert st.inventory() == []
    assert st.recent_trades()[0]["slug"] == "keep"


def test_fmt_exc_names_empty_timeouts():
    from app.runtime import fmt_exc
    import httpx

    msg = fmt_exc(httpx.ReadTimeout(""))
    assert msg.startswith("ReadTimeout")
    assert "ReadTimeout" in msg
    empty = fmt_exc(TimeoutError())
    assert empty.startswith("TimeoutError")


def test_rescue_prefers_hedge_over_dump():
    from app.rescue import plan_rescue

    plan = plan_rescue(
        filled_px=0.71,
        shares=10,
        other_asks=_L((0.32, 80)),
        filled_bids=_L((0.65, 80)),
        fee_rate=0.07,
    )
    assert plan.action == "hedge"
    assert plan.pnl > -2
    assert plan.pnl > -10 * 0.71 + 1  # better than dump/hold of the full leg


def test_rescue_dumps_when_other_ask_is_one():
    from app.rescue import plan_rescue

    plan = plan_rescue(
        filled_px=0.71,
        shares=10,
        other_asks=_L((0.99, 80)),
        filled_bids=_L((0.60, 80)),
        fee_rate=0.07,
    )
    assert plan.action == "dump"
    assert plan.cash_out > 5


def test_rescue_hold_when_no_book():
    from app.rescue import plan_rescue

    plan = plan_rescue(filled_px=0.71, shares=10, other_asks=[], filled_bids=[], fee_rate=0.07)
    assert plan.action == "hold"


def test_parse_outcome_prices_json():
    from app.rescue import parse_outcome_prices

    assert parse_outcome_prices('["1","0"]') == (1.0, 0.0)
    assert parse_outcome_prices([0, 1]) == (0.0, 1.0)


def test_hunt_skips_maker_when_window_far():
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(seconds=600)).isoformat()
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.70, 100)),
        down_asks=_L((0.40, 100)),
        up_bids=_L((0.50, 80)),
        down_bids=_L((0.48, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=True,
        end=end,
    )
    assert setup is None


def test_hunt_allows_late_balanced_maker():
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.70, 100)),
        down_asks=_L((0.40, 100)),
        up_bids=_L((0.50, 80)),
        down_bids=_L((0.48, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=True,
        end=end,
    )
    assert setup is not None
    assert setup.kind == "maker"


def test_hunt_rejects_skewed_maker():
    from datetime import datetime, timedelta, timezone

    end = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.80, 100)),
        down_asks=_L((0.30, 100)),
        up_bids=_L((0.71, 80)),
        down_bids=_L((0.27, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=True,
        end=end,
    )
    assert setup is None


def test_risk_maker_too_early():
    from app.hunter import Setup

    setup = Setup(
        slug="x",
        title="x",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="maker",
        up_price=0.50,
        down_price=0.48,
        shares=10,
        fillable=10,
        gross=0.02,
        fees=0,
        net=0.2,
        tail=False,
    )
    d = approve(
        setup,
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=0,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=0,
        max_open_markets=8,
        killed=False,
        engine_running=True,
        auto_execute=True,
        seconds_left=600,
    )
    assert d.ok is False
    assert d.reason == "maker_too_early"


def test_circuit_tripped_uses_equity_pnl(tmp_path):
    from app.config import Env
    from app.runtime import Runtime

    st = Store(tmp_path / "c.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(80)
    st.patch_settings(daily_loss_limit_usd=50)
    rt = Runtime(st, Env())
    assert rt.store.paper_state()["today_pnl"] <= -50
    assert rt.circuit_tripped() is True


def test_paper_settle_credits_winner(tmp_path):
    st = Store(tmp_path / "s.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 25.5, 0)
    before = st.paper_state()
    st.take_inventory("c1", up=25.5, down=0)
    st.paper_apply_credit(25.5)
    after = st.paper_state()
    assert after["cash"] - before["cash"] == 25.5
    assert st.inventory_one("c1")["up"] == 0
