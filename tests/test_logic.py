from __future__ import annotations

from app.fees import taker_net
from app.hunter import Level, book_quote, hunt, summarize_quotes
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


def _late_end(seconds: float) -> str:
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()


def test_hunt_late_maker_when_taker_first():
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
        maker_first=False,
        end=_late_end(30),
    )
    assert setup is not None
    assert setup.kind == "maker"


def test_hunt_late_one_tick_maker_uses_separate_edge():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.51, 100)),
        down_asks=_L((0.50, 100)),
        up_bids=_L((0.50, 80)),
        down_bids=_L((0.49, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(30),
        maker_min_edge=0.01,
    )
    assert setup is not None
    assert setup.kind == "maker"
    assert setup.gross == 0.01


def test_hunt_late_one_tick_skipped_without_maker_edge():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.51, 100)),
        down_asks=_L((0.50, 100)),
        up_bids=_L((0.50, 80)),
        down_bids=_L((0.49, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(30),
    )
    assert setup is None


def test_hunt_skips_one_tick_maker_outside_window():
    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        up_asks=_L((0.51, 100)),
        down_asks=_L((0.50, 100)),
        up_bids=_L((0.50, 80)),
        down_bids=_L((0.49, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(600),
        maker_min_edge=0.01,
    )
    assert setup is None


def test_book_quote_and_tape_summary():
    q = book_quote(
        slug="btc-updown",
        up_asks=_L((0.51, 10)),
        down_asks=_L((0.50, 10)),
        up_bids=_L((0.50, 10)),
        down_bids=_L((0.49, 10)),
        fee_rate=0.07,
        end=_late_end(40),
    )
    assert q["ask_sum"] == 1.01
    assert q["bid_sum"] == 0.99
    assert q["maker_gross"] == 0.01
    assert q["maker_balanced"] is True
    assert q["taker_net"] < 0
    wide = {**q, "slug": "eth-wide", "ask_sum": 1.41, "taker_net": -0.43, "maker_gross": 0.41, "maker_balanced": False}
    tape = summarize_quotes([q, wide])
    assert tape["n"] == 2
    assert tape["min_ask_sum"] == 1.01
    assert tape["max_maker_gross"] == 0.01
    assert tape["best_taker_slug"] == "btc-updown"
    assert tape["best_maker_slug"] == "btc-updown"


def test_home_text_shows_scan_tape(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import home_text

    st = Store(tmp_path / "t.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    rt.last_loop = {
        "status": "ok",
        "markets": 10,
        "signals": 0,
        "fills": 0,
        "tape": {
            "n": 10,
            "min_ask_sum": 1.01,
            "max_taker_net": -0.045,
            "max_maker_gross": 0.01,
            "nearest_s": 42,
            "nearest_slug": "btc-updown-15m",
            "slugs": ["eth-updown-15m-a", "sol-updown-15m-b"],
        },
    }
    text = home_text(rt)
    assert "ask合 1.01" in text
    assert "taker淨 -0.045/股" in text
    assert "掛單缺口" not in text
    assert "最近 42s btc-updown-15m" in text
    assert "掃 eth-updown-15m-a, sol-updown-15m-b" in text
    st.patch_settings(maker_window_seconds=75)
    assert "掛單缺口 0.01" in home_text(rt)


def test_pick_markets_ranks_soonest_and_skips_empty():
    from app.universe import DEFAULT_ASSETS, asset_hit, looks_empty, pick_markets

    rows = [
        {"condition_id": "far", "slug": "btc-updown-15m-z", "seconds_left": 2000, "best_ask": 0.51, "volume24hr": 9},
        {"condition_id": "empty", "slug": "zec-updown-15m", "seconds_left": 40, "best_ask": 1.0, "volume24hr": 1},
        {"condition_id": "soon", "slug": "eth-updown-15m-a", "seconds_left": 80, "best_ask": 0.81, "volume24hr": 5},
        {"condition_id": "next", "slug": "sol-updown-15m-b", "seconds_left": 400, "best_ask": 0.50, "volume24hr": 8},
        {"condition_id": "late", "slug": "btc-updown-15m-x", "seconds_left": 1, "best_ask": 0.97, "volume24hr": 99},
        {"condition_id": "hour", "slug": "btc-updown-1h", "seconds_left": 5000, "best_ask": 0.50, "volume24hr": 99},
        {"condition_id": "soon", "slug": "eth-updown-15m-a-again", "seconds_left": 80, "best_ask": 0.81, "volume24hr": 99},
    ]
    picked = pick_markets(rows, want=3, max_horizon=3600)
    assert [r["condition_id"] for r in picked] == ["soon", "next", "far"]
    assert looks_empty(1.0) is True
    assert looks_empty(0.97) is False
    assert "zec" not in DEFAULT_ASSETS
    assert asset_hit("sol-updown-15m-123", DEFAULT_ASSETS) is True
    assert asset_hit("zec-updown-15m-123", DEFAULT_ASSETS) is False
    assert asset_hit("bitcoin-up-or-down-august-26-2026-4am-et", ["btc"]) is True
    assert asset_hit("ethereum-up-or-down-august-26-2026-4am-et", ["eth"]) is True
    from app.universe import gamma_events_params, is_updown

    assert is_updown("btc-updown-15m-1") is True
    assert is_updown("bitcoin-up-or-down-august-26-2026-4am-et") is True
    assert is_updown("bitcoin-above-on-august-26-2026-5am-et") is False
    from datetime import datetime, timezone

    q = gamma_events_params("15M", limit=40, now=datetime(2026, 8, 26, 8, 17, tzinfo=timezone.utc), max_horizon=3600)
    assert q["end_date_min"] == "2026-08-26T08:17:00Z"
    assert q["end_date_max"] == "2026-08-26T09:17:00Z"
    assert q["order"] == "endDate"
    assert q["ascending"] == "true"


def _bt_end(seconds_from_epoch_offset: int = 0):
    from datetime import datetime, timezone

    end = datetime(2026, 8, 26, 12, 0, tzinfo=timezone.utc)
    ts = int(end.timestamp()) - seconds_from_epoch_offset
    return end.isoformat().replace("+00:00", "Z"), int(end.timestamp()), ts


def _px(t, outcome, side, price, size=80.0):
    return {"t": t, "outcome": outcome, "side": side, "price": price, "size": size}


def test_replay_taker_tail_has_positive_pnl():
    from app.replay import replay_market

    end, end_ts, _ = _bt_end()
    t = end_ts - 120
    stats = replay_market(
        [_px(t, "down", "BUY", 0.01), _px(t, "up", "BUY", 0.97)],
        end=end,
        slug="btc-updown-15m-taker",
    )
    assert stats["taker_n"] == 1
    assert stats["taker_pnl"] > 0
    assert stats["pnl"] > 0


def test_replay_maker_two_sided_and_expire_unfilled():
    from app.replay import live_replay_settings, replay_market

    end, end_ts, _ = _bt_end()
    q = end_ts - 40
    maker_on = live_replay_settings(maker_window_seconds=75)
    filled = replay_market(
        [
            _px(q, "up", "SELL", 0.50),
            _px(q, "down", "SELL", 0.49),
            _px(q + 2, "up", "BUY", 0.50),
            _px(q + 2, "down", "BUY", 0.49),
        ],
        end=end,
        slug="btc-updown-15m-maker",
        settings=maker_on,
    )
    assert filled["maker_quoted"] >= 1
    assert filled["maker_two_sided_n"] == 1
    assert filled["pnl"] > 0
    dead = replay_market(
        [_px(q, "up", "SELL", 0.50), _px(q, "down", "SELL", 0.49)],
        end=end,
        slug="btc-updown-15m-dead",
        settings=maker_on,
    )
    assert dead["maker_quoted"] >= 1
    assert dead["maker_expire_unfilled"] == 1
    assert dead["pnl"] == 0


def test_replay_one_sided_hedge_can_lose():
    from app.replay import live_replay_settings, replay_market

    end, end_ts, _ = _bt_end()
    q = end_ts - 40
    stats = replay_market(
        [
            _px(q, "up", "SELL", 0.39),
            _px(q, "down", "SELL", 0.5456),
            _px(q + 1, "down", "SELL", 0.20),
            _px(q + 2, "up", "BUY", 0.56, 80),
            _px(q + 3, "down", "BUY", 0.5456, 80),
        ],
        end=end,
        slug="sol-updown-15m-hedge",
        settings=live_replay_settings(maker_window_seconds=75, maker_max_skew=0.28),
    )
    assert stats["maker_hedge_n"] == 1
    assert stats["maker_hedge_pnl"] < 0
    assert stats["pnl"] < 0


def test_hunt_skips_maker_when_window_off():
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
        end=_late_end(30),
        maker_window_seconds=0,
        maker_min_edge=0.01,
    )
    assert setup is None


def test_risk_blocks_maker_when_window_off():
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
        seconds_left=30,
        maker_window=0,
    )
    assert d.ok is False
    assert d.reason == "maker_window_off"


def test_book_cache_applies_book_and_skips_stale():
    import time

    from app.ws_books import BookCache

    cache = BookCache()
    now = time.time() * 1000.0
    cache.apply_message(
        {
            "event_type": "book",
            "asset_id": "up",
            "timestamp": now,
            "asks": [{"price": "0.51", "size": "10"}],
            "bids": [{"price": "0.50", "size": "10"}],
        }
    )
    cache.apply_message(
        {
            "event_type": "book",
            "asset_id": "dn",
            "timestamp": now,
            "asks": [{"price": "0.50", "size": "10"}],
            "bids": [{"price": "0.49", "size": "10"}],
        }
    )
    pair = cache.pair("up", "dn", max_age_ms=2000)
    assert pair is not None
    assert pair["up"]["asks"][0].price == 0.51
    assert pair["down"]["bids"][0].price == 0.49
    cache.put("up", pair["up"]["asks"], pair["up"]["bids"], ts_ms=now - 5000, source="ws")
    assert cache.pair("up", "dn", max_age_ms=2000) is None
    assert cache.apply_message("PONG") == []


def test_replay_rev6_skips_toxic_maker():
    from app.replay import live_replay_settings, replay_market

    end, end_ts, _ = _bt_end()
    q = end_ts - 40
    stats = replay_market(
        [
            _px(q, "up", "SELL", 0.39),
            _px(q, "down", "SELL", 0.5456),
            _px(q + 1, "down", "SELL", 0.20),
            _px(q + 2, "up", "BUY", 0.56, 80),
            _px(q + 3, "down", "BUY", 0.5456, 80),
        ],
        end=end,
        slug="sol-updown-15m-hedge",
        settings=live_replay_settings(),
    )
    assert stats["maker_quoted"] == 0
    assert stats["maker_hedge_n"] == 0
    assert stats["pnl"] == 0


def test_rev6_boot_cancels_resting_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev6.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(20)
    st.add_inventory("c1", "btc", 5, 0)
    st.add_resting(
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
    st.patch_settings(strategy_rev=5, maker_window_seconds=75)
    cash_before = st.paper_state()["cash"]
    n = apply_strategy_rev(st)
    assert n == 1
    s = st.settings()
    assert s["strategy_rev"] == 6
    assert float(s["maker_window_seconds"]) == 0.0
    assert st.resting_open() == []
    after = st.paper_state()
    assert after["cash"] > cash_before
    assert after["starting"] == 500
    assert st.inventory_one("c1")["up"] == 5
    assert apply_strategy_rev(st) == 0


def test_health_reports_rev_and_ws(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Env
    from app.dashboard import create_app
    from app.runtime import Runtime

    st = Store(tmp_path / "h.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(dashboard_token="tok"))
    rt.ws_status = "connected"
    client = TestClient(create_app(rt))
    h = client.get("/health")
    assert h.status_code == 200
    body = h.json()
    assert body["ok"] is True
    assert body["strategy_rev"] == 6
    assert body["ws_status"] == "connected"
    assert body["live_trading"] is False
    assert float(body["maker_window_seconds"]) == 0.0
    state = client.get("/api/state?t=tok").json()
    assert state["ws_status"] == "connected"
    assert "ws_status" in state


def test_merge_deletes_empty_inventory_row(tmp_path):
    st = Store(tmp_path / "empty.sqlite")
    st.add_inventory("c1", "btc", 10, 10)
    st.merge_inventory("c1", 10)
    assert st.inventory() == []
    assert st.inventory_open() == []
    assert st.inventory_one("c1")["up"] == 0


def test_prune_empty_inventory_and_pos_hides_ghosts(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _log_text, _pos_text

    st = Store(tmp_path / "ghost.sqlite")
    st.ensure_paper(500)
    st._conn.execute(
        "INSERT INTO inventory(condition_id,slug,up,down,updated) VALUES(?,?,?,?,?)",
        ("c1", "btc-updown-15m-ghost", 0.0, 0.0, 1.0),
    )
    st._conn.commit()
    assert len(st.inventory()) == 1
    assert st.prune_empty_inventory() == 1
    assert st.inventory() == []
    rt = Runtime(st, Env())
    text = _pos_text(rt)
    assert "btc-updown-15m-ghost" not in text
    assert "無倉" in text
    st.add_trade(slug="btc", kind="maker", shares=10, up_price=0.5, down_price=0.49, net=0.0, mode="paper", status="paper_resting")
    st.add_trade(slug="btc", kind="maker", shares=10, up_price=0.5, down_price=0.49, net=0.0, mode="paper", status="paper_leg_fill")
    st.add_trade(slug="btc", kind="maker", shares=10, up_price=0.5, down_price=0.49, net=-10.8, mode="paper", status="paper_hedged")
    log = _log_text(rt)
    assert "paper_resting" not in log
    assert "paper_leg_fill" not in log
    assert "單邊對沖" in log
    assert "$-10.80" in log


def test_snapshot_hides_old_scans_and_noise_trades(tmp_path):
    from app.config import Env
    from app.runtime import Runtime

    st = Store(tmp_path / "snap.sqlite")
    st.ensure_paper(500)
    st.add_scan("old-maker", "maker", {"reason": "approved", "net": 1})
    st.add_trade(slug="btc", kind="maker", shares=5, up_price=0.5, down_price=0.49, net=0, mode="paper", status="paper_resting")
    st.add_trade(slug="btc", kind="maker", shares=5, up_price=0.5, down_price=0.49, net=-2, mode="paper", status="paper_hedged")
    st.add_event("info", "ws subscribed 32 tokens")
    rt = Runtime(st, Env())
    rt.started_at = 9e12
    snap = rt.snapshot()
    assert snap["scans"] == []
    assert [t["status"] for t in snap["trades"]] == ["paper_hedged"]
    assert snap["inventory"] == []
    assert snap["events"] == []


def test_book_cache_wanted_ignores_order():
    from app.ws_books import BookCache

    cache = BookCache()
    assert cache.set_wanted(["b", "a", "b"]) is True
    assert cache.wanted == ("a", "b")
    assert cache.set_wanted(["a", "b"]) is False


def test_dashboard_kill_cancels_resting(tmp_path):
    from fastapi.testclient import TestClient

    from app.config import Env
    from app.dashboard import create_app
    from app.runtime import Runtime

    st = Store(tmp_path / "kill.sqlite")
    st.ensure_paper(500)
    st.add_resting(
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
    rt = Runtime(st, Env(dashboard_token="tok"))
    client = TestClient(create_app(rt))
    out = client.post("/api/action/kill?t=tok")
    assert out.status_code == 200
    assert st.resting_open() == []
    assert st.settings()["killed"] is True
    assert st.settings()["live_trading"] is False



