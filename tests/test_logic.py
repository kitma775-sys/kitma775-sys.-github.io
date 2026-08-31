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


def test_hunt_twap_mode_skips_complement_hole():
    setup = hunt(
        slug="btc-updown-5m-1000",
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
        maker_first=False,
        strategy_mode="twap",
        twap_snap=None,
    )
    assert setup is None
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
    assert round(mid["realized_pnl"], 2) == 0.0

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
    assert "Rev 39" in text
    assert "預熱" in text
    assert "TWAP" in text
    assert "280" in text
    assert "唔做 YES+NO 互補" in text
    assert "互補洞仍然會先吃" not in text


def test_telegram_dashboard_url_button(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import dashboard_open_url, home_kb, home_text

    st = Store(tmp_path / "dashbtn.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    assert dashboard_open_url(rt) is None
    assert all(not getattr(btn, "url", None) for row in home_kb(rt).inline_keyboard for btn in row)

    rt = Runtime(
        st,
        Env(dashboard_token="tok+/=x", dashboard_public_url="https://surf-arb.zeabur.app"),
    )
    url = dashboard_open_url(rt)
    assert url == "https://surf-arb.zeabur.app/?t=tok%2B%2F%3Dx"
    buttons = [btn for row in home_kb(rt).inline_keyboard for btn in row]
    dash = next(b for b in buttons if b.text == "🖥 開 Dashboard")
    assert dash.url == url
    assert "tok+/=x" not in home_text(rt)
    assert "開 Dashboard" not in home_text(rt)


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
    assert plan.floor_px == 0.60


def test_walk_dump_uses_vwap_and_last_level_floor():
    from app.rescue import plan_rescue, walk_dump

    filled, vwap, floor = walk_dump(_L((0.50, 5), (0.40, 10)), 10)
    assert filled == 10
    assert abs(vwap - 0.45) < 1e-9
    assert floor == 0.40
    plan = plan_rescue(
        filled_px=0.50,
        shares=10,
        other_asks=[],
        filled_bids=_L((0.50, 5), (0.40, 10)),
        fee_rate=0.0,
    )
    assert plan.action == "dump"
    assert plan.price == 0.45
    assert plan.floor_px == 0.40
    assert abs(plan.cash_out - 4.5) < 1e-9


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


def test_complement_mode_skips_locked_favorite():
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
        max_usd=5,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(20),
        strategy_mode="complement",
        favorite_maker=False,
        maker_window_seconds=0,
    )
    assert setup is None or not is_favorite_setup(setup)
    if setup is not None:
        assert setup.down_price > 0


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
    from app.config import favorite_window_label

    assert favorite_window_label(0) == "全段（完場前3秒）"
    assert favorite_window_label(45) == "尾 45s"
    assert favorite_window_label(None) == "尾 60s"


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
    assert "盤口 10 盤" in text
    assert "最近 42s btc-updown-15m" in text
    assert "掃 eth-updown-15m-a, sol-updown-15m-b" in text
    assert "ask合" not in text
    assert "taker淨" not in text
    assert "掛單缺口" not in text


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
    assert s["strategy_rev"] == 39
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "twap"
    assert float(s["favorite_min_price"]) == 0.97
    assert float(s["favorite_max_price"]) == 0.98
    assert float(s["max_usd_per_trade"]) == 5.0
    assert float(s["favorite_window_seconds"]) == 60.0
    assert s.get("favorite_dir") == "auto"
    assert s.get("favorite_maker") is False
    assert float(s["maker_window_seconds"]) == 0.0
    assert float(s["max_book_age_ms"]) == 60000.0
    assert s["tags"] == ["5M"]
    assert int(s["scan_limit"]) == 40
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
    assert s["strategy_rev"] == 39
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "twap"
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
    assert s["strategy_rev"] == 39
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "twap"
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
    assert body["strategy_rev"] == 39
    assert body.get("auto_redeem") is True
    assert body.get("strategy_mode") == "twap"
    assert float(body.get("max_usd_per_trade") or 0) == 5.0
    assert "favorite_min_price" not in body
    assert "favorite_window_label" not in body
    assert "maker_window_seconds" not in body
    assert body["taker_fok"] is True
    assert body["ws_status"] == "connected"
    assert body["live_trading"] is False
    assert body.get("force_paper") is False
    assert "no_key" in (body.get("live_blockers") or [])
    assert body.get("chainlink_status") == "off"
    assert body.get("twap_min_lead_bps") in (6, 6.0)
    assert float(body.get("twap_max_left") or 0) == 280.0
    assert body.get("twap_horizons") == ["5m"]
    assert float(body.get("clob_rtt_ms") or 0) == 150.0
    assert "clob_ws_wanted_n" in body
    assert "clob_ws_slugs" in body
    assert body.get("twap_ptb_n") == 0
    assert body.get("last_ws_error") in (None, "")
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
    assert round(after["realized_pnl"], 2) == 2.0
    assert round(after["total_pnl"], 2) == 2.0
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
    assert round(after["realized_pnl"], 2) == -18.0
    assert round(after["total_pnl"], 2) == -18.0
    assert st.inventory_open() == []


def test_paper_dump_records_bid_vwap_and_realized(tmp_path):
    import asyncio

    from app.config import Env
    from app.rescue import RescuePlan
    from app.runtime import Runtime, _apply_rescue

    st = Store(tmp_path / "dump-px.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(10.00001)
    st.add_inventory("c1", "btc-updown-5m-1", 0.0, 19.3237, kind="twap", cost=10.00001)
    rt = Runtime(st, Env())
    plan = RescuePlan(
        action="dump",
        price=0.49,
        fees=0.33803,
        cash_out=9.130583,
        pnl=-0.869427,
        reason="dump_bid",
        floor_px=0.49,
    )
    row = {
        "id": None,
        "slug": "btc-updown-5m-1",
        "condition_id": "c1",
        "shares": 19.3237,
        "up_price": 0.5175,
        "down_price": 0.5175,
        "up_token": "u",
        "down_token": "d",
        "kind": "twap",
    }
    n = asyncio.run(_apply_rescue(rt, row, "up", plan))
    assert n == 1
    trade = st.recent_trades(1)[0]
    assert trade["status"] == "paper_dumped"
    assert trade["down_price"] == 0.49
    assert trade["up_price"] == 0.0
    after = st.paper_state()
    assert abs(after["cash"] - (500 - 10.00001 + 9.130583)) < 1e-5
    assert abs(after["realized_pnl"] - (-0.869427)) < 1e-5
    assert abs(after["total_pnl"] - (-0.869427)) < 1e-5
    assert after["inventory_value"] == 0


def test_live_paper_fill_and_dump_fee_identity():
    from app.fees import taker_fee

    shares, buy_px = 19.3237, 0.50
    buy_fee = taker_fee(shares, buy_px, 0.07)
    cost = round(shares * buy_px + buy_fee, 6)
    assert abs(cost - 10.00001) < 1e-5
    dump_px = 0.49
    dump_fee = taker_fee(shares, dump_px, 0.07)
    proceeds = round(max(0.0, shares * dump_px - dump_fee), 6)
    assert abs(proceeds - 9.130583) < 1e-5
    assert abs(round(proceeds - 10.00001, 6) - (-0.869427)) < 1e-5
    win_shares, win_px = 18.9576, 0.51
    win_fee = taker_fee(win_shares, win_px, 0.07)
    win_cost = round(win_shares * win_px + win_fee, 6)
    assert abs(win_cost - 10.000006) < 1e-5
    assert abs(round(win_shares - win_cost, 6) - 8.957594) < 1e-5


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
    st.add_inventory("c1", "btc-updown", 20.0, 0.0, kind="favorite_live", cost=18.0)
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
    assert s["strategy_rev"] == 39
    assert s.get("auto_redeem") is True
    assert s.get("strategy_mode") == "twap"
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
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
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
    assert s["strategy_rev"] == 39
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
    assert s["strategy_rev"] == 39
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
    assert s["strategy_rev"] == 39
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
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
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


def test_rev22_stops_favorite_keeps_paper_and_edge(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev22.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=21,
        strategy_mode="favorite",
        min_edge=0.02,
        maker_window_seconds=0.0,
        taker_fok=True,
        favorite_min_price=0.97,
        favorite_max_price=0.98,
        favorite_window_seconds=60,
        favorite_maker=False,
        max_usd_per_trade=5.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
    assert float(s["min_edge"]) == 0.02
    assert float(s.get("twap_min_lead_bps") or 0) == 6.0
    assert float(s.get("twap_min_edge") or 0) == 0.04
    assert float(s.get("twap_max_left") or 0) == 280.0
    assert (s.get("twap_assets") or ["btc"])[0] == "btc"
    assert "eth" in (s.get("twap_assets") or [])
    assert float(s["maker_window_seconds"]) == 0.0
    assert s.get("taker_fok") is True
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev23_twap_engine_keeps_paper_and_complement_edge(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev23.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(40)
    st.patch_settings(
        strategy_rev=22,
        strategy_mode="complement",
        min_edge=0.02,
        maker_window_seconds=0.0,
        taker_fok=True,
        favorite_min_price=0.97,
        favorite_max_price=0.98,
        favorite_window_seconds=60,
        favorite_maker=False,
        max_usd_per_trade=5.0,
        live_trading=False,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
    assert float(s["min_edge"]) == 0.02
    assert float(s["twap_min_price"]) == 0.45
    assert float(s["twap_max_price"]) == 0.55
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["twap_min_edge"]) == 0.04
    assert float(s["twap_max_left"]) == 280.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["starting"] == 500
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


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


def test_format_fill_headline_ten_dollar_xrp_is_not_nineteen():
    from app.config import format_fill_headline, format_share_qty

    # $10 @ 0.51 after official taker fee is 18.9576 shares, not $19.
    line = format_fill_headline(up=0.51, down=0, shares=18.9576, cost=10.000006, leg="up")
    assert line == "Up 0.51 × 18.96股 · 成本 $10.00"
    assert "19.0" not in line
    assert "× 19" not in line
    assert format_share_qty(18.9576) == "18.96股"
    assert format_share_qty(19.3237) == "19.32股"
    down_line = format_fill_headline(up=0, down=0.50, shares=19.3237, cost=10.00001, leg="down")
    assert down_line == "Down 0.5 × 19.32股 · 成本 $10.00"


def test_pos_and_log_use_share_qty_not_one_decimal(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _log_text, _pos_text

    st = Store(tmp_path / "share-fmt.sqlite")
    st.ensure_paper(500)
    st.add_inventory("c-xrp", "xrp-updown-5m-1788161100", 18.9576, 0.0, kind="twap", cost=10.000006)
    rt = Runtime(st, Env())
    pos = _pos_text(rt)
    assert "18.96股" in pos
    assert "成本 $10.00" in pos
    assert "19.0" not in pos
    assert "Up 19" not in pos
    st.add_trade(
        slug="xrp-updown-5m-1788161100",
        kind="taker",
        shares=18.9576,
        up_price=0.51,
        down_price=0.0,
        net=8.957594,
        mode="paper",
        status="paper_filled",
        payload={"cost": 10.000006, "leg": "up"},
    )
    log = _log_text(rt)
    assert "18.96股" in log
    assert "成本 $10.00" in log
    assert "19.0" not in log


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
    kw = calls[0][1]
    assert kw["order_type"] == "FAK"
    assert kw["side"] == "BUY"
    assert "shares" not in kw
    assert kw["amount"] == "4.9500"
    assert kw["max_price"] == "0.9000"
    assert "limit" not in [c[0] for c in calls]
    assert (result.payload or {}).get("orders")
    assert result.payload["orders"][0]["amount"] == "4.9500"


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


def test_live_sell_uses_shares_and_min_price():
    import asyncio

    from app.broker import LiveBroker

    calls = []

    class FakeClient:
        async def place_market_order(self, **kw):
            calls.append(kw)
            return SimpleOrder(ok=True, status="matched", order_id="s1")

    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_sell("tok", 10, 0.38))
    assert result.ok is True
    assert result.status == "dumped"
    kw = calls[0]
    assert kw["side"] == "SELL"
    assert kw["order_type"] == "FAK"
    assert kw["shares"] == "10.00"
    assert kw["min_price"] == "0.3800"
    assert "amount" not in kw


def test_buy_fak_kwargs_reject_shares():
    from app.broker import buy_fak_kwargs, sell_fak_kwargs, setup_buy_orders
    from app.hunter import Setup

    buy = buy_fak_kwargs(token_id="u", price=0.5, shares=10)
    assert buy["side"] == "BUY"
    assert buy["amount"] == "5.0000"
    assert buy["max_price"] == "0.5000"
    assert "shares" not in buy
    sell = sell_fak_kwargs(token_id="u", shares=10, min_price=0.4)
    assert sell["side"] == "SELL"
    assert sell["shares"] == "10.00"
    assert sell["min_price"] == "0.4000"
    assert "amount" not in sell
    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.5,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=1.0,
        tail=False,
        extra={"strategy": "twap", "leg": "up"},
    )
    orders = setup_buy_orders(setup)
    assert len(orders) == 1
    assert orders[0]["token_id"] == "u"


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


def test_live_switch_blockers_geo_and_keys():
    from app.config import Env, live_switch_blockers

    assert "FORCE_PAPER" in live_switch_blockers(Env(force_paper=True, private_key="0x1"))
    assert "no_key" in live_switch_blockers(Env(force_paper=False, private_key=""))
    assert live_switch_blockers(Env(force_paper=False, private_key="0x1"), {"api_status": "open"}) == []
    assert "geo_close_only" in live_switch_blockers(
        Env(force_paper=False, private_key="0x1"), {"api_status": "close_only"}
    )
    assert "geo_full_block" in live_switch_blockers(
        Env(force_paper=False, private_key="0x1"), {"api_status": "full_block"}
    )


def test_clamp_live_at_boot_keeps_tg_confirm(tmp_path):
    from app.config import Env
    from app.main import clamp_live_at_boot

    st = Store(tmp_path / "boot-live.sqlite")
    st.ensure_paper(500)
    st.patch_settings(live_trading=True)
    clamp_live_at_boot(st, Env(force_paper=False, private_key="0xabc", trading_mode="paper"))
    assert st.settings()["live_trading"] is True
    clamp_live_at_boot(st, Env(force_paper=False, private_key="", trading_mode="paper"))
    assert st.settings()["live_trading"] is False
    st.patch_settings(live_trading=True)
    clamp_live_at_boot(st, Env(force_paper=True, private_key="0xabc"))
    assert st.settings()["live_trading"] is False


def test_live2_arms_when_preflight_skipped(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _handle_callback

    st = Store(tmp_path / "live2-ok.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.skip_live_preflight = True
    q = FakeQuery()
    asyncio.run(_handle_callback(rt, q, "live2"))
    assert st.settings()["live_trading"] is True
    assert rt.mode() == "live"


def test_live2_blocked_geo_close_only(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _handle_callback

    st = Store(tmp_path / "live2-geo.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.geo = {"api_status": "close_only", "country": "US"}
    rt.skip_live_preflight = True
    q = FakeQuery()
    asyncio.run(_handle_callback(rt, q, "live2"))
    assert st.settings()["live_trading"] is False


def test_scratch_skips_paper_twap_when_live(tmp_path):
    from app.config import inventory_matches_mode

    assert inventory_matches_mode("twap", live=True) is False
    assert inventory_matches_mode("twap_live", live=True) is True
    assert inventory_matches_mode("twap", live=False) is True
    assert inventory_matches_mode("favorite_live", live=False) is False


def _scratch_event(cid="cid-btc", slug="btc-updown-5m-1000"):
    return {
        "condition_id": cid,
        "slug": slug,
        "end": _late_end(40),
        "up_token": "u",
        "down_token": "d",
        "fee_rate": 0.07,
    }


def _put_dumpable_books(rt, *, up="u", down="d"):
    from app.hunter import Level

    now_ms = __import__("time").time() * 1000.0
    bids = [Level(0.50, 40.0)]
    asks = [Level(0.51, 40.0)]
    rt.books.put(up, asks, bids, ts_ms=now_ms, source="test")
    rt.books.put(down, asks, bids, ts_ms=now_ms, source="test")


def test_scratch_twap_dumps_paper_inventory(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _scratch_twap

    st = Store(tmp_path / "scratch-paper.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(5.0)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env(force_paper=True))
    _put_dumpable_books(rt)
    n = asyncio.run(_scratch_twap(rt, [_scratch_event()]))
    assert n == 1
    assert st.inventory_open() == []
    assert st.recent_trades(1)[0]["status"] == "paper_dumped"


def test_scratch_twap_skips_paper_when_live(tmp_path):
    import asyncio

    from app.broker import FillResult
    from app.config import Env
    from app.runtime import Runtime, _scratch_twap

    class Spy:
        mode = "live"

        def __init__(self):
            self.sells = 0

        async def execute_sell(self, token_id, shares, min_price):
            self.sells += 1
            return FillResult(True, "dumped", "live", "should not run", {"shares": shares})

    st = Store(tmp_path / "scratch-skip.sqlite")
    st.ensure_paper(500)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap", cost=5.0)
    st.patch_settings(live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    spy = Spy()
    rt._broker = spy
    rt._broker_mode = "live"
    _put_dumpable_books(rt)
    n = asyncio.run(_scratch_twap(rt, [_scratch_event()]))
    assert n == 0
    assert spy.sells == 0
    row = st.inventory_one("cid-btc")
    assert abs(float(row["down"]) - 10.0) < 1e-9
    assert row["kind"] == "twap"


def test_scratch_twap_dumps_live_inventory(tmp_path):
    import asyncio

    from app.broker import FillResult
    from app.config import Env
    from app.runtime import Runtime, _scratch_twap

    class Spy:
        mode = "live"

        def __init__(self):
            self.sells = []

        async def execute_sell(self, token_id, shares, min_price):
            self.sells.append((token_id, shares, min_price))
            return FillResult(True, "dumped", "live", "ok", {"shares": shares, "proceeds": 4.8})

    st = Store(tmp_path / "scratch-live.sqlite")
    st.ensure_paper(500)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap_live", cost=5.0)
    st.add_inventory("cid-paper", "eth-updown-5m-1000", 0.0, 8.0, kind="twap", cost=4.0)
    st.patch_settings(live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    spy = Spy()
    rt._broker = spy
    rt._broker_mode = "live"
    _put_dumpable_books(rt)
    _put_dumpable_books(rt, up="u2", down="d2")
    events = [
        _scratch_event(),
        _scratch_event(cid="cid-paper", slug="eth-updown-5m-1000")
        | {"up_token": "u2", "down_token": "d2"},
    ]
    n = asyncio.run(_scratch_twap(rt, events))
    assert n == 1
    assert spy.sells == [("d", 10.0, 0.5)]
    assert st.inventory_one("cid-btc")["down"] <= 0.01
    assert abs(float(st.inventory_one("cid-paper")["down"]) - 8.0) < 1e-9
    assert st.recent_trades(1)[0]["status"] == "dumped"


def test_live_mode_settles_paper_inventory_without_chain(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    class Spy:
        mode = "live"

        def __init__(self):
            self.calls = 0

        async def redeem(self, condition_id: str):
            self.calls += 1
            raise AssertionError("paper leftover must not hit live redeem")

    st = Store(tmp_path / "live-paper-redeem.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(10.0)
    st.add_inventory("c1", "btc-updown", 0.0, 10.0, kind="twap", cost=10.0)
    st.patch_settings(live_trading=True, auto_redeem=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.skip_live_preflight = True
    rt._broker = Spy()
    rt._broker_mode = "live"
    rt.data = _FakeGamma({"btc-updown": _closed_up_win()})
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 1
    after = st.paper_state()
    assert abs(after["cash"] - 490.0) < 1e-9
    assert round(after["total_pnl"], 2) == -10.0
    assert st.inventory_open() == []
    assert st.recent_trades(1)[0]["status"] == "paper_settled"


def test_take_inventory_prorates_cost(tmp_path):
    st = Store(tmp_path / "take-cost.sqlite")
    st.add_inventory("c1", "btc", 0.0, 20.0, kind="twap", cost=10.0)
    st.take_inventory("c1", up=0.0, down=10.0)
    row = st.inventory_one("c1")
    assert abs(row["down"] - 10.0) < 1e-9
    assert abs(row["cost"] - 5.0) < 1e-9


def test_live_taker_uses_actual_taking_amount():
    import asyncio

    from app.broker import LiveBroker
    from app.hunter import Setup

    class FillOrder:
        def __init__(self):
            self.ok = True
            self.status = "matched"
            self.order_id = "o2"
            self.message = ""
            self.code = None
            self.taking_amount = 12.5
            self.making_amount = 6.4

    class FakeClient:
        async def place_market_order(self, **kw):
            return FillOrder()

    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.51,
        down_price=0.0,
        shares=18.9,
        fillable=18.9,
        gross=0.1,
        fees=0.3,
        net=8.0,
        tail=True,
        extra={"strategy": "twap", "leg": "up"},
    )
    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_pair(setup))
    assert result.ok is True
    assert abs(float(result.payload["shares"]) - 12.5) < 1e-9
    assert abs(float(result.payload["cost"]) - 6.4) < 1e-9


def test_normalize_private_key_adds_0x():
    from app.config import normalize_private_key

    assert normalize_private_key("ab") == "0xab"
    assert normalize_private_key("0xab") == "0xab"
    assert normalize_private_key("  0xab  ") == "0xab"
    assert normalize_private_key("") == ""


def test_live_broker_passes_wallet_to_client(monkeypatch):
    import asyncio
    import sys
    import types

    from app.broker import LiveBroker

    captured = {}

    class FakeClient:
        @classmethod
        async def create(cls, **kw):
            captured.update(kw)
            return cls()

    fake = types.ModuleType("polymarket")
    fake.AsyncSecureClient = FakeClient
    monkeypatch.setitem(sys.modules, "polymarket", fake)
    safe = "0xC8a8dEF991F2FC0fa7322b9374A682848615b3db"
    broker = LiveBroker("abc123", wallet=safe)
    asyncio.run(broker._client_ready())
    assert captured["private_key"] == "0xabc123"
    assert captured["wallet"] == safe
    eoa = LiveBroker("0xabc")
    captured.clear()
    asyncio.run(eoa._client_ready())
    assert captured["private_key"] == "0xabc"
    assert "wallet" not in captured


def _arm_client(*, usdc=19.76, closed=False, approvals_exc=None):
    class Bal:
        balance = int(round(float(usdc) * 1_000_000))
        allowances = {"x": 10**30}

    class Client:
        async def setup_trading_approvals(self):
            if approvals_exc is not None:
                raise approvals_exc

        async def get_balance_allowance(self, **kw):
            return Bal()

        async def get_closed_only_mode(self):
            return bool(closed)

    return Client()


def test_arm_live_wallet_skips_gasless_extras(tmp_path, monkeypatch):
    import asyncio

    from app.broker import LiveBroker
    from app.config import Env
    from app.runtime import Runtime, arm_live_wallet

    st = Store(tmp_path / "arm-gasless.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc", wallet="0xC8a8dEF991F2FC0fa7322b9374A682848615b3db"))
    client = _arm_client(
        approvals_exc=RuntimeError(
            "Gasless transactions require a Builder API Key or Relayer API Key. Pass api_key= when constructing the client."
        )
    )

    async def ready(self):
        return client

    async def no_core(c):
        return []

    monkeypatch.setattr(LiveBroker, "_client_ready", ready)
    monkeypatch.setattr("app.runtime._missing_core_clob_approvals", no_core)
    err = asyncio.run(arm_live_wallet(rt))
    assert err is None
    assert rt.live_onchain_limited is True
    assert rt.live_usdc is not None and rt.live_usdc >= 19.0
    assert st.settings()["live_trading"] is False


def test_arm_live_wallet_blocks_when_core_clob_missing(tmp_path, monkeypatch):
    import asyncio

    from app.broker import LiveBroker
    from app.config import Env
    from app.runtime import Runtime, arm_live_wallet

    st = Store(tmp_path / "arm-core.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc", wallet="0xC8"))
    client = _arm_client(
        approvals_exc=RuntimeError("Gasless transactions require a Builder API Key or Relayer API Key.")
    )

    async def ready(self):
        return client

    async def core_gap(c):
        return ["0xe111180000d2663c0091e4f400237545b87b996b"]

    monkeypatch.setattr(LiveBroker, "_client_ready", ready)
    monkeypatch.setattr("app.runtime._missing_core_clob_approvals", core_gap)
    err = asyncio.run(arm_live_wallet(rt))
    assert err is not None and "CLOB" in err
    assert st.settings()["live_trading"] is False


def test_arm_live_wallet_blocks_close_only_account(tmp_path, monkeypatch):
    import asyncio

    from app.broker import LiveBroker
    from app.config import Env
    from app.runtime import Runtime, arm_live_wallet

    st = Store(tmp_path / "arm-closed.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    client = _arm_client(closed=True)

    async def ready(self):
        return client

    monkeypatch.setattr(LiveBroker, "_client_ready", ready)
    err = asyncio.run(arm_live_wallet(rt))
    assert err is not None and "close-only" in err
    assert st.settings()["live_trading"] is False


def test_today_pnl_live_includes_dumps(tmp_path):
    st = Store(tmp_path / "live-pnl.sqlite")
    st.add_trade(slug="a", kind="twap", shares=5, up_price=0.5, down_price=0, net=-2, mode="live", status="dumped")
    st.add_trade(slug="b", kind="settle", shares=5, up_price=1, down_price=0, net=3, mode="live", status="redeemed")
    st.add_trade(slug="c", kind="twap", shares=5, up_price=0.5, down_price=0, net=-9, mode="paper", status="paper_dumped")
    assert abs(st.today_pnl(mode="live") - 1.0) < 1e-9


def test_reverse_breakeven_is_reverse_rate_not_win_rate():
    """Old 30d script labelled p+fee (~97%) as reverse BE; true BE is 1-p-fee (~2.8% at 97¢)."""
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "reverse_30d.py"
    spec = importlib.util.spec_from_file_location("reverse_30d_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    be97 = mod.reverse_breakeven(0.97)
    be98 = mod.reverse_breakeven(0.98)
    assert 0.027 < be97 < 0.029
    assert 0.018 < be98 < 0.020
    assert be97 == round(1.0 - 0.97 - mod.fee_on(0.97), 6)

    row = mod.summarize(
        [
            {"won": True, "px": 0.97, "pnl": 0.14, "left": 40, "looked_50": False, "looked_90": False},
            {"won": False, "px": 0.97, "pnl": -5.0, "left": 40, "looked_50": True, "looked_90": True},
        ]
    )
    assert row["reverse"] == 0.5
    assert row["ev_ok"] is False
    assert row["vs_be"] > 0.4


def test_fair_p_stay_brownian():
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "reverse_predict.py"
    spec = importlib.util.spec_from_file_location("reverse_predict_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert abs(mod.phi(0.0) - 0.5) < 1e-9
    assert mod.fair_p_stay(0.0, 1.0, 60.0) == 0.5
    assert mod.fair_p_stay(80.0, 1.0, 9.0) > 0.99
    assert mod.fair_p_stay(-80.0, 1.0, 9.0) < 0.01
    assert mod.to_sec(1787875200000000) == 1787875200


def test_strategy_mode_of_defaults_to_twap():
    from app.config import DEFAULT_SETTINGS, strategy_mode_of

    assert DEFAULT_SETTINGS["strategy_rev"] == 39
    assert DEFAULT_SETTINGS["strategy_mode"] == "twap"
    assert strategy_mode_of(None) == "twap"
    assert strategy_mode_of({}) == "twap"
    assert strategy_mode_of({"strategy_mode": "favorite"}) == "twap"
    assert strategy_mode_of({"strategy_mode": "complement"}) == "twap"
    assert strategy_mode_of({"strategy_mode": "nope"}) == "twap"


def test_telegram_settings_lock_twap_and_drop_legacy(tmp_path):
    from app.config import Env, SETTING_STEPS
    from app.runtime import Runtime
    from app.telegram_ui import TOGGLES, home_text, settings_kb

    st = Store(tmp_path / "tgset.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    labels = " ".join(btn.text for row in settings_kb(rt).inline_keyboard for btn in row)
    assert "鎖定" in labels
    assert "週期：5分鐘（鎖定）" in labels
    assert "週期 5M／15M／1H" not in labels
    assert "大熱尾窗" not in labels
    assert "尾盤優先" not in labels
    assert "全日掛單" not in labels
    assert "大熱定價掛單" not in labels
    assert "favorite_maker" not in TOGGLES
    assert "prefer_tail" not in TOGGLES
    assert "maker_first" not in TOGGLES
    assert "min_edge" not in SETTING_STEPS
    assert "favorite_min_price" not in SETTING_STEPS
    assert "twap_max_left" in SETTING_STEPS
    assert "唔做 YES+NO 互補" in home_text(rt)
    assert "只做 5 分鐘" in home_text(rt)
    assert "15 分鐘同 5 分鐘搶槽，已砍" in home_text(rt)


def test_top_5m_follow_is_not_a_ship_signal():
    """Top 5m wallets are not 97¢ farmers; Binance-TWAP follow sign-flips train vs holdout."""
    import json
    from collections import Counter
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "top_5m.py"
    spec = importlib.util.spec_from_file_location("top_5m_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert mod.band_of(0.40) == "longshot_<45"
    assert mod.band_of(0.50) == "mid_45_55"
    assert mod.band_of(0.975) == "favorite_97_99"
    acc = Counter({"longshot_<45": 400, "midhi_56_89": 400, "mid_45_55": 200})
    assert mod.classify_style(acc, 0, 1000, 70, 100) == "both_sides_accumulator"
    fav = Counter({"favorite_97_99": 90, "hi_90_96": 10})
    assert mod.classify_style(fav, 0, 100, 0, 50) == "favorite_taker"

    data = json.loads(path.with_name("top_5m.json").read_text())
    f = data["findings"]
    assert f["sim_follow_2bps_robust"] is False
    assert f["n_wallets_with_50plus_5m"] >= 10
    assert 0.45 <= f["median_of_median_buy_px"] <= 0.70
    train = data["sim_twap60"]["train"]["follow_2bps"]
    hold = data["sim_twap60"]["holdout"]["follow_2bps"]
    assert train["ev_ok"] is False
    assert train["pnl_usd"] < 0
    # Holdout can look lucky; both splits must pass before this is a hunter.
    assert not (train.get("ev_ok") and hold.get("ev_ok"))
    assert "complement" in f["recommend"]["stop_now"]
    assert data["sim_twap60"]["all"]["favorite"]["ev_ok"] is False


def _twap_snap(**kw):
    from app.twap import TwapSnap

    base = dict(
        symbol="btc/usd",
        slug="btc-updown-5m-1000",
        asset="btc",
        start=1000,
        ptb=100000.0,
        twap=100080.0,
        spot=100080.0,
        lead_bps=8.0,
        vol_bps_sqrt_s=2.0,
        fair_p_up=0.70,
        lookback=60,
        age_ms=100.0,
        tick_n=40,
        connected=True,
    )
    base.update(kw)
    return TwapSnap(**base)


def test_time_weighted_twap_step_path():
    from app.twap import time_weighted_twap

    assert abs(time_weighted_twap([(0.0, 100.0), (10.0, 100.0)], 10.0, 10.0) - 100.0) < 1e-9
    assert abs(time_weighted_twap([(0.0, 10.0), (5.0, 20.0)], 10.0, 10.0) - 15.0) < 1e-9
    # First tick inside the window: weight from first tick only.
    got = time_weighted_twap([(2.0, 10.0), (8.0, 20.0)], 10.0, 10.0)
    assert abs(got - 12.5) < 1e-9
    assert time_weighted_twap([], 10.0, 10.0) is None


def test_fair_p_up_and_lead():
    from app.twap import fair_p_up, lead_bps, phi

    assert abs(phi(0.0) - 0.5) < 1e-9
    assert lead_bps(100080.0, 100000.0) == 8.0
    assert fair_p_up(0.0, 2.0, 60.0) == 0.5
    assert fair_p_up(80.0, 1.0, 9.0) > 0.99
    assert fair_p_up(-80.0, 1.0, 9.0) < 0.01
    assert fair_p_up(8.0, None, 90.0) is None


def test_twap_entry_reason_and_scratch():
    from app.twap import TwapParams, should_scratch, twap_entry_reason

    snap = _twap_snap()
    params = TwapParams()
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=None, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) == "twap_no_feed"
    assert twap_entry_reason(
        slug="eth-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="xrp-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) == "twap_asset"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.97, bid=0.96, left=90.0, fee_rate=0.07, params=params
    ) == "twap_band"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=290.0, fee_rate=0.07, params=params
    ) == "twap_window"
    weak = _twap_snap(lead_bps=2.0)
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=weak, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) == "twap_lead"

    go, why = should_scratch(fair_p=0.40, lead_bps_signed=8.0, bid=0.38, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_weak"
    go, why = should_scratch(fair_p=0.55, lead_bps_signed=-1.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_flip"
    go, why = should_scratch(fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is False and why == "twap_hold"
    go, why = should_scratch(fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=5.0, params=params)
    assert go is False and why == "twap_scratch_late"
    go, why = should_scratch(fair_p=None, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_no_fair"
    go, why = should_scratch(fair_p=0.50, lead_bps_signed=8.0, bid=0.52, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_better"


def test_twap_gate_row_reports_window_and_signal():
    from app.runtime import _twap_gate_row
    from app.twap import TwapParams

    snap = _twap_snap()
    ev = {"slug": "btc-updown-5m-1000", "end": _late_end(90)}
    up = {"asks": _L((0.50, 20)), "bids": _L((0.49, 20))}
    dn = {"asks": _L((0.52, 20)), "bids": _L((0.48, 20))}
    gate = _twap_gate_row(ev, snap, up, dn, 0.07, TwapParams(), None)
    assert gate["reason"] == "ready"
    assert gate["ask"] == 0.50
    assert gate["lead_bps"] == 8.0
    early = dict(ev, end=_late_end(290))
    gate2 = _twap_gate_row(early, snap, up, dn, 0.07, TwapParams(), None)
    assert gate2["reason"] == "twap_window"

    class _Tape:
        connected = True
        ticks = {"btc/usd": [1]}

    gate_ptb = _twap_gate_row(ev, None, up, dn, 0.07, TwapParams(), None, chainlink=_Tape())
    assert gate_ptb["reason"] == "twap_no_ptb"
    from app.hunter import Setup

    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=0.5,
        tail=False,
        extra={"strategy": "twap", "leg": "up"},
    )
    gate3 = _twap_gate_row(ev, snap, up, dn, 0.07, TwapParams(), setup)
    assert gate3["reason"] == "signal"


def test_chainlink_ptb_requires_tick_before_open():
    from app.chainlink import ChainlinkTape

    start = 1_700_000_000 - (1_700_000_000 % 300)
    slug = f"btc-updown-5m-{start}"

    mid = ChainlinkTape()
    mid.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100001, "timestamp": start + 10}}
    )
    assert mid.ensure_ptb(slug) is None

    ok = ChainlinkTape()
    ok.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100000, "timestamp": start - 1}}
    )
    ok.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100010, "timestamp": start + 0.2}}
    )
    assert ok.ensure_ptb(slug) == 100010.0

    late = ChainlinkTape()
    late.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100000, "timestamp": start - 1}}
    )
    late.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100010, "timestamp": start + 6}}
    )
    assert late.ensure_ptb(slug) is None


