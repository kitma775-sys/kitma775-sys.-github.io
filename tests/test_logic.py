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


def test_hunter_skips_fee_killed_underround():
    """ask_sum 0.98 at 0.72+0.26 is still −EV after 7% crypto fees. Do not lower min_edge."""
    setup = hunt(
        slug="eth",
        title="eth",
        condition_id="0x2",
        up_token="u",
        down_token="d",
        up_asks=_L((0.72, 20)),
        down_asks=_L((0.26, 50)),
        up_bids=_L((0.69, 10)),
        down_bids=_L((0.25, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
    )
    assert setup is None
    assert taker_net(1.0, 0.72, 0.26, 0.07) < 0


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


def test_fok_pair_fills_full_size_at_or_better():
    from app.hunter import Level
    from app.paper_sim import fok_pair

    ok = fok_pair(
        up_asks=[Level(0.81, 40), Level(0.82, 40)],
        down_asks=[Level(0.11, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        fee_rate=0.07,
    )
    assert ok.ok is True
    assert ok.reason == "fok_filled"
    assert ok.up_price <= 0.82
    assert ok.down_price <= 0.12
    assert ok.net > 0


def test_fok_pair_kills_short_size_and_worse_ask():
    from app.hunter import Level
    from app.paper_sim import fok_pair

    short = fok_pair(
        up_asks=[Level(0.82, 10)],
        down_asks=[Level(0.12, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        fee_rate=0.07,
    )
    assert short.ok is False
    assert short.reason == "fok_up_short"
    moved = fok_pair(
        up_asks=[Level(0.91, 40)],
        down_asks=[Level(0.12, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        fee_rate=0.07,
    )
    assert moved.ok is False
    assert moved.reason == "fok_up_short"


def test_fak_pair_fills_remaining_plus_ev_size():
    from app.paper_sim import fak_pair

    got = fak_pair(
        up_asks=[Level(0.82, 10)],
        down_asks=[Level(0.12, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        tail_confirm=0.90,
    )
    assert got.ok is True
    assert got.reason == "fok_fak"
    assert 9.9 <= got.shares <= 10.01
    assert got.net > 0


def test_fak_pair_kills_below_min_shares_and_price_through():
    from app.paper_sim import fak_pair

    short = fak_pair(
        up_asks=[Level(0.82, 3)],
        down_asks=[Level(0.12, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        tail_confirm=0.90,
    )
    assert short.ok is False
    assert short.reason == "fok_short"
    moved = fak_pair(
        up_asks=[Level(0.91, 40)],
        down_asks=[Level(0.12, 40)],
        shares=26.6,
        up_limit=0.82,
        down_limit=0.12,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        tail_confirm=0.90,
    )
    assert moved.ok is False
    assert moved.reason == "fok_short"


def test_confirm_pair_requotes_one_tick_worse_if_still_plus_ev():
    from app.hunter import Setup
    from app.paper_sim import confirm_pair

    setup = Setup(
        slug="sol",
        title="sol",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.82,
        down_price=0.12,
        shares=26.6,
        fillable=26.6,
        gross=0.06,
        fees=0.0,
        net=1.12,
        tail=False,
    )
    got = confirm_pair(
        setup=setup,
        up_asks=[Level(0.83, 40)],
        down_asks=[Level(0.12, 40)],
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        tail_confirm=0.90,
        max_usd=25,
    )
    assert got.ok is True
    assert got.reason == "fok_requote"
    assert got.up_price <= 0.8301
    assert got.down_price <= 0.1201
    assert got.net > 0
    assert got.shares >= 5


def test_confirm_pair_kills_minus_ev_delayed_book():
    from app.hunter import Setup
    from app.paper_sim import confirm_pair

    setup = Setup(
        slug="sol",
        title="sol",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.82,
        down_price=0.12,
        shares=26.6,
        fillable=26.6,
        gross=0.06,
        fees=0.0,
        net=1.12,
        tail=False,
    )
    got = confirm_pair(
        setup=setup,
        up_asks=[Level(0.91, 40)],
        down_asks=[Level(0.12, 40)],
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        tail_confirm=0.90,
        max_usd=25,
    )
    assert got.ok is False


def test_hunt_clips_plus_ev_prefix_instead_of_mixing_junk():
    from app.fees import taker_net
    from app.hunter import plus_ev_fill, walk

    up = _L((0.82, 10), (0.90, 200))
    down = _L((0.12, 10), (0.20, 200))
    filled_up, up_vwap = walk(up, 26.6, asks=True)
    filled_dn, dn_vwap = walk(down, 26.6, asks=True)
    assert min(filled_up, filled_dn) >= 26.6
    assert taker_net(26.6, up_vwap, dn_vwap, 0.07) <= 0
    clipped = plus_ev_fill(up, down, 26.6, 5, 0.02, 0.07, 0.90)
    assert clipped is not None
    assert clipped[0] < 20
    assert clipped[4] > 0
    setup = hunt(
        slug="sol",
        title="sol",
        condition_id="c",
        up_token="u",
        down_token="d",
        up_asks=up,
        down_asks=down,
        up_bids=_L((0.80, 10)),
        down_bids=_L((0.10, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
    )
    assert setup is not None
    assert setup.kind == "taker"
    assert setup.shares < 20
    assert setup.net > 0


def test_home_text_shows_fok_kill_tape(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import home_text

    st = Store(tmp_path / "fok.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    rt.last_loop = {
        "status": "ok",
        "markets": 10,
        "signals": 1,
        "fills": 0,
        "snapshot_signals": 1,
        "fok_kills": 1,
        "fok_fills": 0,
        "tape": {
            "n": 10,
            "min_ask_sum": 1.01,
            "max_taker_net": -0.01,
            "taker_fok": True,
            "snapshot_signals": 1,
            "fok_kills": 1,
            "fok_fills": 0,
            "nearest_s": 40,
            "nearest_slug": "sol-updown-5m",
            "slugs": ["sol-updown-5m"],
        },
    }
    text = home_text(rt)
    assert "FOK 影1/成0/殺1" in text
    assert "Rev 21" in text


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
    book = st.reset_today_pnl()
    assert abs(book["today_pnl"]) < 0.02
    assert rt.circuit_tripped() is False
    assert book["cash"] == st.paper_state()["cash"]


def test_favorite_budget_caps_stack():
    from app.runtime import favorite_budget
    from app.hunter import Setup
    from app.risk import approve

    assert favorite_budget(25, None) == 25
    assert favorite_budget(25, {"kind": "pair", "up": 10, "down": 10, "cost": 20}) == 25
    assert favorite_budget(25, {"kind": "favorite", "up": 0, "down": 0, "cost": 0}) == 25
    assert favorite_budget(25, {"kind": "favorite", "up": 25, "down": 0, "cost": 24.9}) == round(25 - 24.9, 6)
    assert favorite_budget(25, {"kind": "favorite", "up": 80, "down": 0, "cost": 25}) == 0
    setup = Setup(
        slug="xrp",
        title="xrp",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.99,
        down_price=0.0,
        shares=25.25,
        fillable=25.25,
        gross=0.01,
        fees=0.02,
        net=0.23,
        tail=True,
        extra={"strategy": "favorite", "leg": "up"},
    )
    blocked = approve(
        setup,
        stale_leg=0.02,
        tail_confirm=0.9,
        max_imbalance=40,
        inventory_up=25,
        inventory_down=0,
        daily_pnl=0,
        daily_loss_limit=50,
        open_markets=1,
        max_open_markets=8,
        killed=False,
        engine_running=True,
        auto_execute=True,
        seconds_left=20,
        cost=setup.cost,
        favorite_min_price=0.95,
        favorite_max_price=0.99,
        favorite_window_seconds=300,
        max_usd_per_trade=25,
        favorite_spent=24.9,
    )
    assert blocked.ok is False
    assert blocked.reason == "favorite_stack_cap"


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


def test_favorite_skips_ghost_99_01_book():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="zec",
        title="zec",
        condition_id="0xzec",
        up_token="u",
        down_token="d",
        up_asks=_L((0.99, 80)),
        down_asks=_L((0.01, 80)),
        up_bids=_L((0.01, 80)),
        down_bids=_L((0.01, 80)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert setup is None or not is_favorite_setup(setup)


def test_favorite_lifts_97_ask_in_last_30s():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert setup is not None
    assert is_favorite_setup(setup)
    assert setup.kind == "taker"
    assert setup.extra["leg"] == "up"
    assert 0.969 <= setup.up_price <= 0.971
    assert setup.down_price == 0.0
    assert setup.net > 0


def test_favorite_skips_outside_window():
    setup = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(90),
        strategy_mode="favorite",
        favorite_window_seconds=30,
        favorite_maker=False,
    )
    assert setup is None


def test_in_favorite_window_zero_is_full_session():
    from app.config import setting_num
    from app.hunter import in_favorite_window, parse_favorite_dir

    assert in_favorite_window(200, 0) is True
    assert in_favorite_window(200, 0.0) is True
    assert in_favorite_window(90, 45) is False
    assert in_favorite_window(20, 45) is True
    assert in_favorite_window(2, 0) is False
    assert in_favorite_window(None, 0) is False
    assert parse_favorite_dir("UP") == "up"
    assert parse_favorite_dir("Down") == "down"
    assert parse_favorite_dir("nope") == "auto"
    assert setting_num({"favorite_window_seconds": 0}, "favorite_window_seconds", 30.0) == 0.0
    from app.telegram_ui import FAVORITE_WINDOWS, _favorite_window_label

    assert 0 in FAVORITE_WINDOWS
    assert 60 in FAVORITE_WINDOWS
    assert _favorite_window_label({"favorite_window_seconds": 0}) == "全段（完場前3秒）"
    assert _favorite_window_label({"favorite_window_seconds": 45}) == "尾 45s"
    assert _favorite_window_label({}) == "尾 60s"


def test_favorite_full_session_lifts_mid_book():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(200),
        strategy_mode="favorite",
        favorite_window_seconds=0,
        favorite_maker=False,
    )
    assert setup is not None
    assert is_favorite_setup(setup)
    assert setup.extra["leg"] == "up"
    too_late = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(2),
        strategy_mode="favorite",
        favorite_window_seconds=0,
        favorite_maker=False,
    )
    assert too_late is None


def test_favorite_dir_up_ignores_richer_down():
    from app.hunter import is_favorite_setup

    up_book = dict(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_window_seconds=30,
        favorite_maker=False,
    )
    auto = hunt(**up_book, favorite_dir="auto")
    assert is_favorite_setup(auto)
    assert auto.extra["leg"] == "up"
    down_book = dict(up_book)
    down_book.update(
        up_asks=_L((0.04, 10)),
        down_asks=_L((0.98, 40)),
        up_bids=_L((0.03, 10)),
        down_bids=_L((0.97, 20)),
    )
    auto_dn = hunt(**down_book, favorite_dir="auto")
    assert is_favorite_setup(auto_dn)
    assert auto_dn.extra["leg"] == "down"
    up_only = hunt(**up_book, favorite_dir="up")
    assert is_favorite_setup(up_only)
    assert up_only.extra["leg"] == "up"
    down_only_on_up_book = hunt(**up_book, favorite_dir="down")
    assert down_only_on_up_book is None or not is_favorite_setup(down_only_on_up_book)


def test_favorite_skips_two_sided_97_99_book():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.99, 40)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.98, 20)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert setup is None or not is_favorite_setup(setup)


def test_favorite_skips_wide_spread_and_rich_other():
    from app.hunter import is_favorite_setup

    wide = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.80, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert wide is None or not is_favorite_setup(wide)
    flipping = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.22, 10)),
        up_bids=_L((0.96, 20)),
        down_bids=_L((0.20, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert flipping is None or not is_favorite_setup(flipping)


def test_favorite_skips_hanging_97_behind_cheap_ask():
    from app.hunter import favorite_lock_reason, favorite_ws_ok, is_favorite_setup

    assert favorite_ws_ok("connected", "ws") is True
    assert favorite_ws_ok("down", "ws") is False
    assert favorite_ws_ok("connected", "http") is False
    assert favorite_ws_ok("connected", "ws", {"source": "http"}, {"source": "ws"}) is False
    assert favorite_ws_ok("connected", "ws", {"source": "ws"}, {"source": "ws"}) is True
    hanging = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.63, 40), (0.97, 40)),
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.62, 20)),
        down_bids=_L((0.03, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert hanging is None or not is_favorite_setup(hanging)
    assert (
        favorite_lock_reason(
            asks=_L((0.63, 40), (0.97, 40)),
            bids=_L((0.62, 20)),
            other_asks=_L((0.04, 10)),
            min_px=0.97,
            max_px=0.98,
        )
        == "favorite_not_top"
    )


def test_favorite_skips_leftover_97_after_99_bid():
    from app.hunter import favorite_lock_reason, is_favorite_setup

    leftover = hunt(
        slug="eth",
        title="eth",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_asks=_L((0.97, 20), (0.99, 40)),
        down_asks=_L((0.01, 10)),
        up_bids=_L((0.99, 20)),
        down_bids=_L((0.005, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_maker=False,
    )
    assert leftover is None or not is_favorite_setup(leftover)
    assert (
        favorite_lock_reason(
            asks=_L((0.97, 20), (0.99, 40)),
            bids=_L((0.99, 20)),
            other_asks=_L((0.01, 10)),
            min_px=0.97,
            max_px=0.98,
        )
        == "favorite_through"
    )
    assert (
        favorite_lock_reason(
            asks=_L((0.97, 20)),
            bids=_L((0.98, 20)),
            other_asks=_L((0.02, 10)),
            min_px=0.97,
            max_px=0.98,
        )
        == "favorite_crossed"
    )
    assert (
        favorite_lock_reason(
            asks=_L((0.97, 40)),
            bids=_L((0.96, 20)),
            other_asks=_L((0.04, 10)),
            min_px=0.97,
            max_px=0.98,
        )
        is None
    )


def test_auto_prefers_complement_when_both_asks():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="sol",
        title="sol",
        condition_id="0xsol",
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
        maker_first=False,
        end=_late_end(20),
        strategy_mode="auto",
        favorite_maker=True,
    )
    assert setup is not None
    assert setup.kind == "taker"
    assert not is_favorite_setup(setup)
    assert setup.down_price > 0


def test_favorite_mode_skips_two_ask_complement():
    from app.hunter import is_favorite_setup

    kw = dict(
        slug="sol",
        title="sol",
        condition_id="0xsol",
        up_token="u",
        down_token="d",
        up_asks=_L((0.82, 80)),
        down_asks=_L((0.14, 80)),
        up_bids=_L((0.81, 10)),
        down_bids=_L((0.13, 10)),
        max_usd=5,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        favorite_min_price=0.90,
        favorite_max_price=0.98,
        favorite_maker=False,
    )
    fav = hunt(**kw, strategy_mode="favorite")
    assert fav is None
    auto = hunt(**kw, strategy_mode="auto")
    if auto is not None:
        assert not is_favorite_setup(auto)
        assert auto.kind == "taker"
        assert auto.down_price > 0


def test_favorite_maker_rests_at_min_when_ask_pulled():
    from app.hunter import is_favorite_setup

    setup = hunt(
        slug="btc",
        title="btc",
        condition_id="0xbtc",
        up_token="u",
        down_token="d",
        up_asks=[],
        down_asks=_L((0.04, 10)),
        up_bids=_L((0.97, 40)),
        down_bids=_L((0.02, 10)),
        max_usd=25,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="favorite",
        favorite_min_price=0.95,
        favorite_max_price=0.99,
        favorite_maker=True,
    )
    assert setup is not None
    assert is_favorite_setup(setup)
    assert setup.kind == "maker"
    assert setup.extra["leg"] == "up"
    assert abs(setup.up_price - 0.95) < 1e-9


def test_favorite_approve_allows_naked_and_rescue_skips(tmp_path):
    from app.hunter import Setup, is_favorite_setup
    from app.risk import approve

    setup = Setup(
        slug="eth",
        title="eth",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.03,
        fees=0.02,
        net=0.28,
        tail=True,
        extra={"strategy": "favorite", "leg": "up", "fee_rate": 0.07},
    )
    assert is_favorite_setup(setup)
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
        seconds_left=20,
        favorite_min_price=0.95,
        favorite_max_price=0.99,
        favorite_window_seconds=30,
    )
    assert ok.ok is True
    wrong = approve(
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
        seconds_left=200,
        favorite_min_price=0.95,
        favorite_max_price=0.99,
        favorite_window_seconds=0,
        favorite_dir="down",
    )
    assert wrong.ok is False
    assert wrong.reason == "favorite_wrong_dir"
    full = approve(
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
        seconds_left=200,
        favorite_min_price=0.95,
        favorite_max_price=0.99,
        favorite_window_seconds=0,
        favorite_dir="up",
    )
    assert full.ok is True
    st = Store(tmp_path / "fav.sqlite")
    st.ensure_paper(500)
    st.add_inventory("c1", "eth", 10, 0, kind="favorite", cost=9.72)
    paper = st.paper_state()
    assert paper["inventory_value"] == 9.72
    assert st.inventory_one("c1")["kind"] == "favorite"


def test_fak_one_clips_band():
    from app.paper_sim import fak_one

    got = fak_one(
        asks=[Level(0.97, 8), Level(0.99, 40)],
        shares=25,
        limit=0.97,
        min_shares=5,
        min_px=0.95,
        max_px=0.99,
        fee_rate=0.07,
    )
    assert got.ok is True
    assert 7.9 <= got.shares <= 8.01
    assert got.up_price <= 0.9701


def test_paper_execute_favorite_is_one_leg():
    from app.broker import paper_execute
    from app.hunter import Setup

    setup = Setup(
        slug="eth",
        title="eth",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.03,
        fees=0.02,
        net=0.28,
        tail=True,
        extra={"strategy": "favorite", "leg": "up", "fee_rate": 0.07},
    )
    result = paper_execute(setup)
    assert result.ok is True
    assert result.status == "paper_filled"
    assert result.payload["down_price"] == 0.0
    assert result.payload["up_price"] == 0.97
    # Cost is 10*0.97 + taker fee, not a free 0¢ down leg.
    assert 9.70 < float(result.payload["cost"]) < 9.85


def test_favorite_maker_consume_then_complete_does_not_double_release(tmp_path):
    st = Store(tmp_path / "favrest.sqlite")
    st.ensure_paper(500)
    row = st.add_resting(
        slug="btc",
        condition_id="c1",
        title="btc",
        up_token="u",
        down_token="d",
        shares=10,
        up_price=0.95,
        down_price=0.0,
        net=0.50,
        payload={"strategy": "favorite", "leg": "up"},
    )
    after_rest = st.paper_state()
    assert abs(after_rest["reserved"] - 9.5) < 1e-9
    assert abs(after_rest["cash"] - 490.5) < 1e-9
    filled = st.fill_resting_leg(row["id"], "up")
    assert filled["up_filled"] == 1
    st.complete_resting(row["id"], "favorite_hit")
    paper = st.paper_state()
    assert paper["reserved"] == 0
    assert abs(paper["cash"] - 490.5) < 1e-9
    assert abs(paper["inventory_value"] - 9.5) < 1e-9
    assert abs(paper["equity"] - 500) < 1e-9
    inv = st.inventory_one("c1")
    assert inv["kind"] == "favorite"
    assert inv["up"] == 10
    assert inv["down"] == 0


def test_favorite_taker_replaces_rest_and_http_due():
    from app.hunter import Setup
    from app.runtime import favorite_taker_replaces_rest, http_book_due

    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.99,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.01,
        fees=0.01,
        net=0.06,
        tail=True,
        extra={"strategy": "favorite", "leg": "up"},
    )
    rest = {"payload": {"strategy": "favorite", "leg": "up"}}
    assert favorite_taker_replaces_rest(setup, rest) is True
    setup.kind = "maker"
    assert favorite_taker_replaces_rest(setup, rest) is False
    setup.kind = "taker"
    assert favorite_taker_replaces_rest(setup, {"payload": {}}) is False
    assert http_book_due(missing=False, flicker=False) is False
    assert http_book_due(missing=True, flicker=False) is True
    assert http_book_due(missing=False, flicker=True) is True


def test_favorite_replace_rest_releases_cash(tmp_path):
    st = Store(tmp_path / "lift.sqlite")
    st.ensure_paper(500)
    row = st.add_resting(
        slug="btc",
        condition_id="c1",
        title="btc",
        up_token="u",
        down_token="d",
        shares=10,
        up_price=0.97,
        down_price=0.0,
        net=0.30,
        payload={"strategy": "favorite", "leg": "up"},
    )
    assert st.has_open_resting("btc")
    st.cancel_resting(row["id"], "favorite_lift")
    paper = st.paper_state()
    assert paper["reserved"] == 0
    assert abs(paper["cash"] - 500) < 1e-9
    assert st.has_open_resting("btc") is False


def test_telegram_clip_uses_tg_max():
    from app.telegram_ui import TG_MAX, _clip

    assert TG_MAX >= 1000
    assert _clip("ok") == "ok"
    long = "x" * (TG_MAX + 50)
    clipped = _clip(long)
    assert len(clipped) <= TG_MAX
    assert "過長" in clipped


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
    assert looks_empty(0.99) is True
    assert looks_empty(0.99, 40) is False
    assert looks_empty(1.0, 40) is True
    tailed = pick_markets(
        [
            {"condition_id": "empty", "slug": "zec-updown-15m", "seconds_left": 40, "best_ask": 1.0, "volume24hr": 1},
            {"condition_id": "tail99", "slug": "btc-updown-15m-tail", "seconds_left": 40, "best_ask": 0.99, "volume24hr": 9},
        ],
        want=3,
        max_horizon=3600,
    )
    assert [r["condition_id"] for r in tailed] == ["tail99"]
    assert "zec" not in DEFAULT_ASSETS
    assert asset_hit("sol-updown-15m-123", DEFAULT_ASSETS) is True
    assert asset_hit("zec-updown-15m-123", DEFAULT_ASSETS) is False
    assert asset_hit("bitcoin-up-or-down-august-26-2026-4am-et", ["btc"]) is True
    assert asset_hit("ethereum-up-or-down-august-26-2026-4am-et", ["eth"]) is True
    from app.universe import gamma_events_params, is_updown

    assert is_updown("btc-updown-15m-1") is True
    assert is_updown("btc-updown-5m-1") is True
    assert is_updown("bitcoin-up-or-down-august-26-2026-4am-et") is True
    assert is_updown("bitcoin-above-on-august-26-2026-5am-et") is False
    from datetime import datetime, timezone

    q = gamma_events_params("15M", limit=40, now=datetime(2026, 8, 26, 8, 17, tzinfo=timezone.utc), max_horizon=3600)
    assert q["end_date_min"] == "2026-08-26T08:17:00Z"
    assert q["end_date_max"] == "2026-08-26T09:17:00Z"
    assert q["order"] == "endDate"
    assert q["ascending"] == "true"
    from app.universe import DEFAULT_TAGS, tag_horizon

    assert DEFAULT_TAGS[0] == "5M"
    assert tag_horizon("5M", 3600) == 900
    assert tag_horizon("15M", 3600) == 1800
    assert tag_horizon("1H", 3600) == 3600
    assert tag_horizon("15M", 600) == 600

    crowded = pick_markets(
        [
            {"condition_id": "penny", "slug": "doge-updown-5m-x", "seconds_left": 200, "best_ask": 0.02, "volume24hr": 99},
            {"condition_id": "twosided", "slug": "eth-updown-15m-y", "seconds_left": 500, "best_ask": 0.51, "volume24hr": 1},
            {"condition_id": "flicker", "slug": "btc-updown-5m-z", "seconds_left": 90, "best_ask": 0.03, "volume24hr": 1},
        ],
        want=2,
        max_horizon=3600,
    )
    assert [r["condition_id"] for r in crowded] == ["flicker", "twosided"]


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
    assert cache.pair("up", "dn", max_age_ms=60000) is not None
    assert cache.apply_message("PONG") == []
    cache.apply_message(
        {
            "event_type": "best_bid_ask",
            "asset_id": "dn",
            "timestamp": now,
            "best_ask": "0",
            "best_bid": "0.49",
        }
    )
    gone = cache.pair("up", "dn", max_age_ms=60000)
    assert gone is not None
    assert gone["down"]["asks"] == []
    assert gone["down"]["bids"][0].price == 0.49


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
    assert s["strategy_rev"] == 21
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "favorite"
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_window_seconds"]) == 60.0
    assert s.get("favorite_dir") == "auto"
    assert s.get("favorite_maker") is False
    assert float(s["maker_window_seconds"]) == 0.0
    assert float(s["max_book_age_ms"]) == 60000.0
    assert s["tags"] == ["5M", "15M", "1H"]
    assert int(s["scan_limit"]) == 24
    assert s.get("taker_fok") is True
    assert st.resting_open() == []
    after = st.paper_state()
    assert after["cash"] > cash_before
    assert after["starting"] == 500
    assert st.inventory_one("c1")["up"] == 5
    assert apply_strategy_rev(st) == 0


def test_rev13_widens_window_without_paper_reset(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev13.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(80)
    st.patch_settings(
        strategy_rev=12,
        favorite_window_seconds=45,
        favorite_min_price=0.97,
        favorite_max_price=0.99,
        favorite_dir="auto",
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "favorite"
    assert float(s["favorite_window_seconds"]) == 60
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["favorite_dir"] == "auto"
    assert s.get("favorite_maker") is False
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]


def test_rev15_opens_90_99_keeps_window_and_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev15.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=14,
        favorite_min_price=0.96,
        favorite_max_price=0.98,
        favorite_window_seconds=180,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "favorite"
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_window_seconds"]) == 60
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]
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
    assert body["strategy_rev"] == 21
    assert body.get("auto_redeem") is True
    assert body.get("strategy_mode") == "favorite"
    assert float(body.get("max_usd_per_trade") or 0) == 5.0
    assert float(body.get("favorite_min_price") or 0) == 0.97
    assert float(body.get("favorite_max_price") or 0) == 0.98
    assert body["taker_fok"] is True
    assert body["ws_status"] == "connected"
    assert body["live_trading"] is False
    assert float(body["maker_window_seconds"]) == 0.0
    assert body.get("favorite_maker") is False
    assert body.get("favorite_dir") == "auto"
    assert float(body.get("favorite_window_seconds") or 0) == 60.0
    assert body.get("force_paper") is False
    assert body.get("favorite_window_label") == "尾 60s"
    assert "engine_running" in body
    assert body.get("circuit") is False
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


def test_already_redeemed_helper():
    from app.broker import already_redeemed

    assert already_redeemed("UserInputError: You have no positions")
    assert already_redeemed("nothing to redeem")
    assert not already_redeemed("nonce too low")
    assert not already_redeemed("")


def test_is_redeemable_market_waits_for_decided_prices():
    from datetime import datetime, timedelta, timezone

    from app.rescue import is_redeemable_market

    past = (datetime.now(timezone.utc) - timedelta(seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert is_redeemable_market(
        {"closed": True, "markets": [{"closed": True, "outcomePrices": ["1", "0"]}]}
    ) == (1.0, 0.0)
    assert is_redeemable_market(
        {"closed": True, "markets": [{"closed": True, "outcomePrices": ["0.62", "0.38"]}]}
    ) is None
    assert is_redeemable_market(
        {"closed": False, "endDate": future, "markets": [{"outcomePrices": ["0.99", "0.01"]}]}
    ) is None
    assert is_redeemable_market(
        {"closed": False, "endDate": past, "markets": [{"endDate": past, "outcomePrices": ["0", "1"]}]}
    ) == (0.0, 1.0)
    assert is_redeemable_market(
        {"closed": True, "markets": [{"closed": True, "outcomePrices": ["0.50", "0.50"]}]}
    ) is None
    assert is_redeemable_market(
        {
            "closed": True,
            "markets": [
                {"closed": True, "umaResolutionStatus": "resolved", "outcomePrices": ["0.5", "0.5"]}
            ],
        }
    ) == (0.5, 0.5)
    assert is_redeemable_market(
        {
            "closed": False,
            "endDate": past,
            "markets": [{"endDate": past, "outcomePrices": ["0.515", "0.485"]}],
        }
    ) is None
    assert is_redeemable_market(
        {"closed": True, "markets": [{"closed": True, "outcomePrices": ["0.999", "0.001"]}]}
    ) == (1.0, 0.0)
    assert is_redeemable_market(None) is None


class _FakeGamma:
    def __init__(self, events: dict):
        self.events = events

    async def event_by_slug(self, slug: str):
        return self.events.get(slug)


def _closed_up_win():
    return {"closed": True, "markets": [{"closed": True, "outcomePrices": ["1", "0"]}]}


def test_paper_redeem_credits_winner_and_clears_inventory(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    st = Store(tmp_path / "redeem-win.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 1
    after = st.paper_state()
    assert after["cash"] == 502.0
    assert st.inventory_open() == []
    trades = st.recent_trades(5)
    assert trades[0]["status"] == "paper_settled"
    assert (trades[0].get("payload") or {}).get("redeem") is True


def test_paper_redeem_skips_ended_mid_quotes(tmp_path):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    past = (datetime.now(timezone.utc) - timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    st = Store(tmp_path / "redeem-mid.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(10.02)
    st.add_inventory("c1", "btc-updown", 0.0, 10.3093, kind="favorite", cost=10.02)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma(
        {
            "btc-updown": {
                "closed": False,
                "endDate": past,
                "markets": [{"endDate": past, "outcomePrices": ["0.515", "0.485"]}],
            }
        }
    )
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 0
    assert st.inventory_one("c1")["down"] == 10.3093
    assert abs(st.paper_state()["cash"] - 489.98) < 0.02


def test_paper_redeem_loser_clears_without_credit(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    st = Store(tmp_path / "redeem-lose.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma(
        {"btc-updown": {"closed": True, "markets": [{"closed": True, "outcomePrices": ["0", "1"]}]}}
    )
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 1
    after = st.paper_state()
    assert after["cash"] == 482.0
    assert st.inventory_open() == []


def test_auto_redeem_off_skips(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    st = Store(tmp_path / "redeem-off.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    st.patch_settings(auto_redeem=False)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 0
    assert st.inventory_one("c1")["up"] == 20.0
    assert st.paper_state()["cash"] == 482.0


def test_redeem_failure_keeps_inventory(tmp_path):
    import asyncio

    from app.broker import FillResult, PaperBroker
    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    class Boom(PaperBroker):
        async def redeem(self, condition_id: str) -> FillResult:
            return FillResult(False, "redeem_error", "paper", "boom", {})

    st = Store(tmp_path / "redeem-fail.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    rt._broker = Boom()
    rt._broker_mode = "paper"
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 0
    assert st.inventory_one("c1")["up"] == 20.0
    assert st.paper_state()["cash"] == 482.0


def test_paused_engine_still_redeems(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _refresh_universe

    st = Store(tmp_path / "redeem-pause.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    st.patch_settings(engine_running=False, auto_redeem=True)
    rt = Runtime(st, Env())
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    asyncio.run(_refresh_universe(rt))
    assert rt.last_loop["status"] == "paused"
    assert rt.last_loop["redeemed"] == 1
    assert st.inventory_open() == []
    assert st.paper_state()["cash"] == 502.0


def test_live_redeem_does_not_credit_paper(tmp_path):
    import asyncio

    from app.broker import FillResult, LiveBroker
    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    class FakeLive(LiveBroker):
        def __init__(self):
            super().__init__("0xabc")

        async def redeem(self, condition_id: str) -> FillResult:
            return FillResult(True, "redeemed", "live", "ok", {"condition_id": condition_id})

        async def list_redeemable(self) -> list[dict]:
            return []

    st = Store(tmp_path / "redeem-live.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18.0)
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite", cost=18.0)
    st.patch_settings(live_trading=True, auto_redeem=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    rt._broker = FakeLive()
    rt._broker_mode = "live"
    before = st.paper_state()["cash"]
    n = asyncio.run(_redeem_resolved(rt))
    assert rt.mode() == "live"
    assert n == 1
    assert st.inventory_open() == []
    assert st.paper_state()["cash"] == before
    assert st.recent_trades(1)[0]["status"] == "redeemed"


def test_rev16_enables_auto_redeem_keeps_band_and_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev16.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=15,
        favorite_min_price=0.90,
        favorite_max_price=0.98,
        favorite_window_seconds=180,
        auto_redeem=False,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "favorite"
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_window_seconds"]) == 60
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev17_favorite_only_five_usd_keeps_window_and_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev17.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=16,
        strategy_mode="auto",
        favorite_min_price=0.90,
        favorite_max_price=0.99,
        favorite_window_seconds=180,
        max_usd_per_trade=25.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert s.get("strategy_mode") == "favorite"
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["maker_window_seconds"]) == 0.0
    assert float(s["favorite_window_seconds"]) == 60
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev18_pins_180s_window_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev18.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=17,
        strategy_mode="favorite",
        favorite_min_price=0.90,
        favorite_max_price=0.98,
        favorite_window_seconds=0,
        max_usd_per_trade=5.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert float(s["favorite_window_seconds"]) == 60
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_min_price"]) == 0.97
    assert s.get("favorite_maker") is False
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev19_waits_for_binary_redeem_pins_97_98_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev19.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=18,
        strategy_mode="favorite",
        favorite_min_price=0.90,
        favorite_max_price=0.98,
        favorite_window_seconds=180,
        favorite_maker=True,
        max_usd_per_trade=10.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s.get("favorite_maker") is False
    assert float(s["favorite_window_seconds"]) == 60
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev20_pins_60s_locked_favorite_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev
    from app.hunter import favorite_window_key
    from app.runtime import favorite_same_window_open
    from app.config import Env
    from app.runtime import Runtime

    assert favorite_window_key("btc-updown-5m-1787981100") == "updown-5m-1787981100"
    assert favorite_window_key("eth-updown-5m-1787981100") == "updown-5m-1787981100"

    st = Store(tmp_path / "rev20.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=19,
        strategy_mode="favorite",
        favorite_min_price=0.97,
        favorite_max_price=0.98,
        favorite_window_seconds=180,
        favorite_maker=False,
        max_usd_per_trade=5.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert float(s["favorite_window_seconds"]) == 60
    assert float(s["favorite_min_price"]) == 0.97
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]

    st.add_inventory("c-btc", "btc-updown-5m-1787981100", 5.0, 0.0, kind="favorite", cost=5.0)
    rt = Runtime(st, Env())
    assert favorite_same_window_open(rt, "eth-updown-5m-1787981100") is True
    assert favorite_same_window_open(rt, "btc-updown-5m-1787981100") is False
    assert favorite_same_window_open(rt, "eth-updown-5m-1787981400") is False
    assert apply_strategy_rev(st) == 0


def test_rev21_pins_five_usd_and_kills_down_requote(tmp_path):
    from app.config import Env
    from app.hunter import Setup
    from app.main import apply_strategy_rev
    from app.runtime import Runtime, _confirm_favorite

    st = Store(tmp_path / "rev21.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=20,
        strategy_mode="favorite",
        favorite_min_price=0.97,
        favorite_max_price=0.98,
        favorite_window_seconds=60,
        favorite_maker=False,
        max_usd_per_trade=10.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 21
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_window_seconds"]) == 60
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0

    rt = Runtime(st, Env())
    setup = Setup(
        slug="eth-updown-5m-1",
        title="eth",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.98,
        down_price=0.0,
        shares=5.1,
        fillable=5.1,
        gross=0.02,
        fees=0.01,
        net=0.09,
        tail=True,
        end=_late_end(40),
        extra={"strategy": "favorite", "leg": "up", "favorite_px": 0.98, "fee_rate": 0.07},
    )
    ev = {
        "slug": setup.slug,
        "title": "eth",
        "condition_id": "c",
        "up_token": "u",
        "down_token": "d",
        "end": setup.end,
        "min_size": 5,
        "fee_rate": 0.07,
    }
    through = _confirm_favorite(
        rt,
        ev,
        setup,
        {"asks": _L((0.97, 20)), "bids": _L((0.99, 20))},
        {"asks": _L((0.01, 20)), "bids": _L((0.005, 20))},
        st.settings(),
        0.07,
        st.paper_state(),
    )
    assert through.ok is False
    assert through.reason in {"favorite_through", "favorite_crossed"}

    up_req = Setup(
        slug="btc-updown-5m-1",
        title="btc",
        condition_id="c2",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.97,
        down_price=0.0,
        shares=5.15,
        fillable=5.15,
        gross=0.03,
        fees=0.02,
        net=0.14,
        tail=True,
        end=_late_end(40),
        extra={"strategy": "favorite", "leg": "up", "favorite_px": 0.97, "fee_rate": 0.07},
    )
    ev2 = {**ev, "slug": up_req.slug, "condition_id": "c2"}
    requote = _confirm_favorite(
        rt,
        ev2,
        up_req,
        {"asks": _L((0.98, 40)), "bids": _L((0.97, 20))},
        {"asks": _L((0.02, 20)), "bids": _L((0.01, 20))},
        st.settings(),
        0.07,
        st.paper_state(),
    )
    assert requote.ok is True
    assert requote.reason == "fok_requote"
    assert 0.979 <= requote.up_price <= 0.981

    # Snapshot 0.98 but delayed book is a real 97 lock: FAK the better ask.
    better = _confirm_favorite(
        rt,
        ev,
        setup,
        {"asks": _L((0.97, 40)), "bids": _L((0.96, 20))},
        {"asks": _L((0.03, 20)), "bids": _L((0.02, 20))},
        st.settings(),
        0.07,
        st.paper_state(),
    )
    assert better.ok is True
    assert better.reason in {"fok_filled", "fok_fak"}
    assert 0.969 <= better.up_price <= 0.971


def test_hedges_24h_ignores_settles(tmp_path):
    st = Store(tmp_path / "hedge-stat.sqlite")
    st.add_trade(slug="a", kind="settle", shares=5, up_price=1, down_price=0, net=0.2, mode="paper", status="paper_settled")
    st.add_trade(slug="b", kind="hedge", shares=5, up_price=0.5, down_price=0.49, net=-0.1, mode="paper", status="paper_hedged")
    st.add_trade(slug="c", kind="dump", shares=5, up_price=0.4, down_price=0, net=-2, mode="paper", status="paper_dumped")
    got = st.stats()
    assert got["hedges_24h"] == 2
    assert got["trades_24h"] == 0


def test_format_leg_prices_one_leg():
    from app.config import format_leg_prices

    assert format_leg_prices(0.9, 0.0, leg="up") == "Up 0.9"
    assert format_leg_prices(0.0, 0.96, leg="down") == "Down 0.96"
    assert format_leg_prices(0.5, 0.49) == "0.5+0.49"


def test_live_favorite_inventory_does_not_inflate_paper(tmp_path):
    st = Store(tmp_path / "live-inv.sqlite")
    st.ensure_paper(500)
    before = st.paper_state()["equity"]
    st.add_inventory("c1", "btc-updown", 5.0, 0.0, kind="favorite_live", cost=4.5)
    after = st.paper_state()
    assert after["equity"] == before
    assert after["inventory_value"] == 0
    assert st.inventory_open()[0]["kind"] == "favorite_live"


def test_today_pnl_includes_redeemed_and_settled(tmp_path):
    st = Store(tmp_path / "pnl.sqlite")
    st.add_trade(slug="a", kind="settle", shares=5, up_price=1, down_price=0, net=-4.5, mode="live", status="redeemed")
    st.add_trade(slug="b", kind="settle", shares=5, up_price=1, down_price=0, net=0.5, mode="paper", status="paper_settled")
    assert abs(st.today_pnl() - (-4.0)) < 1e-9


class SimpleOrder:
    def __init__(self, *, ok, status, order_id=None, message="", code=None):
        self.ok = ok
        self.status = status
        self.order_id = order_id
        self.message = message
        self.code = code


class SimpleCancel:
    canceled = ()


class FakeQuery:
    def __init__(self):
        self.answered = None
        self.message = None

    async def answer(self, *args, **kwargs):
        self.answered = {"args": args, "kwargs": kwargs}

    async def edit_message_text(self, *args, **kwargs):
        return None


def test_live_taker_uses_market_fak_not_limit():
    import asyncio

    from app.broker import LiveBroker
    from app.hunter import Setup

    calls = []

    class FakeClient:
        async def place_market_order(self, **kw):
            calls.append(("market", kw))
            return SimpleOrder(ok=True, status="matched", order_id="o1")

        async def place_limit_order(self, **kw):
            calls.append(("limit", kw))
            raise AssertionError("taker must not rest a GTC bid")

    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.9,
        down_price=0.0,
        shares=5.5,
        fillable=5.5,
        gross=0.1,
        fees=0.006,
        net=0.5,
        tail=True,
        extra={"strategy": "favorite", "leg": "up"},
    )
    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_pair(setup))
    assert result.ok is True
    assert result.status == "filled"
    assert calls[0][0] == "market"
    assert calls[0][1]["order_type"] == "FAK"
    assert "limit" not in [c[0] for c in calls]


def test_live_taker_unmatched_live_is_cancelled():
    import asyncio

    from app.broker import LiveBroker
    from app.hunter import Setup

    calls = []

    class FakeClient:
        async def place_market_order(self, **kw):
            calls.append(("market", kw))
            return SimpleOrder(ok=True, status="live", order_id="resting-1")

        async def cancel_order(self, **kw):
            calls.append(("cancel", kw))
            return SimpleOrder(ok=True, status="cancelled", order_id=kw.get("order_id"))

        async def cancel_all(self):
            calls.append(("cancel_all", {}))
            return SimpleCancel()

    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.9,
        down_price=0.0,
        shares=5.5,
        fillable=5.5,
        gross=0.1,
        fees=0.006,
        net=0.5,
        tail=True,
        extra={"strategy": "favorite", "leg": "up"},
    )
    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_pair(setup))
    assert result.ok is False
    assert any(c[0] == "cancel" for c in calls)


def test_live2_blocked_when_force_paper(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _handle_callback

    st = Store(tmp_path / "live2.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=True, private_key="0xabc"))
    q = FakeQuery()
    asyncio.run(_handle_callback(rt, q, "live2"))
    assert st.settings()["live_trading"] is False
    assert q.answered