def test_chainlink_apply_message_accepts_json_string():
    import json

    from app.chainlink import ChainlinkTape

    tape = ChainlinkTape()
    assert tape.apply_message('{"topic":"crypto_prices_chainlink","type":"update","payload":{"symbol":"btc/usd","value":101,"timestamp":1700000000}}')
    assert tape.ticks["btc/usd"][-1].price == 101.0
    assert tape.apply_message("PONG") is False
    frame = tape.subscribe_frame()
    subs = json.loads(frame)["subscriptions"]
    assert subs[0]["filters"] == '{"symbol":"btc/usd"}'
    frames = tape.subscribe_frames()
    assert len(frames) == 2
    assert json.loads(frames[1])["subscriptions"][0]["filters"] == '{"symbol":"eth/usd"}'


def test_chainlink_ingests_filtered_snapshot_and_slash_topic():
    from app.chainlink import ChainlinkTape

    tape = ChainlinkTape()
    start = 1_700_000_000 - (1_700_000_000 % 300)
    ok = tape.apply_message(
        {
            "topic": "crypto_prices",
            "type": "subscribe",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": (start - 2) * 1000, "value": 100000},
                    {"timestamp": (start + 1) * 1000, "value": 100010},
                    {"timestamp": (start + 2) * 1000, "value": 100020},
                ],
            },
        }
    )
    assert ok is True
    assert tape.ticks["btc/usd"][-1].price == 100020.0
    assert tape.ensure_ptb(f"btc-updown-5m-{start}") == 100010.0
    assert tape.apply_message({"topic": "crypto_prices", "type": "update", "payload": {"symbol": "btcusdt", "value": 99}}) is False


def test_twap_hunt_lifts_mid_band_skips_97_and_needs_snap():
    from app.hunter import is_twap_setup

    kw = dict(
        slug="btc-updown-5m-1000",
        title="btc 5m",
        condition_id="0xtwap",
        up_token="u",
        down_token="d",
        up_bids=_L((0.49, 20)),
        down_bids=_L((0.48, 20)),
        max_usd=5,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(90),
        strategy_mode="twap",
        twap_snap=_twap_snap(),
    )
    setup = hunt(
        **kw,
        up_asks=_L((0.50, 40)),
        down_asks=_L((0.52, 40)),
    )
    assert setup is not None
    assert is_twap_setup(setup)
    assert setup.extra["leg"] == "up"
    assert 0.49 <= setup.up_price <= 0.51
    assert setup.down_price == 0.0
    assert setup.net > 0
    assert float(setup.extra["cash_cost"]) > setup.net

    fav_book = hunt(
        **kw,
        up_asks=_L((0.97, 40)),
        down_asks=_L((0.04, 40)),
    )
    assert fav_book is None

    missing = hunt(
        **{**kw, "twap_snap": None},
        up_asks=_L((0.50, 40)),
        down_asks=_L((0.52, 40)),
    )
    assert missing is None

    hole = hunt(
        **kw,
        up_asks=_L((0.97, 80)),
        down_asks=_L((0.01, 80)),
    )
    assert hole is None


def test_twap_two_dollar_cannot_fill_five_share_min_three_can():
    """5m CLOB min is 5 shares. $2 @ 45–55¢ is under that; $3 clears even at 55¢."""
    from app.fees import taker_cash

    kw = dict(
        slug="btc-updown-5m-1000",
        title="btc 5m",
        condition_id="0xtwap",
        up_token="u",
        down_token="d",
        up_bids=_L((0.49, 20)),
        down_bids=_L((0.48, 20)),
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=False,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(90),
        strategy_mode="twap",
        twap_snap=_twap_snap(),
    )
    assert taker_cash(5, 0.55, 0.07) > 2.0
    assert taker_cash(5, 0.55, 0.07) < 3.0
    assert hunt(
        **kw,
        max_usd=2,
        up_asks=_L((0.51, 40)),
        down_asks=_L((0.50, 40)),
    ) is None
    assert hunt(
        **kw,
        max_usd=2,
        up_asks=_L((0.45, 40)),
        down_asks=_L((0.55, 40)),
    ) is None
    setup = hunt(
        **kw,
        max_usd=3,
        up_asks=_L((0.51, 40)),
        down_asks=_L((0.50, 40)),
    )
    assert setup is not None
    assert setup.shares >= 5
    assert float(setup.extra["cash_cost"]) <= 3.01
    hi_kw = {
        **kw,
        "max_usd": 3,
        "up_asks": _L((0.55, 40)),
        "down_asks": _L((0.46, 40)),
        "up_bids": _L((0.52, 20)),
        "down_bids": _L((0.44, 20)),
        "twap_snap": _twap_snap(lead_bps=8.0, fair_p_up=0.70),
    }
    hi = hunt(**hi_kw)
    assert hi is not None
    assert hi.shares >= 5
    assert float(hi.extra["cash_cost"]) <= 3.01


def test_nudge_trade_usd_skips_two_and_keeps_ten():
    from app.config import SETTING_STEPS, TRADE_USD_STEPS, nudge_trade_usd

    assert TRADE_USD_STEPS[0] == 3.0
    assert 2.0 not in TRADE_USD_STEPS
    assert 10.0 in TRADE_USD_STEPS
    assert SETTING_STEPS["max_usd_per_trade"][1] == 3.0
    assert nudge_trade_usd(10, up=False) == 5.0
    assert nudge_trade_usd(5, up=False) == 3.0
    assert nudge_trade_usd(3, up=False) == 3.0
    assert nudge_trade_usd(10, up=True) == 15.0
    assert nudge_trade_usd(2, up=True) == 3.0
    assert nudge_trade_usd(2, up=False) == 3.0


def test_telegram_stake_steps_two_dollar_floor_message(tmp_path):
    import asyncio

    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import _handle_callback

    st = Store(tmp_path / "usd-step.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=10.0)
    rt = Runtime(st, Env())
    q = FakeQuery()
    asyncio.run(_handle_callback(rt, q, "dec:max_usd_per_trade"))
    assert float(st.settings()["max_usd_per_trade"]) == 5.0
    asyncio.run(_handle_callback(rt, q, "dec:max_usd_per_trade"))
    assert float(st.settings()["max_usd_per_trade"]) == 3.0
    asyncio.run(_handle_callback(rt, q, "dec:max_usd_per_trade"))
    assert float(st.settings()["max_usd_per_trade"]) == 3.0
    assert "最低$3" in (q.answered.get("args") or ("",))[0]
    asyncio.run(_handle_callback(rt, q, "inc:max_usd_per_trade"))
    assert float(st.settings()["max_usd_per_trade"]) == 5.0

    from app.hunter import Setup

    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.20,
        fees=0.17,
        net=0.50,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "cash_cost": 5.175, "fair_p": 0.70},
    )
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
        seconds_left=90,
        cash=500,
        cost=setup.cost,
    )
    assert ok.ok is True
    setup.net = 0.0
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
        killed=False,
        engine_running=True,
        auto_execute=True,
        seconds_left=90,
    )
    assert dead.ok is False
    assert dead.reason == "non_positive_net"
    setup.net = 0.50
    setup.up_price = 0.40
    band = approve(
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
        seconds_left=90,
    )
    assert band.ok is False
    assert band.reason == "twap_out_of_band"
    setup.up_price = 0.50
    early = approve(
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
        seconds_left=290,
    )
    assert early.ok is False
    assert early.reason == "twap_window"


def test_twap_inventory_marks_equity_at_cost(tmp_path):
    st = Store(tmp_path / "twap-eq.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(5.18)
    st.add_inventory("c1", "btc-updown-5m-1000", 10, 0, kind="twap", cost=5.18)
    paper = st.paper_state()
    assert paper["inventory_value"] == 5.18
    assert abs(paper["equity"] - (500 - 5.18 + 5.18)) < 1e-9
    assert abs(paper["total_pnl"]) < 1e-9
    assert abs(paper["realized_pnl"]) < 1e-9
    assert st.inventory_one("c1")["kind"] == "twap"
    st.add_inventory("c2", "btc-updown-5m-1001", 8, 0, kind="twap_live", cost=4.0)
    live = st.paper_state()
    assert live["inventory_value"] == 5.18
    assert abs(live["equity"] - paper["equity"]) < 1e-9


def test_paper_execute_twap_is_one_leg():
    from app.broker import paper_execute
    from app.hunter import Setup

    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.20,
        fees=0.17,
        net=1.80,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "fee_rate": 0.07, "cash_cost": 5.175},
    )
    result = paper_execute(setup)
    assert result.ok is True
    assert result.status == "paper_filled"
    assert result.payload["down_price"] == 0.0
    assert result.payload["up_price"] == 0.50
    assert 5.10 < float(result.payload["cost"]) < 5.30
    orders = result.payload["orders"]
    assert len(orders) == 1
    assert orders[0]["side"] == "BUY"
    assert "shares" not in orders[0]
    assert orders[0]["amount"] == "5.0000"
    assert orders[0]["max_price"] == "0.5000"
    assert orders[0]["order_type"] == "FAK"


def test_twap_engine_json_scratch_is_robust():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "twap_engine.json").read_text())
    picked = data["picked"]
    assert picked["robust"] is True
    assert picked["train"]["ev_ok"] is True
    assert picked["holdout"]["ev_ok"] is True
    assert picked["all"]["scratch_n"] > 0
    assert picked["max_left"] == 120.0
    assert picked["min_lead_bps"] == 6.0
    assert data["findings"]["use_live"] is True


def test_rev24_copies_whale_timing_not_pairlock(tmp_path):
    from app.main import apply_strategy_rev
    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    st = Store(tmp_path / "rev24.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(25)
    st.patch_settings(
        strategy_rev=23,
        strategy_mode="twap",
        min_edge=0.02,
        twap_max_left=120.0,
        twap_min_lead_bps=6.0,
        live_trading=False,
        max_usd_per_trade=5.0,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
    assert float(s["twap_max_left"]) == 280.0
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["min_edge"]) == 0.02
    assert s["live_trading"] is False
    assert float(DEFAULT_SETTINGS["twap_max_left"]) == 280.0
    p = default_params(s)
    assert p.max_left == 280.0
    assert p.min_lead_bps == 6.0
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev25_aligns_paper_clob_fak_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev
    from app.config import DEFAULT_SETTINGS

    st = Store(tmp_path / "rev25.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(25)
    st.patch_settings(
        strategy_rev=24,
        strategy_mode="twap",
        twap_max_left=180.0,
        live_trading=False,
        max_usd_per_trade=5.0,
        clob_rtt_ms=0.0,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s.get("strategy_mode") == "twap"
    assert float(s["twap_max_left"]) == 280.0
    assert float(s["clob_rtt_ms"]) == 150.0
    assert s["live_trading"] is False
    assert float(DEFAULT_SETTINGS["clob_rtt_ms"]) == 150.0
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev26_locks_twap_only_keeps_paper_and_universe(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev26.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(11)
    st.patch_settings(
        strategy_rev=25,
        strategy_mode="favorite",
        tags=["5M"],
        assets=["btc", "eth"],
        live_trading=False,
        max_usd_per_trade=5.0,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["strategy_mode"] == "twap"
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth"]
    assert s["live_trading"] is False
    assert float(s["twap_max_left"]) == 280.0
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_fok_rtt_miss_does_not_ghost_fill(tmp_path):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app.config import Env
    from app.hunter import Setup
    from app.runtime import Runtime, _fok_confirm

    st = Store(tmp_path / "rtt.sqlite")
    st.ensure_paper(500)
    st.patch_settings(fok_delay_ms=0, clob_rtt_ms=1, strategy_mode="twap", min_shares=5)
    rt = Runtime(st, Env())

    class FakeData:
        def __init__(self):
            self.round = 0

        async def book(self, token):
            if token != "u":
                return {"asks": [], "bids": []}
            self.round += 1
            if self.round == 1:
                return {"asks": _L((0.50, 20)), "bids": _L((0.49, 20))}
            return {"asks": _L((0.70, 20)), "bids": _L((0.49, 20))}

    rt.data = FakeData()
    end = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()
    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.20,
        fees=0.17,
        net=1.80,
        tail=False,
        extra={"strategy": "twap", "leg": "up"},
    )
    ev = {
        "slug": "btc-updown-5m-1000",
        "title": "btc",
        "condition_id": "c1",
        "up_token": "u",
        "down_token": "d",
        "end": end,
        "fee_rate": 0.07,
        "min_size": 5,
    }
    miss = asyncio.run(_fok_confirm(rt, ev, setup))
    assert miss.ok is False
    assert miss.reason == "clob_rtt_miss"

    st.patch_settings(clob_rtt_ms=0)
    setup2 = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.50,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.20,
        fees=0.17,
        net=1.80,
        tail=False,
        extra={"strategy": "twap", "leg": "up"},
    )
    rt.data = FakeData()
    hit = asyncio.run(_fok_confirm(rt, ev, setup2))
    assert hit.ok is True
    assert hit.up_price == 0.50


def test_copy_top_rejects_taker_pairlock_and_copytrade():
    import json
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "copy_top.py"
    spec = importlib.util.spec_from_file_location("copy_top_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    data = json.loads(path.with_name("copy_top.json").read_text())
    f = data["findings"]
    assert f["copy_trade_robust"] is False
    assert f["print_implied_simul_is_mirage"] is True
    assert f["pairlock_fee07_any_plus"] == []
    assert f["copy_as"] == "twap_earlier_window"
    assert f["picked_rule"] == "twap_max_left_180_lead6_scratch"
    assert f["picked_early_twap"]["max_left"] == 180.0
    assert f["picked_early_twap"]["min_lead_bps"] == 6.0
    assert f["picked_early_twap"]["robust"] is True
    assert f["n_5m_specialists"] >= 20
    assert f["n_pair_lock_harvesters"] >= 3
    rec = mod.sim_pairlock(
        [(100, 0.48, "Up"), (145, 0.47, "Down")],
        "Up",
        0,
        300,
        first_max=0.50,
        complete_sum=0.96,
        min_left=12,
        max_left=240,
        chop=False,
        fee_rate=0.07,
    )
    assert rec["kind"] == "paired"
    assert rec["pnl"] < 0.5
    unmatched = mod.sim_pairlock(
        [(100, 0.48, "Up")],
        "Down",
        0,
        300,
        first_max=0.50,
        complete_sum=0.96,
        min_left=12,
        max_left=240,
        chop=False,
        fee_rate=0.07,
    )
    assert unmatched["kind"] == "unmatched"
    assert unmatched["pnl"] < 0


def test_rev27_opens_280s_and_eth_keeps_paper(tmp_path):
    from app.main import apply_strategy_rev
    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    st = Store(tmp_path / "rev27.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(18)
    st.patch_settings(
        strategy_rev=26,
        strategy_mode="twap",
        tags=["5M"],
        assets=["btc", "eth"],
        twap_max_left=180.0,
        twap_assets=["btc"],
        live_trading=False,
        max_usd_per_trade=5.0,
    )
    before = st.paper_state()
    n = apply_strategy_rev(st)
    assert n == 0
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth"]
    assert float(s["twap_max_left"]) == 280.0
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["twap_min_price"]) == 0.45
    assert float(s["twap_max_price"]) == 0.55
    assert "eth" in s["twap_assets"]
    assert "sol" in s["twap_assets"]
    assert s["twap_horizons"] == ["5m"]
    assert s["live_trading"] is False
    assert "sol" in DEFAULT_SETTINGS["twap_assets"]
    assert DEFAULT_SETTINGS["twap_horizons"] == ["5m"]
    p = default_params(s)
    assert p.max_left == 280.0
    assert p.assets == ("btc", "eth")
    assert p.horizons == ("5m",)
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_twap_freq_ships_280s_band_and_eth():
    import json
    from pathlib import Path

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "twap_freq.json").read_text())
    shipped = data["shipped"]
    assert shipped["max_left"] == 280.0
    assert shipped["band"] == "45-55"
    assert shipped["min_lead_bps"] == 6.0
    assert shipped["assets"] == ["btc", "eth"]
    assert data["ship_eth"] is True
    btc = data["picked"]
    assert btc["robust"] is True
    assert btc["holdout"]["pnl_usd"] > data["baseline"]["holdout"]["pnl_usd"]
    assert btc["all"]["n"] > data["baseline"]["all"]["n"]
    eth = data["eth_pick"]
    assert eth["robust"] is True
    assert eth["holdout"]["pnl_usd"] > 0
    assert eth["max_left"] == 280.0
    assert eth["band"] == "45-55"


def test_eth_twap_hunt_lifts_when_assets_include_eth():
    from app.hunter import is_twap_setup
    from app.twap import TwapParams

    kw = dict(
        slug="eth-updown-5m-1000",
        title="eth 5m",
        condition_id="0xeth",
        up_token="u",
        down_token="d",
        up_bids=_L((0.49, 20)),
        down_bids=_L((0.48, 20)),
        max_usd=5,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(90),
        strategy_mode="twap",
        twap_snap=_twap_snap(),
        twap_params=TwapParams(assets=("btc", "eth"), max_left=280.0),
        up_asks=_L((0.50, 40)),
        down_asks=_L((0.52, 40)),
    )
    setup = hunt(**kw)
    assert setup is not None
    assert is_twap_setup(setup)
    blocked = hunt(**{**kw, "twap_params": TwapParams(assets=("btc",), max_left=280.0)})
    assert blocked is None


def test_parse_window_settlement_allowlist():
    from app.twap import (
        TwapParams,
        default_params,
        future_listing,
        is_hourly_updown,
        parse_window,
        slug_allowed,
        twap_entry_reason,
    )

    btc5 = parse_window("btc-updown-5m-1000")
    assert btc5 is not None and btc5.window_seconds == 300 and btc5.symbol == "btc/usd"
    sol15 = parse_window("sol-updown-15m-1000")
    assert sol15 is not None and sol15.window_seconds == 900 and sol15.asset == "sol"
    assert parse_window("bitcoin-up-or-down-september-1-2026-11pm-et") is None
    assert parse_window("btc-above-100000-on-september-1") is None
    assert is_hourly_updown("hype-up-or-down-september-1-2026-11pm-et") is True
    assert is_hourly_updown("btc-updown-5m-1000") is False

    open_all = default_params(
        {
            "assets": ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"],
            "tags": ["5M", "15M", "1H"],
            "twap_assets": ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge", "zec"],
            "twap_horizons": ["5m", "15m"],
        }
    )
    assert slug_allowed("sol-updown-15m-1000", open_all) is True
    assert slug_allowed("btc-updown-5m-1000", open_all) is True
    assert slug_allowed("bitcoin-up-or-down-september-1-2026-11pm-et", open_all) is False
    hour_only = default_params({"assets": ["btc", "eth"], "tags": ["1H"], "twap_horizons": ["5m", "15m"], "twap_assets": ["btc", "eth"]})
    assert hour_only.horizons == ()
    assert slug_allowed("btc-updown-5m-1000", hour_only) is False
    keep_filter = default_params(
        {"assets": ["btc", "eth"], "tags": ["5M"], "twap_assets": ["btc", "eth", "sol"], "twap_horizons": ["5m", "15m"]}
    )
    assert keep_filter.assets == ("btc", "eth")
    assert keep_filter.horizons == ("5m",)
    assert slug_allowed("sol-updown-5m-1000", keep_filter) is False
    from app.config import DEFAULT_SETTINGS

    live = default_params(DEFAULT_SETTINGS)
    assert live.horizons == ("5m",)
    assert slug_allowed("btc-updown-5m-1000", live) is True
    assert slug_allowed("sol-updown-15m-1000", live) is False
    leftover = default_params(
        {
            "assets": ["btc", "eth", "sol"],
            "tags": ["5M", "15M"],
            "twap_assets": ["btc", "eth", "sol"],
            "twap_horizons": ["5m"],
        }
    )
    assert leftover.horizons == ("5m",)
    assert slug_allowed("sol-updown-15m-1000", leftover) is False
    assert future_listing(400.0, 300) is True
    assert future_listing(400.0, 900) is False
    snap = _twap_snap(slug="sol-updown-15m-1000", asset="sol", symbol="sol/usd")
    assert twap_entry_reason(
        slug="sol-updown-15m-1000",
        snap=snap,
        ask=0.50,
        bid=0.49,
        left=90.0,
        fee_rate=0.07,
        params=TwapParams(assets=("sol",), horizons=("15m",)),
    ) is None
    assert twap_entry_reason(
        slug="bitcoin-up-or-down-x",
        snap=snap,
        ask=0.50,
        bid=0.49,
        left=90.0,
        fee_rate=0.07,
        params=open_all,
    ) == "twap_oracle"


def test_pick_markets_prefers_twap_ok_over_hourly():
    from app.universe import pick_markets

    picked = pick_markets(
        [
            {
                "condition_id": "hour",
                "slug": "bitcoin-up-or-down-september-1-2026-11pm-et",
                "seconds_left": 800,
                "best_ask": 0.50,
                "volume24hr": 99,
                "twap_ok": False,
            },
            {
                "condition_id": "btc5",
                "slug": "btc-updown-5m-1",
                "seconds_left": 1200,
                "best_ask": 0.51,
                "volume24hr": 1,
                "twap_ok": True,
            },
        ],
        want=1,
        max_horizon=3600,
    )
    assert [r["condition_id"] for r in picked] == ["btc5"]


def test_chainlink_15m_ptb_and_sol_symbol():
    from app.chainlink import ChainlinkTape

    tape = ChainlinkTape(symbols=("sol/usd",))
    start = 1_700_000_000 - (1_700_000_000 % 900)
    slug = f"sol-updown-15m-{start}"
    tape.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "sol/usd", "value": 200, "timestamp": start - 1}}
    )
    tape.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "sol/usd", "value": 201, "timestamp": start + 0.3}}
    )
    assert tape.ensure_ptb(slug) == 201.0
    snap = tape.snapshot(slug, now=start + 120, lookback=60, left=780)
    assert snap is not None
    assert snap.asset == "sol"
    assert snap.ptb == 201.0


def test_twap_conflict_locks_same_asset_across_horizons(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, twap_conflict_open

    st = Store(tmp_path / "conflict.sqlite")
    st.ensure_paper(500)
    st.add_inventory("c-btc5", "btc-updown-5m-1787981100", 5.0, 0.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env())
    assert twap_conflict_open(rt, "btc-updown-15m-1787980800") is True
    assert twap_conflict_open(rt, "eth-updown-5m-1787981100") is True
    assert twap_conflict_open(rt, "eth-updown-5m-1787981400") is False
    assert twap_conflict_open(rt, "sol-updown-15m-1787980800") is False
    assert twap_conflict_open(rt, "sol-updown-5m-1787981100") is False


def test_pick_markets_prefers_twap_window_over_penny_tail():
    from app.universe import pick_markets

    picked = pick_markets(
        [
            {
                "condition_id": "penny5",
                "slug": "btc-updown-5m-1",
                "seconds_left": 90,
                "best_ask": 0.03,
                "volume24hr": 99,
                "twap_ok": True,
            },
            {
                "condition_id": "mid15",
                "slug": "eth-updown-15m-1",
                "seconds_left": 250,
                "best_ask": 0.51,
                "volume24hr": 1,
                "twap_ok": True,
            },
        ],
        want=1,
        max_horizon=3600,
    )
    assert [r["condition_id"] for r in picked] == ["mid15"]


def test_chainlink_age_uses_recv_not_print_ts():
    import time

    from app.chainlink import ChainlinkTape

    tape = ChainlinkTape(symbols=("btc/usd",))
    old = time.time() - 400
    tape.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100, "timestamp": old}}
    )
    assert tape.age_ms("btc/usd") < 2000
    pub = tape.public()
    assert pub["symbols"]["btc/usd"]["age_ms"] < 2000


def test_rev28_does_not_open_user_scan_filters(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev28.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(11)
    st.patch_settings(
        strategy_rev=27,
        strategy_mode="twap",
        tags=["5M"],
        assets=["btc", "eth"],
        twap_assets=["btc", "eth"],
        twap_max_left=280.0,
        scan_limit=24,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth"]
    assert int(s.get("scan_limit") or 0) == 40
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]


def test_ws_wanted_tokens_skips_future_and_far_15m():
    from app.runtime import ws_wanted_tokens
    from app.twap import TwapParams

    params = TwapParams(assets=("btc", "eth", "sol"), horizons=("5m", "15m"), max_left=280.0)
    events = [
        {
            "slug": "btc-updown-5m-1",
            "condition_id": "c5",
            "up_token": "u5",
            "down_token": "d5",
            "end": _late_end(200),
        },
        {
            "slug": "eth-updown-5m-2",
            "condition_id": "cnext",
            "up_token": "unext",
            "down_token": "dnext",
            "end": _late_end(400),
        },
        {
            "slug": "sol-updown-15m-3",
            "condition_id": "c15",
            "up_token": "u15",
            "down_token": "d15",
            "end": _late_end(600),
        },
        {
            "slug": "sol-updown-15m-3",
            "condition_id": "chold",
            "up_token": "uhold",
            "down_token": "dhold",
            "end": _late_end(600),
        },
        {
            "slug": "btc-updown-15m-4",
            "condition_id": "c15h",
            "up_token": "u15h",
            "down_token": "d15h",
            "end": _late_end(300),
        },
    ]
    got = ws_wanted_tokens(
        events,
        params=params,
        hold_condition_ids={"chold"},
        extra_tokens=["restu"],
        ptb_slugs={"btc-updown-5m-1"},
    )
    assert "u5" in got and "d5" in got
    assert "unext" not in got and "dnext" not in got
    assert "u15" not in got
    assert "uhold" in got and "dhold" in got
    assert "u15h" not in got
    assert "restu" in got
    assert len(got) <= 8

    with_ptb = ws_wanted_tokens(
        events,
        params=params,
        hold_condition_ids={"chold"},
        ptb_slugs={"btc-updown-5m-1", "btc-updown-15m-4"},
    )
    assert "u15h" in with_ptb and "d15h" in with_ptb


def test_ws_wanted_tokens_caps_and_prefers_5m_with_ptb():
    from app.runtime import WS_MAX_TOKENS, ws_wanted_tokens
    from app.twap import TwapParams

    params = TwapParams(assets=("btc", "eth", "sol", "xrp"), horizons=("5m", "15m"), max_left=280.0)
    events = []
    ptb = set()
    for i, asset in enumerate(("btc", "eth", "sol", "xrp")):
        five = f"{asset}-updown-5m-{i}"
        fifteen = f"{asset}-updown-15m-{i}"
        ptb.add(five)
        ptb.add(fifteen)
        events.append(
            {
                "slug": five,
                "condition_id": f"c5{i}",
                "up_token": f"u5{i}",
                "down_token": f"d5{i}",
                "end": _late_end(200),
            }
        )
        events.append(
            {
                "slug": fifteen,
                "condition_id": f"c15{i}",
                "up_token": f"u15{i}",
                "down_token": f"d15{i}",
                "end": _late_end(250),
            }
        )
    got = ws_wanted_tokens(events, params=params, ptb_slugs=ptb)
    assert len(got) <= WS_MAX_TOKENS
    for i in range(4):
        assert f"u5{i}" in got and f"d5{i}" in got
    # 4×5m = 8 tokens, remaining cap 6 → at most three 15m books
    fifteen_n = sum(1 for t in got if t.startswith("u15") or t.startswith("d15"))
    assert fifteen_n <= 6


def test_ws_token_shards_split_fourteen():
    from app.runtime import ws_token_shards

    toks = [f"t{i}" for i in range(14)]
    shards = ws_token_shards(toks)
    assert shards == [toks[:8], toks[8:]]
    assert max(len(s) for s in shards) <= 8


def test_ws_wanted_seven_5m_excludes_15m():
    from app.runtime import WS_MAX_TOKENS, ws_wanted_tokens
    from app.twap import TwapParams

    assets = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
    params = TwapParams(assets=assets, horizons=("5m", "15m"), max_left=280.0)
    events = []
    ptb = set()
    for i, asset in enumerate(assets):
        five = f"{asset}-updown-5m-{i}"
        fifteen = f"{asset}-updown-15m-{i}"
        ptb.add(five)
        ptb.add(fifteen)
        events.append(
            {
                "slug": five,
                "condition_id": f"c5{i}",
                "up_token": f"u5{i}",
                "down_token": f"d5{i}",
                "end": _late_end(200),
            }
        )
        events.append(
            {
                "slug": fifteen,
                "condition_id": f"c15{i}",
                "up_token": f"u15{i}",
                "down_token": f"d15{i}",
                "end": _late_end(250),
            }
        )
    got = ws_wanted_tokens(events, params=params, ptb_slugs=ptb)
    assert len(got) == 14
    assert len(got) <= WS_MAX_TOKENS
    assert not any(t.startswith("u15") or t.startswith("d15") for t in got)


def test_ws_wanted_prefers_inband_15m_over_locked_5m():
    from app.runtime import ws_wanted_tokens
    from app.twap import TwapParams

    params = TwapParams(assets=("btc", "eth", "sol"), horizons=("5m", "15m"), max_left=280.0)
    events = [
        {
            "slug": "btc-updown-5m-1",
            "condition_id": "c5",
            "up_token": "u5",
            "down_token": "d5",
            "end": _late_end(80),
            "best_ask": 0.99,
        },
        {
            "slug": "eth-updown-15m-1",
            "condition_id": "c15",
            "up_token": "u15",
            "down_token": "d15",
            "end": _late_end(90),
            "best_ask": 0.51,
        },
        {
            "slug": "sol-updown-5m-1",
            "condition_id": "c5s",
            "up_token": "u5s",
            "down_token": "d5s",
            "end": _late_end(70),
            "best_ask": 0.03,
        },
    ]
    ptb = {"btc-updown-5m-1", "eth-updown-15m-1", "sol-updown-5m-1"}
    got = ws_wanted_tokens(events, params=params, ptb_slugs=ptb, max_tokens=2)
    assert "u15" in got and "d15" in got
    assert "u5" not in got
    assert "u5s" not in got


def test_ws_prewarm_future_uses_horizon_not_hunt_max():
    from app.runtime import ws_prewarm_future

    # 45s before T0: left = 300 + 45 = 345. Live probe was 345.1.
    assert ws_prewarm_future(345.1, 300) is True
    assert ws_prewarm_future(320.0, 300) is True
    # 100s before T0 is too early (would steal slots all cycle)
    assert ws_prewarm_future(400.0, 300) is False
    # current window is not a pre-warm
    assert ws_prewarm_future(200.0, 300) is False
    # next+1
    assert ws_prewarm_future(645.0, 300) is False


def test_ws_wanted_prewarms_next_5m_drops_locked_pennies():
    from app.runtime import ws_wanted_tokens
    from app.twap import TwapParams

    params = TwapParams(assets=("btc", "eth", "sol"), horizons=("5m",), max_left=280.0)
    locked = {
        "slug": "btc-updown-5m-1",
        "condition_id": "clocked",
        "up_token": "ulock",
        "down_token": "dlock",
        "end": _late_end(40),
        "outcome_prices": [0.99, 0.01],
        "best_ask": 0.99,
    }
    nxt = {
        "slug": "eth-updown-5m-9",
        "condition_id": "cnext",
        "up_token": "unext",
        "down_token": "dnext",
        "end": _late_end(320),
        "outcome_prices": [0.47, 0.53],
    }
    far = {
        "slug": "sol-updown-5m-9",
        "condition_id": "cfar",
        "up_token": "ufar",
        "down_token": "dfar",
        "end": _late_end(400),
        "outcome_prices": [0.50, 0.50],
    }
    got = ws_wanted_tokens(
        [locked, nxt, far],
        params=params,
        ptb_slugs={"btc-updown-5m-1"},
        max_tokens=2,
    )
    assert "unext" in got and "dnext" in got
    assert "ulock" not in got and "dlock" not in got
    assert "ufar" not in got

    held = ws_wanted_tokens(
        [locked, nxt],
        params=params,
        hold_condition_ids={"clocked"},
        ptb_slugs={"btc-updown-5m-1"},
    )
    assert "ulock" in held and "dlock" in held
    assert "unext" in held


def test_ws_wanted_keeps_locked_current_until_prewarm_needs_cap():
    from app.runtime import WS_MAX_TOKENS, ws_wanted_tokens
    from app.twap import TwapParams

    params = TwapParams(assets=("btc", "eth"), horizons=("5m",), max_left=280.0)
    locked = {
        "slug": "btc-updown-5m-1",
        "condition_id": "clocked",
        "up_token": "ulock",
        "down_token": "dlock",
        "end": _late_end(80),
        "outcome_prices": [0.99, 0.01],
        "best_ask": 0.99,
    }
    # No next window → keep pennies so the socket does not reconnect.
    got = ws_wanted_tokens([locked], params=params, ptb_slugs={"btc-updown-5m-1"})
    assert "ulock" in got and "dlock" in got

    assets = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
    params7 = TwapParams(assets=assets, horizons=("5m",), max_left=280.0)
    events = []
    ptb = set()
    for i, asset in enumerate(assets):
        five = f"{asset}-updown-5m-{1000 + i}"
        nxt = f"{asset}-updown-5m-{2000 + i}"
        ptb.add(five)
        events.append(
            {
                "slug": five,
                "condition_id": f"c{i}",
                "up_token": f"u{i}",
                "down_token": f"d{i}",
                "end": _late_end(80),
                "outcome_prices": [0.99, 0.01],
            }
        )
        events.append(
            {
                "slug": nxt,
                "condition_id": f"n{i}",
                "up_token": f"nu{i}",
                "down_token": f"nd{i}",
                "end": _late_end(320),
                "outcome_prices": [0.50, 0.50],
            }
        )
    pre = ws_wanted_tokens(events, params=params7, ptb_slugs=ptb, max_tokens=WS_MAX_TOKENS)
    assert len(pre) == 14
    assert all(t.startswith("nu") or t.startswith("nd") for t in pre)


def test_rev36_pins_hysteresis_keeps_user_coins_and_paper(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev36.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(9)
    st.patch_settings(
        strategy_rev=35,
        strategy_mode="twap",
        tags=["5M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        twap_horizons=["5m"],
        max_open_markets=10,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert apply_strategy_rev(st) == 0


def test_rev35_pins_prewarm_keeps_user_coins_and_paper(tmp_path):
    from app.main import apply_strategy_rev
    from app.twap import default_params, slug_allowed

    st = Store(tmp_path / "rev35.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(9)
    st.patch_settings(
        strategy_rev=34,
        strategy_mode="twap",
        tags=["5M"],
        tag="5M",
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        twap_horizons=["5m"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["twap_horizons"] == ["5m"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert int(s.get("max_open_markets") or 0) == 10
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    p = default_params(s)
    assert p.horizons == ("5m",)
    assert slug_allowed("btc-updown-5m-1", p) is True
    assert slug_allowed("btc-updown-15m-1", p) is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_ws_band_rank_uses_outcome_prices_not_stale_gamma():
    from app.runtime import ws_band_rank, ws_wanted_tokens
    from app.twap import TwapParams

    locked = {
        "slug": "bnb-updown-5m-1",
        "best_ask": 0.50,
        "outcome_prices": [0.885, 0.115],
    }
    inband = {
        "slug": "hype-updown-15m-1",
        "best_ask": 0.55,
        "outcome_prices": [0.54, 0.46],
    }
    stale_15 = {
        "slug": "btc-updown-15m-1",
        "best_ask": 0.55,
        "outcome_prices": [0.695, 0.305],
    }
    assert ws_band_rank(locked) == 3
    assert ws_band_rank(inband) == 0
    assert ws_band_rank(stale_15) == 3
    assert ws_band_rank({"best_ask": 0.51}) == 0

    params = TwapParams(assets=("bnb", "hype", "btc"), horizons=("5m", "15m"), max_left=280.0)
    events = [
        {
            "slug": "bnb-updown-5m-1",
            "condition_id": "c5",
            "up_token": "u5",
            "down_token": "d5",
            "end": _late_end(90),
            "best_ask": 0.50,
            "outcome_prices": [0.885, 0.115],
        },
        {
            "slug": "hype-updown-15m-1",
            "condition_id": "c15",
            "up_token": "u15",
            "down_token": "d15",
            "end": _late_end(80),
            "best_ask": 0.55,
            "outcome_prices": [0.54, 0.46],
        },
        {
            "slug": "btc-updown-15m-1",
            "condition_id": "c15b",
            "up_token": "u15b",
            "down_token": "d15b",
            "end": _late_end(85),
            "best_ask": 0.55,
            "outcome_prices": [0.695, 0.305],
        },
    ]
    ptb = {"bnb-updown-5m-1", "hype-updown-15m-1", "btc-updown-15m-1"}
    got = ws_wanted_tokens(events, params=params, ptb_slugs=ptb, max_tokens=2)
    assert "u15" in got and "d15" in got
    assert "u5" not in got
    assert "u15b" not in got


def test_gate_better_prefers_lead_over_nearest_lock():
    from app.runtime import gate_better

    locked = {"slug": "hype-updown-5m-1", "left": 40.0, "lead_bps": 4.0, "ask": 1.0, "reason": "twap_band"}
    lead = {"slug": "btc-updown-5m-1", "left": 200.0, "lead_bps": 5.2, "ask": 0.51, "reason": "twap_lead"}
    noptb = {"slug": "btc-updown-15m-1", "left": 30.0, "lead_bps": None, "ask": 0.50, "reason": "twap_no_ptb"}
    signal = {"slug": "sol-updown-5m-1", "left": 90.0, "lead_bps": 8.0, "ask": 0.45, "reason": "signal"}
    assert gate_better(locked, lead) is True
    assert gate_better(lead, locked) is False
    assert gate_better(noptb, lead) is True
    assert gate_better(lead, signal) is True
    assert gate_better(None, locked) is True


def test_chainlink_ptb_persists_and_reloads_without_pre_open_ticks():
    from app.chainlink import ChainlinkTape

    start = 1_700_000_000 - (1_700_000_000 % 300)
    slug = f"btc-updown-5m-{start}"
    saved: list[tuple[str, float]] = []
    src = ChainlinkTape()
    src.persist_ptb = lambda s, p: saved.append((s, p))
    src.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100000, "timestamp": start - 1}}
    )
    src.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100010, "timestamp": start + 0.2}}
    )
    assert src.ensure_ptb(slug) == 100010.0
    assert (slug, 100010.0) in saved

    dst = ChainlinkTape()
    dst.load_ptb({slug: 100010.0})
    dst.apply_message(
        {"topic": "crypto_prices_chainlink", "type": "update", "payload": {"symbol": "btc/usd", "value": 100040, "timestamp": start + 20}}
    )
    assert dst.ensure_ptb(slug) == 100010.0


def test_runtime_loads_fresh_ptb_drops_expired(tmp_path):
    import json
    import time

    from app.config import Env
    from app.runtime import Runtime

    st = Store(tmp_path / "ptb.sqlite")
    st.ensure_paper(500)
    now = int(time.time())
    start = now - (now % 300)
    fresh = f"btc-updown-5m-{start}"
    old = f"btc-updown-5m-{start - 3600}"
    leftover_15 = f"eth-updown-15m-{start}"
    st.kv_set(f"ptb:{fresh}", json.dumps({"px": 99.5, "ts": now}))
    st.kv_set(f"ptb:{old}", json.dumps({"px": 1.0, "ts": now - 3600}))
    st.kv_set(f"ptb:{leftover_15}", json.dumps({"px": 88.0, "ts": now}))
    rt = Runtime(st, Env())
    assert rt.chainlink.ptb[fresh] == 99.5
    assert old not in rt.chainlink.ptb
    assert leftover_15 not in rt.chainlink.ptb
    assert st.kv_get(f"ptb:{old}") is None
    assert st.kv_get(f"ptb:{leftover_15}") is None


def test_rev30_does_not_reset_paper_or_user_filters(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev30.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(11)
    st.patch_settings(
        strategy_rev=29,
        strategy_mode="twap",
        tags=["5M", "15M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert int(s.get("max_open_markets") or 0) == 10
    assert int(s.get("scan_limit") or 0) == 40
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev31_does_not_reset_paper_or_user_filters(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev31.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(11)
    st.patch_settings(
        strategy_rev=30,
        strategy_mode="twap",
        tags=["5M", "15M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert int(s.get("max_open_markets") or 0) == 10
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev32_does_not_reset_paper_or_user_filters(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev32.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(11)
    st.patch_settings(
        strategy_rev=31,
        strategy_mode="twap",
        tags=["5M", "15M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev33_does_not_reset_paper_or_user_filters(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev33.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(7)
    st.patch_settings(
        strategy_rev=32,
        strategy_mode="twap",
        tags=["5M", "15M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["twap_max_left"]) == 280.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0


def test_rev34_pins_5m_only_keeps_user_coins_and_paper(tmp_path):
    from app.main import apply_strategy_rev
    from app.twap import default_params, slug_allowed

    st = Store(tmp_path / "rev34.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(9)
    st.patch_settings(
        strategy_rev=33,
        strategy_mode="twap",
        tags=["5M", "15M"],
        tag="5M",
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        twap_horizons=["5m", "15m"],
        max_open_markets=10,
        scan_limit=40,
        twap_max_left=280.0,
        live_trading=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 39
    assert s["tags"] == ["5M"]
    assert s["tag"] == "5M"
    assert s["twap_horizons"] == ["5m"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert int(s.get("max_open_markets") or 0) == 10
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    p = default_params(s)
    assert p.horizons == ("5m",)
    assert slug_allowed("btc-updown-5m-1", p) is True
    assert slug_allowed("btc-updown-15m-1", p) is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert after["total_pnl"] == before["total_pnl"]
    assert apply_strategy_rev(st) == 0







