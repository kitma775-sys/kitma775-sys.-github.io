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


def test_home_text_is_short_operator_board(tmp_path):
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
    assert "🧪 紙盤" in text
    assert "現金 $500.00" in text
    assert "本金 $500.00" in text
    assert "單筆 $5" in text
    assert "FOK" not in text
    assert "盤口 10 盤" not in text
    assert "Rev 45" not in text
    assert "預熱" not in text
    assert "唔做 YES+NO 互補" not in text
    assert "sol-updown-5m" not in text
    assert "可用 USDC" not in text


def test_telegram_dashboard_url_button(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import CLOB_STATUS_URL, dashboard_open_url, home_kb, home_text

    st = Store(tmp_path / "dashbtn.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    assert dashboard_open_url(rt) is None
    naked = [btn for row in home_kb(rt).inline_keyboard for btn in row]
    clob = next(b for b in naked if b.text == "📡 CLOB 狀態")
    assert clob.url == CLOB_STATUS_URL
    assert all(getattr(btn, "url", None) in (None, CLOB_STATUS_URL) for btn in naked)

    rt = Runtime(
        st,
        Env(dashboard_token="tok+/=x", dashboard_public_url="https://surf-arb.zeabur.app"),
    )
    url = dashboard_open_url(rt)
    assert url == "https://surf-arb.zeabur.app/?t=tok%2B%2F%3Dx"
    buttons = [btn for row in home_kb(rt).inline_keyboard for btn in row]
    dash = next(b for b in buttons if b.text == "🖥 開 Dashboard")
    clob = next(b for b in buttons if b.text == "📡 CLOB 狀態")
    assert dash.url == url
    assert clob.url == CLOB_STATUS_URL
    assert "tok+/=x" not in home_text(rt)
    assert "開 Dashboard" not in home_text(rt)
    assert "CLOB 狀態" not in home_text(rt)


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
    thin, thin_vwap, thin_floor = walk_dump(_L((0.87, 2), (0.40, 20)), 10, min_px=0.87)
    assert thin == 2
    assert abs(thin_vwap - 0.87) < 1e-9
    assert thin_floor == 0.87
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


def test_home_text_hides_scan_tape(tmp_path):
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
    assert "盤口 10 盤" not in text
    assert "btc-updown-15m" not in text
    assert "eth-updown-15m-a" not in text
    assert "ask合" not in text
    assert "taker淨" not in text
    assert "現金 $" in text


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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert body["strategy_rev"] == 60
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
    assert float(body.get("twap_min_left") or 0) == 120.0
    assert float(body.get("twap_late_left") or 0) == 0.0
    assert float(body.get("twap_late_min_price") or 0) == 0.45
    assert float(body.get("twap_alt_min_left") or 0) == 120.0
    assert float(body.get("twap_max_lead_bps") or 0) == 40.0
    assert float(body.get("twap_scratch_dump_floor") or 0) == 0.22
    assert float(body.get("twap_scratch_late_left") or 0) == 0.0
    assert float(body.get("twap_scratch_late_bid") or 0) == 1.0
    assert body.get("twap_reverse") is False
    assert float(body.get("twap_tp_bid") or 0) == 0.87
    assert float(body.get("twap_confirm_px") or 0) == 0.62
    assert float(body.get("twap_confirm_left") or 0) == 90.0
    assert abs(float(body.get("twap_confirm_fair") or 0) - 0.60) < 1e-9
    assert body.get("twap_no_cheaper") is True
    assert abs(float(body.get("twap_up_tick") or 0) - 0.01) < 1e-9
    assert abs(float(body.get("twap_scratch_hot_ms") or 0) - 2000.0) < 1e-9
    assert abs(float(body.get("twap_rescore_hot_seconds") or 0) - 3.0) < 1e-9
    assert body.get("twap_assets") == ["btc", "eth"]
    assert body.get("twap_horizons") == ["5m"]
    assert float(body.get("clob_rtt_ms") or 0) == 150.0
    assert "clob_ws_wanted_n" in body
    assert "clob_ws_slugs" in body
    assert body.get("twap_ptb_n") == 0
    assert body.get("last_ws_error") in (None, "")
    assert "engine_running" in body
    assert body.get("circuit") is False
    assert body.get("clob_halted") is False
    state = client.get("/api/state?t=tok").json()
    assert state["ws_status"] == "connected"
    assert "ws_status" in state
    assert state["board"]["mode"] == "paper"
    assert state["board"]["cash_label"] == "紙盤現金"
    assert state["board"]["cash"] == 500.0
    assert "可用 USDC" not in (state["board"].get("cash_label") or "")


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
    assert "$-10.80" not in log
    assert "-$10.80" in log


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
    from app.broker import already_redeemed, redeem_not_ready

    assert already_redeemed("UserInputError: You have no positions")
    assert already_redeemed("nothing to redeem")
    assert not already_redeemed("nonce too low")
    assert not already_redeemed("")
    assert not already_redeemed("Gasless transactions require a Builder API Key")
    assert redeem_not_ready("No market found for condition 0xabc")
    assert redeem_not_ready("Gasless transactions require a Builder API Key or Relayer API Key.")
    assert not redeem_not_ready("nonce too low")


def test_live_redeem_empty_position_counts_as_settled():
    import asyncio

    from app.broker import LiveBroker

    class Boom(Exception):
        pass

    class FakeClient:
        def __init__(self):
            self.calls = 0
            self.msg = "Gasless transactions require a Builder API Key or Relayer API Key."

        async def redeem_positions(self, condition_id):
            self.calls += 1
            raise Boom(self.msg)

    broker = LiveBroker("0xabc", wallet="0xC8a8dEF991F2FC0fa7322b9374A682848615b3db")
    client = FakeClient()
    broker._client = client

    async def empty(_cid):
        return 0.0

    broker.condition_token_size = empty
    result = asyncio.run(broker.redeem("0xdfb67e96ca73a866757ddfda21ca9be1a4c9cb4d7483d945d77f0ae668237200"))
    assert result.ok is True
    assert result.payload["already"] is True
    assert client.calls == 0

    async def held(_cid):
        return 6.8

    broker.condition_token_size = held
    client.msg = "No market found for condition 0x601e6540403f71a02a3bbec796423f006c2df9eaf4545b4100376f9981b769ae"
    stuck = asyncio.run(broker.redeem("0x601e6540403f71a02a3bbec796423f006c2df9eaf4545b4100376f9981b769ae"))
    assert stuck.ok is False
    assert stuck.status == "redeem_wait"
    assert "No market found" in stuck.detail
    assert client.calls == 1


def test_live_redeem_already_empty_clears_twap_live(tmp_path):
    import asyncio

    from app.broker import FillResult
    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved

    class Spy:
        mode = "live"

        async def redeem(self, condition_id):
            return FillResult(
                True,
                "redeemed",
                "live",
                "already empty",
                {"condition_id": condition_id, "already": True},
            )

        async def list_redeemable(self):
            return []

    st = Store(tmp_path / "live-redeem-empty.sqlite")
    st.ensure_paper(500)
    st.add_inventory(
        "0xhype",
        "hype-updown-5m-1788174300",
        0.0,
        6.857141,
        kind="twap_live",
        cost=2.88,
    )
    st.patch_settings(live_trading=True, auto_redeem=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.skip_live_preflight = True
    rt._broker = Spy()
    rt._broker_mode = "live"
    rt.data = _FakeGamma(
        {"hype-updown-5m-1788174300": {"closed": True, "markets": [{"closed": True, "outcomePrices": ["0", "1"]}]}}
    )
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 1
    assert st.inventory_open() == []
    trade = st.recent_trades(1)[0]
    assert trade["status"] == "redeemed"
    assert abs(float(trade["net"]) - (6.857141 - 2.88)) < 1e-5


def test_redeem_wait_logs_once_then_settles_when_empty(tmp_path):
    import asyncio

    from app.broker import FillResult
    from app.config import Env
    from app.runtime import Runtime, _redeem_resolved, operator_board
    from app.wall import operator_wall

    class WaitThenEmpty:
        mode = "live"

        def __init__(self):
            self.redeem_calls = 0
            self.held = 5.3

        async def redeem(self, condition_id):
            self.redeem_calls += 1
            return FillResult(
                False,
                "redeem_wait",
                "live",
                f"No market found for condition {condition_id}",
                {"condition_id": condition_id, "wait": True},
            )

        async def condition_token_size(self, condition_id):
            return self.held

        async def list_redeemable(self):
            return [
                {
                    "condition_id": "0xweather",
                    "slug": "highest-temperature-in-nyc",
                    "size": 12.0,
                }
            ]

    st = Store(tmp_path / "redeem-wait.sqlite")
    st.ensure_paper(500)
    st.add_inventory(
        "0xxrp",
        "xrp-updown-5m-1788178800",
        0.0,
        5.3,
        kind="twap_live",
        cost=2.65,
    )
    st.patch_settings(live_trading=True, auto_redeem=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.skip_live_preflight = True
    spy = WaitThenEmpty()
    rt._broker = spy
    rt._broker_mode = "live"
    rt.data = _FakeGamma(
        {"xrp-updown-5m-1788178800": {"closed": True, "markets": [{"closed": True, "outcomePrices": ["1", "0"]}]}}
    )
    assert asyncio.run(_redeem_resolved(rt)) == 0
    assert asyncio.run(_redeem_resolved(rt)) == 0
    assert spy.redeem_calls == 1
    notes = [e["message"] for e in st.recent_events(20) if "redeem" in str(e.get("message") or "")]
    assert len(notes) == 1
    assert notes[0].startswith("redeem 等結算")
    assert "fail" not in notes[0]
    assert st.inventory_one("0xxrp")["down"] == 5.3
    st.add_event("warn", "redeem fail xrp-updown-5m-1788178800: No market found for condition 0xabc")
    wall = operator_wall(rt, operator_board(rt))
    texts = " ".join(row.get("text") or "" for row in wall["log"])
    assert "No market found" not in texts
    assert "等結算" in texts

    spy.held = 0.0
    rt.cooldown.clear()
    n = asyncio.run(_redeem_resolved(rt))
    assert n == 1
    assert spy.redeem_calls == 1
    assert st.inventory_open() == []
    trade = st.recent_trades(1)[0]
    assert trade["status"] == "redeemed"
    assert abs(float(trade["net"]) - (0.0 - 2.65)) < 1e-5


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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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


def test_clob_sell_floors_dust_and_retries_wallet_shortfall():
    import asyncio

    from app.broker import (
        LiveBroker,
        clob_sell_shares,
        sell_fak_kwargs,
        token_balance_shares,
    )

    assert clob_sell_shares(5.489794) == 5.48
    assert clob_sell_shares(5.49) == 5.49
    assert clob_sell_shares(10) == 10.0
    assert sell_fak_kwargs(token_id="u", shares=5.489794, min_price=0.4)["shares"] == "5.48"
    detail = "not enough balance / allowance: the balance is not enough -> balance: 5489794, order amount: 5490000"
    assert abs(token_balance_shares(detail) - 5.489794) < 1e-9

    calls = []

    class Boom(Exception):
        pass

    class FakeClient:
        async def place_market_order(self, **kw):
            calls.append(kw)
            if kw.get("shares") == "5.49":
                raise Boom(detail)
            return SimpleOrder(ok=True, status="matched", order_id="s1")

    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_sell("tok", 5.49, 0.38))
    assert result.ok is True
    assert [c["shares"] for c in calls] == ["5.49", "5.48"]


def test_journal_hides_dump_balance_dust(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.wall import operator_wall

    st = Store(tmp_path / "dump-dust-log.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    st.add_event(
        "warn",
        "dump fail eth-updown-5m-1788180300: not enough balance / allowance: the balance is not enough -> balance: 5489794, order amount: 5490000",
    )
    st.add_event("warn", "dump fail eth-updown-5m-1: sell FAK not matched (live)")
    texts = " ".join(row.get("text") or "" for row in operator_wall(rt, operator_board(rt))["log"])
    assert "not enough balance" not in texts
    assert "not matched" in texts


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
    trade = st.recent_trades(1)[0]
    assert trade["status"] == "dumped"
    from app.fees import taker_fee

    fee = taker_fee(10.0, 0.48, 0.07)
    assert abs(float(trade["net"]) - (4.8 - fee - 5.0)) < 1e-5
    assert abs(float(trade["payload"]["proceeds"]) - (4.8 - fee)) < 1e-5


def test_is_clob_unavailable_matches_503_and_disabled():
    from app.runtime import is_clob_unavailable

    assert is_clob_unavailable("trading is disabled") is True
    assert is_clob_unavailable("Trading is currently disabled") is True
    assert is_clob_unavailable("order rejected", http_status=503) is True
    assert is_clob_unavailable("order rejected", http_status="503") is True
    assert is_clob_unavailable("not enough balance") is False
    assert is_clob_unavailable("invalid amount", http_status=400) is False
    assert is_clob_unavailable("cancel-only mode") is True


def test_trip_clob_halt_only_fresh_once(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, clob_halt_seconds

    st = Store(tmp_path / "halt.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    assert rt.clob_halted() is False
    assert clob_halt_seconds("trading is disabled") == 300.0
    assert clob_halt_seconds("cancel-only", retry_after=79) == 79.0
    assert rt.trip_clob_halt("trading is disabled", seconds=90) is True
    assert rt.clob_halted() is True
    assert rt.trip_clob_halt("trading is disabled", seconds=90) is False
    rt._clob_halt_until = 0
    assert rt.clob_halted() is False
    assert rt.trip_clob_halt("trading is disabled", seconds=90) is False
    assert rt.clob_halted() is True
    assert abs(float(rt._clob_halt_backoff) - 180.0) < 1e-9
    rt.clear_clob_halt()
    assert rt.trip_clob_halt("trading is disabled", seconds=90) is True
    assert "trading is disabled" in rt._clob_halt_reason


def test_live_execute_pair_attaches_503_status():
    import asyncio

    from app.broker import LiveBroker
    from app.hunter import Setup
    from app.runtime import is_clob_unavailable

    class Boom(Exception):
        def __init__(self):
            super().__init__("trading is disabled")
            self.status = 503

    class FakeClient:
        async def place_market_order(self, **kw):
            raise Boom()

    setup = Setup(
        slug="btc",
        title="btc",
        condition_id="0x1",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.51,
        down_price=0.0,
        shares=5.5,
        fillable=5.5,
        gross=0.1,
        fees=0.006,
        net=0.5,
        tail=True,
        extra={"strategy": "twap", "leg": "up"},
    )
    broker = LiveBroker("0xabc")
    broker._client = FakeClient()
    result = asyncio.run(broker.execute_pair(setup))
    assert result.ok is False
    assert result.status == "error"
    assert int(result.payload["http_status"]) == 503
    assert is_clob_unavailable(result.detail, http_status=result.payload["http_status"])


def test_scratch_twap_skips_when_clob_halted(tmp_path):
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

    st = Store(tmp_path / "scratch-halt.sqlite")
    st.ensure_paper(500)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap_live", cost=5.0)
    st.patch_settings(live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    spy = Spy()
    rt._broker = spy
    rt._broker_mode = "live"
    rt.trip_clob_halt("trading is disabled", seconds=90)
    _put_dumpable_books(rt)
    n = asyncio.run(_scratch_twap(rt, [_scratch_event()]))
    assert n == 0
    assert spy.sells == 0
    assert abs(float(st.inventory_one("cid-btc")["down"]) - 10.0) < 1e-9


def test_scratch_twap_hot_books_http_over_stale_ws(tmp_path):
    import asyncio
    import time

    from app.config import Env
    from app.hunter import Level
    from app.runtime import Runtime, _scratch_twap

    class Http22:
        n = 0

        async def book(self, token):
            Http22.n += 1
            return {"asks": _L((0.23, 40)), "bids": _L((0.22, 40))}

    st = Store(tmp_path / "scratch-hot.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(5.0)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env(force_paper=True))
    rt.data = Http22()
    now_ms = time.time() * 1000.0
    stale = [Level(0.50, 40.0)]
    asks = [Level(0.51, 40.0)]
    rt.books.put("u", asks, stale, ts_ms=now_ms - 50_000, source="ws")
    rt.books.put("d", asks, stale, ts_ms=now_ms - 50_000, source="ws")
    ev = _scratch_event()
    ev["end"] = _late_end(80)
    rt._twap_ev[ev["condition_id"]] = ev
    n = asyncio.run(_scratch_twap(rt, []))
    assert n == 1
    assert Http22.n >= 2
    trade = st.recent_trades(1)[0]
    assert trade["status"] == "paper_dumped"
    assert abs(float(trade["payload"]["floor_px"]) - 0.22) < 1e-9
    assert st.inventory_open() == []


def test_scratch_twap_keeps_22c_floor_on_fresh_http(tmp_path):
    import asyncio
    import time

    from app.config import Env
    from app.hunter import Level
    from app.runtime import Runtime, _scratch_twap

    class Http10:
        async def book(self, token):
            return {"asks": _L((0.11, 40)), "bids": _L((0.10, 40))}

    st = Store(tmp_path / "scratch-floor.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(5.0)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env(force_paper=True))
    rt.data = Http10()
    now_ms = time.time() * 1000.0
    stale = [Level(0.50, 40.0)]
    asks = [Level(0.51, 40.0)]
    rt.books.put("u", asks, stale, ts_ms=now_ms - 50_000, source="ws")
    rt.books.put("d", asks, stale, ts_ms=now_ms - 50_000, source="ws")
    ev = _scratch_event()
    ev["end"] = _late_end(80)
    n = asyncio.run(_scratch_twap(rt, [ev]))
    assert n == 0
    assert abs(float(st.inventory_one("cid-btc")["down"]) - 10.0) < 1e-9


def test_scratch_twap_must_dump_allows_partial_below_min_shares(tmp_path):
    import asyncio

    from app.config import Env
    from app.hunter import Level
    from app.runtime import Runtime, _scratch_twap

    st = Store(tmp_path / "scratch-partial.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(5.0)
    st.add_inventory("cid-btc", "btc-updown-5m-1000", 0.0, 10.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env(force_paper=True))
    now_ms = __import__("time").time() * 1000.0
    bids = [Level(0.50, 3.0)]
    asks = [Level(0.51, 40.0)]
    rt.books.put("u", asks, bids, ts_ms=now_ms, source="test")
    rt.books.put("d", asks, bids, ts_ms=now_ms, source="test")
    ev = _scratch_event()
    ev["end"] = _late_end(80)
    n = asyncio.run(_scratch_twap(rt, [ev]))
    assert n == 1
    left = float(st.inventory_one("cid-btc")["down"])
    assert abs(left - 7.0) < 1e-9
    assert abs(float(st.recent_trades(1)[0]["shares"]) - 3.0) < 1e-9
    import asyncio

    from app.config import Env
    from app.runtime import Runtime, _scan_markets

    class Spy:
        mode = "live"

        def __init__(self):
            self.buys = 0

        async def execute_pair(self, setup):
            self.buys += 1
            raise AssertionError("halted CLOB must not post")

        async def execute_sell(self, token_id, shares, min_price):
            raise AssertionError("halted CLOB must not dump")

    st = Store(tmp_path / "scan-halt.sqlite")
    st.ensure_paper(500)
    st.patch_settings(live_trading=True, engine_running=True, auto_execute=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    spy = Spy()
    rt._broker = spy
    rt._broker_mode = "live"
    rt.data = object()
    rt.books.connected = True
    rt.books.wanted = ("u", "d")
    _put_dumpable_books(rt)
    rt.trip_clob_halt("trading is disabled", seconds=90)
    ev = {
        "condition_id": "cid-btc",
        "slug": "btc-updown-5m-1000",
        "title": "btc 5m",
        "end": _late_end(90),
        "up_token": "u",
        "down_token": "d",
        "fee_rate": 0.07,
        "min_size": 5,
    }
    asyncio.run(_scan_markets(rt, [ev]))
    assert spy.buys == 0
    tape = (rt.last_loop or {}).get("tape") or {}
    assert int((tape.get("twap_skips") or {}).get("clob_halt") or 0) >= 1


def test_home_text_shows_clob_halt(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.telegram_ui import home_text

    st = Store(tmp_path / "halt-home.sqlite")
    st.ensure_paper(500)
    st.patch_settings(live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.trip_clob_halt("trading is disabled", seconds=90)
    text = home_text(rt)
    assert "全站暫停" in text
    assert "https://status.polymarket.com" in text
    assert "status.polymarket.com" in text
    assert "可用 USDC" in text
    assert "本金 $" not in text
    assert "唔係錢包" not in text
    assert "CLOB 暫時唔收單" not in text


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
    assert rt.notices.empty()


def test_live_leftover_paper_does_not_block_twap(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, leftover_paper_inventory, twap_conflict_open
    from app.telegram_ui import _log_text, _pos_text, home_text

    st = Store(tmp_path / "live-paper-conflict.sqlite")
    st.ensure_paper(500)
    st.add_inventory("cid-eth-paper", "eth-updown-5m-1788166200", 5.69, 0.0, kind="twap", cost=3.0)
    st.add_trade(
        slug="eth-updown-5m-1788166200",
        kind="settle",
        shares=5.69,
        up_price=1.0,
        down_price=0.0,
        net=2.69,
        mode="paper",
        status="paper_settled",
    )
    st.patch_settings(live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 19.76
    assert leftover_paper_inventory(rt)[0]["slug"] == "eth-updown-5m-1788166200"
    assert twap_conflict_open(rt, "eth-updown-5m-1788166500") is False
    pos = _pos_text(rt)
    assert "eth-updown-5m-1788166200" not in pos
    assert "紙盤剩倉 1 檔" in pos
    home = home_text(rt)
    assert "可用 USDC $19.76" in home
    assert "單筆 $" in home
    assert "紙盤帳" not in home
    assert "本金 $" not in home
    assert "紙盤剩倉" not in home
    log = _log_text(rt)
    assert "eth-updown-5m-1788166200" not in log
    assert "paper_settled" not in log
    snap = rt.snapshot()
    assert snap["leftover_paper_n"] == 1
    assert snap["inventory"] == []
    assert snap["trades"] == []
    assert snap["board"]["mode"] == "live"
    assert snap["board"]["cash"] == 19.76
    assert snap["board"]["leftover_paper_n"] == 1
    assert snap["board"]["notes"] == [
        "💰 止賺 87¢：全倉 bid 夠價先走，弱倉 scratch 照舊",
        "🔒 第一下 6bps 唔追平；可加 1¢；90s 未印 62¢ dump；oracle <0.60 都 dump；最後90s新鮮盤",
        "🎯 只hunt BTC+ETH",
    ]


def test_operator_board_splits_live_and_paper(tmp_path):
    from pathlib import Path

    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.telegram_ui import home_kb, home_text, mode_text

    st = Store(tmp_path / "board.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3)
    rt = Runtime(st, Env())
    paper = operator_board(rt)
    assert paper["mode"] == "paper"
    assert paper["cash"] == 500.0
    assert paper["starting"] == 500.0
    assert paper["cash_label"] == "紙盤現金"
    home = home_text(rt)
    assert "可用 USDC" not in home
    assert "本金 $500.00" in home
    assert "單筆 $3" in home
    labels = " ".join(btn.text for row in home_kb(rt).inline_keyboard for btn in row)
    assert "紙盤本金" in labels
    assert "CLOB 狀態" in labels
    assert "下次重置本金 $500" in mode_text(rt)

    st.patch_settings(live_trading=True)
    live_rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    live_rt.live_usdc = 19.76
    live = operator_board(live_rt)
    assert live["mode"] == "live"
    assert live["cash"] == 19.76
    assert live["cash_label"] == "可用 USDC"
    assert live["starting"] is None
    assert live["equity"] is None
    live_home = home_text(live_rt)
    assert "可用 USDC $19.76" in live_home
    assert "今日PnL" in live_home
    assert "本金 $" not in live_home
    assert "現金 $" not in live_home
    assert "權益 $" not in live_home
    live_labels = " ".join(btn.text for row in home_kb(live_rt).inline_keyboard for btn in row)
    assert "紙盤本金" not in live_labels
    assert "重置紙盤" not in live_labels
    assert "可用 USDC $19.76" in mode_text(live_rt)
    assert "下次重置本金" not in mode_text(live_rt)
    dash = Path(__file__).resolve().parents[1] / "app" / "dashboard.html"
    html = dash.read_text(encoding="utf-8")
    assert "可用 USDC" in html
    assert "d.board" in html
    assert "d.wall" in html
    assert 'id="bankBlock"' in html
    assert "@media (max-width: 860px)" in html
    assert "SURF · 5M TWAP WALL" in html
    assert "Asia/Hong_Kong" in html
    assert 'id="hero"' in html
    assert 'id="curve"' in html
    assert 'id="todayPnl"' in html
    assert 'id="curveLeg"' in html
    assert "掃描日誌" in html
    assert "運行日誌" in html
    assert "命中" in html
    assert "drawCurve" in html
    assert '"hero"' in html
    assert "唔包入金" in html
    assert "Math.abs" in html
    assert "今日已實現 PnL" in html
    assert "twap_confirm_fair" in html
    assert "twap_confirm_px" in html
    assert "oracle <" in html
    assert "可加 " in html
    assert "新鮮盤" in html


def test_format_log_ts_is_hong_kong():
    from datetime import datetime, timezone

    from app.config import format_log_ts
    from app.telegram_ui import _fmt_ts

    ts = datetime(2026, 8, 31, 10, 0, 0, tzinfo=timezone.utc).timestamp()
    assert format_log_ts(ts) == "18:00:00"
    assert _fmt_ts(ts) == "18:00:00"
    assert format_log_ts(None) == ""
    assert "Z" not in format_log_ts(ts)


def test_operator_wall_tape_and_mode_money(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.telegram_ui import _log_text, home_text
    from app.wall import note_wall_gate, operator_wall

    st = Store(tmp_path / "wall.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3, live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 19.76
    rt.ws_status = "connected"
    rt.chainlink_status = "connected"
    note_wall_gate(
        rt,
        {"slug": "btc-updown-5m-1788171600", "reason": "twap_lead", "lead_bps": 3.1, "ask": 0.49, "left": 80},
    )
    note_wall_gate(
        rt,
        {"slug": "eth-updown-5m-1788171600", "reason": "signal", "lead_bps": 7.2, "ask": 0.48, "left": 70, "side": "up"},
    )
    note_wall_gate(
        rt,
        {"slug": "btc-updown-5m-1788171600", "reason": "twap_lead", "lead_bps": 3.4, "ask": 0.50, "left": 60},
    )
    assert sum(1 for row in rt.wall_tape if row["asset"] == "btc") == 1
    board = operator_board(rt)
    wall = operator_wall(rt, board)
    assert wall["board"]["cash"] == 19.76
    assert wall["board"]["cash_label"] == "可用 USDC"
    assert wall["board"]["starting"] is None
    btc = next(s for s in wall["slots"] if s["asset"] == "btc")
    eth = next(s for s in wall["slots"] if s["asset"] == "eth")
    assert btc["status"] == "SKIP"
    assert eth["status"] == "PASS"
    assert any(g["id"] == "ws" and g["pct"] == 100 for g in wall["gauges"])
    snap = rt.snapshot()
    assert snap["wall"]["board"]["cash"] == 19.76
    log = _log_text(rt)
    assert "📜 日誌" in log
    assert "SKIP" in log
    assert "btc-updown-5m" in log
    assert "Z" not in log
    assert "本金 $" not in home_text(rt)
    assert "霓虹監察牆" not in home_text(rt)
    assert home_text(rt).count("命中") == 1
    assert "命中 —" in home_text(rt)
    assert wall["curve"]["hit_label"] == "—"
    assert wall["curve"]["label"] == "今日已實現 PnL"
    assert wall["curve"]["note"]
    assert board["hit_held"] == 0


def test_performance_today_hit_rate_and_curve_align_telegram(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.telegram_ui import home_text, mode_text
    from app.wall import performance_today, operator_wall

    st = Store(tmp_path / "curve.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3, live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 20.84
    st.add_trade(slug="sol-updown-5m-1", kind="twap_live", shares=5, up_price=0, down_price=0.47, net=-0.337, mode="live", status="dumped")
    st.add_trade(slug="hype-updown-5m-1", kind="settle", shares=6.86, up_price=0, down_price=1, net=3.977, mode="live", status="redeemed")
    st.add_trade(slug="xrp-updown-5m-1", kind="settle", shares=5.1, up_price=0, down_price=1, net=-2.6, mode="live", status="redeemed")
    st.add_trade(slug="sol-updown-5m-2", kind="settle", shares=6.3, up_price=1, down_price=0, net=-2.9, mode="live", status="redeemed")
    st.add_trade(slug="sol-updown-5m-2", kind="taker", shares=6.3, up_price=0, down_price=0.46, net=0.0, mode="live", status="filled")
    st.add_trade(slug="eth-updown-5m-9", kind="settle", shares=5, up_price=1, down_price=0, net=2.0, mode="paper", status="paper_settled")
    perf = performance_today(rt)
    assert perf["wins"] == 1
    assert perf["losses"] == 2
    assert perf["held"] == 3
    assert perf["scratch_n"] == 1
    assert perf["hit_label"] == "1/3"
    assert abs(float(perf["hit_rate"]) - (1 / 3)) < 1e-3
    assert abs(float(perf["end"]) - (3.977 - 2.6 - 2.9 - 0.337)) < 1e-6
    board = operator_board(rt)
    assert board["hit_label"] == "1/3"
    assert board["scratch_n"] == 1
    assert abs(float(board["today_pnl"]) - float(perf["end"])) < 1e-6
    home = home_text(rt)
    assert "命中 1/3" in home
    assert "scratch 1" in home
    assert "本金 $" not in home
    assert "可用 USDC $20.84" in home
    assert "命中 1/3" in mode_text(rt)
    wall = operator_wall(rt, board)
    assert wall["curve"]["hit_label"] == "1/3"
    marks = [p["mark"] for p in wall["curve"]["points"] if p["mark"] not in {"start", "now"}]
    assert marks.count("win") == 1
    assert marks.count("lose") == 2
    assert marks.count("scratch") == 1
    assert wall["curve"]["label"] == "今日已實現 PnL"
    assert "入金" in (wall["curve"].get("note") or "")
    assert abs(float(wall["curve"]["end"]) - float(board["today_pnl"])) < 1e-6
    paper_rt = Runtime(st, Env(force_paper=True))
    paper = performance_today(paper_rt)
    assert paper["label"] == "今日權益"
    assert paper["wins"] == 1
    assert paper["hit_label"] == "1/1"


def test_performance_today_curve_follows_window_close_not_batch_redeem(tmp_path):
    from app.config import Env
    from app.runtime import Runtime
    from app.wall import performance_today, utc_day_start
    import time

    st = Store(tmp_path / "curve-order.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3, live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 20.84
    day = int(utc_day_start())
    now_s = time.time()
    preferred = day + 11 * 3600
    # 11:00 UTC is after "now" in early UTC hours; keep the sequence in the past
    # so the curve can append the trailing now-point.
    sol0 = preferred if preferred + 1800 < now_s else int(now_s - 1800)
    sol0 = max(day + 5, sol0)
    hype_w = sol0 + 300
    xrp_w = sol0 + 300
    sol1 = sol0 + 900
    batch = sol0 + 1760
    st.add_trade(
        ts=sol0 + 212,
        slug=f"sol-updown-5m-{sol0}",
        kind="twap_live",
        shares=5,
        up_price=0,
        down_price=0.47,
        net=-0.3372,
        mode="live",
        status="dumped",
    )
    st.add_trade(
        ts=batch,
        slug=f"sol-updown-5m-{sol1}",
        kind="settle",
        shares=6.3,
        up_price=1,
        down_price=0,
        net=-2.9,
        mode="live",
        status="redeemed",
    )
    st.add_trade(
        ts=batch + 0.03,
        slug=f"xrp-updown-5m-{xrp_w}",
        kind="settle",
        shares=5.1,
        up_price=0,
        down_price=1,
        net=-2.6,
        mode="live",
        status="redeemed",
    )
    st.add_trade(
        ts=batch + 0.09,
        slug=f"hype-updown-5m-{hype_w}",
        kind="settle",
        shares=6.86,
        up_price=0,
        down_price=1,
        net=3.9771,
        mode="live",
        status="redeemed",
    )
    perf = performance_today(rt)
    slugs = [p["slug"] for p in perf["points"] if p["mark"] not in {"start", "now"}]
    assert slugs == [
        f"sol-updown-5m-{sol0}",
        f"xrp-updown-5m-{xrp_w}",
        f"hype-updown-5m-{hype_w}",
        f"sol-updown-5m-{sol1}",
    ]
    assert [p["mark"] for p in perf["points"] if p["mark"] not in {"start", "now"}] == ["scratch", "lose", "win", "lose"]
    assert abs(float(perf["end"]) - (3.9771 - 2.6 - 2.9 - 0.3372)) < 1e-6
    assert perf["hit_label"] == "1/3"
    assert perf["scratch_n"] == 1
    assert perf["points"][0]["mark"] == "start"
    assert perf["points"][1]["ts"] > perf["points"][0]["ts"]
    assert perf["points"][-1]["ts"] > perf["points"][2]["ts"]
    assert perf["points"][-1]["mark"] == "now"
    assert abs(float(perf["points"][-1]["y"]) - float(perf["end"])) < 1e-9


def test_format_signed_usd_keeps_minus_before_dollar():
    from app.config import format_signed_usd
    from app.telegram_ui import _signed

    assert format_signed_usd(-15.54) == "-$15.54"
    assert format_signed_usd(15.54) == "+$15.54"
    assert format_signed_usd(0) == "+$0.00"
    assert _signed(-14.20) == "-$14.20"


def test_performance_today_curve_survives_fok_noise_cap(tmp_path):
    """FOK/error rows must not clip later dumps/redeems off the hero curve."""
    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.wall import performance_today, utc_day_start

    st = Store(tmp_path / "curve-cap.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3, live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 156.53
    day = int(utc_day_start())
    for i in range(450):
        st.add_trade(
            ts=day + 60 + i,
            slug=f"btc-updown-5m-{day}",
            kind="taker",
            shares=5,
            up_price=0.5,
            down_price=0,
            net=0.0,
            mode="live",
            status="fok_killed" if i % 2 == 0 else "error",
        )
    st.add_trade(
        ts=day + 800,
        slug=f"sol-updown-5m-{day + 600}",
        kind="twap_live",
        shares=5,
        up_price=0,
        down_price=0.47,
        net=-0.3372,
        mode="live",
        status="dumped",
    )
    st.add_trade(
        ts=day + 900,
        slug=f"doge-updown-5m-{day + 600}",
        kind="settle",
        shares=6.86,
        up_price=0,
        down_price=1,
        net=3.4589,
        mode="live",
        status="redeemed",
    )
    st.add_trade(
        ts=day + 4000,
        slug=f"eth-updown-5m-{day + 3600}",
        kind="settle",
        shares=5.49,
        up_price=1,
        down_price=0,
        net=-2.8,
        mode="live",
        status="redeemed",
    )
    perf = performance_today(rt)
    board = operator_board(rt)
    marks = [p["mark"] for p in perf["points"] if p["mark"] not in {"start", "now"}]
    slugs = [p["slug"] for p in perf["points"] if p["mark"] not in {"start", "now"}]
    assert marks == ["scratch", "win", "lose"]
    assert slugs[-1].startswith("eth-updown-5m-")
    assert abs(float(perf["end"]) - (-0.3372 + 3.4589 - 2.8)) < 1e-6
    assert abs(float(rt.store.today_pnl(mode="live")) - float(perf["end"])) < 1e-6
    assert abs(float(board["today_pnl"]) - round(float(perf["end"]), 2)) < 1e-9
    assert abs(float(perf["points"][-1]["y"]) - float(perf["end"])) < 1e-9
    assert perf["hit_label"] == "1/2"
    assert perf["scratch_n"] == 1
    assert board["cash"] == 156.53
    assert board["cash_label"] == "可用 USDC"


def test_wall_slots_keep_open_window_not_future_listing(tmp_path):
    from app.config import Env
    from app.runtime import Runtime, operator_board
    from app.wall import format_tape_lines, note_wall_gate, operator_wall

    st = Store(tmp_path / "wall-live.sqlite")
    st.ensure_paper(500)
    st.patch_settings(max_usd_per_trade=3, live_trading=True)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    rt.live_usdc = 19.76
    rt.ws_status = "connected"
    rt.chainlink_status = "connected"
    note_wall_gate(
        rt,
        {
            "slug": "eth-updown-5m-1788174000",
            "reason": "twap_band",
            "lead_bps": -9.2,
            "ask": 0.80,
            "left": 240,
            "side": "down",
        },
    )
    note_wall_gate(
        rt,
        {
            "slug": "eth-updown-5m-1788174600",
            "reason": "future_listing",
            "ask": 0.495,
            "left": 840,
        },
    )
    assert all(row["reason"] != "future_listing" for row in rt.wall_tape)
    rt.wall_tape.append(
        {
            "ts": 1,
            "slug": "btc-updown-5m-1788174600",
            "asset": "btc",
            "reason": "future_listing",
            "reason_zh": "未開窗",
            "ask": 0.495,
            "lead_bps": None,
            "left": 840,
            "side": None,
            "ok": False,
        }
    )
    note_wall_gate(
        rt,
        {
            "slug": "btc-updown-5m-1788174000",
            "reason": "twap_lead",
            "lead_bps": 1.2,
            "ask": 0.48,
            "left": 200,
            "side": "up",
        },
    )
    wall = operator_wall(rt, operator_board(rt))
    eth = next(s for s in wall["slots"] if s["asset"] == "eth")
    btc = next(s for s in wall["slots"] if s["asset"] == "btc")
    assert eth["reason"] == "twap_band"
    assert eth["reason_zh"] == "價帶外"
    assert abs(float(eth["ask"]) - 0.80) < 1e-9
    assert btc["reason"] == "twap_lead"
    assert btc["reason_zh"] == "lead 唔夠"
    assert all(row["reason"] != "future_listing" for row in wall["tape"])
    log = "\n".join(format_tape_lines(rt, 8))
    assert "未開窗" not in log
    assert "價帶外" in log
    assert "lead 唔夠" in log


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

    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert DEFAULT_SETTINGS["strategy_mode"] == "twap"
    assert strategy_mode_of(None) == "twap"
    assert strategy_mode_of({}) == "twap"
    assert strategy_mode_of({"strategy_mode": "favorite"}) == "twap"
    assert strategy_mode_of({"strategy_mode": "complement"}) == "twap"
    assert strategy_mode_of({"strategy_mode": "nope"}) == "twap"


def test_telegram_settings_lock_twap_and_drop_legacy(tmp_path):
    from app.config import Env, SETTING_STEPS
    from app.runtime import Runtime
    from app.telegram_ui import TOGGLES, _rev_blurb, assets_help, assets_kb, home_text, settings_kb, settings_text

    st = Store(tmp_path / "tgset.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    labels = " ".join(btn.text for row in settings_kb(rt).inline_keyboard for btn in row)
    assert "鎖定" in labels
    assert "幣種過濾（買盤鎖定 BTC+ETH）" in labels
    coins = " ".join(btn.text for row in assets_kb(rt).inline_keyboard for btn in row)
    assert "✅ BTC 會買" in coins
    assert "✅ ETH 會買" in coins
    assert "🔒 SOL 唔買" in coins
    assert "🔒 XRP 唔買" in coins
    help_txt = assets_help(rt.settings())
    assert "而家會買：BTC+ETH" in help_txt
    assert "剔晒 SOL/XRP/BNB 都唔會入場" in help_txt
    assert "逆向思維" in labels
    assert "週期：5分鐘（鎖定）" in labels
    assert "週期 5M／15M／1H" not in labels
    assert "大熱尾窗" not in labels
    assert "尾盤優先" not in labels
    assert "全日掛單" not in labels
    assert "大熱定價掛單" not in labels
    assert "favorite_maker" not in TOGGLES
    assert "prefer_tail" not in TOGGLES
    assert "maker_first" not in TOGGLES
    assert "twap_reverse" in TOGGLES
    assert "逆向思維" in TOGGLES["twap_reverse"][0]
    assert "min_edge" not in SETTING_STEPS
    assert "favorite_min_price" not in SETTING_STEPS
    assert "twap_max_left" in SETTING_STEPS
    assert "twap_tp_bid" in SETTING_STEPS
    assert "twap_scratch_adverse" not in SETTING_STEPS
    essay = settings_text(rt)
    assert "唔做 YES+NO 互補" in essay
    assert "120–280s" in essay
    assert "只hunt BTC+ETH" in essay
    assert "第一下有效 6bps" in essay
    assert "90 秒剩餘" in essay
    assert "實盤 FOK：250ms 確認之後即刻 FAK" in essay
    assert "可以加 1¢" in essay
    assert "新鮮盤 dump" in essay
    assert "22¢ floor 唔減" in essay
    assert "subscribe/unsubscribe 唔斷線" in essay
    assert "BTC 同 ETH 可以同一 5 分鐘 unix 各做一注" in essay
    assert "scratch 之後唔反手" in essay
    assert "逆向思維" in essay
    assert "止賺 bid" in essay
    assert "唔設價止蝕" in essay
    assert "只做 5 分鐘" in essay
    assert "15 分鐘同 5 分鐘搶槽，已砍" in essay
    assert "主頁／而家狀況／Dashboard" in essay
    assert "今日已實現 PnL 曲線（唔包入金）同命中率" in essay
    assert "唔係錢包" in _rev_blurb(rt.settings())
    assert "halt 5→10→20→30" in _rev_blurb(rt.settings())
    assert "可以加 1¢" in _rev_blurb(rt.settings())
    home = home_text(rt)
    assert "唔做 YES+NO 互補" not in home
    assert "Rev 45" not in home
    assert "止賺 87¢" in home
    st.patch_settings(twap_reverse=True)
    assert "逆向思維開緊" in home_text(rt)
    st.patch_settings(twap_tp_bid=0.0)
    assert "止賺" not in home_text(rt)


def test_cheap_bounce_is_not_a_second_engine():
    """Wounded 20-30¢ dogs bounce often; every exit still −EV. Do not ship."""
    import json
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "cheap_bounce.py"
    spec = importlib.util.spec_from_file_location("cheap_bounce_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert abs(mod.pnl_hold(0.25, True) - (3 / 0.25 * 0.75 - 3 / 0.25 * 0.07 * 0.25 * 0.75)) < 1e-4
    assert mod.pnl_hold(0.25, False) < 0
    assert mod.pnl_scratch(0.25, 0.45) > 0
    ev = {"slug": "btc-updown-5m-1", "asset": "btc", "start": 1000, "end": 1300, "winner": "Up"}
    prints = [
        {"ts": 1010, "px": 0.70, "outcome": "Up", "size": 10},
        {"ts": 1012, "px": 0.28, "outcome": "Down", "size": 8},
        {"ts": 1100, "px": 0.46, "outcome": "Down", "size": 8},
        {"ts": 1280, "px": 0.10, "outcome": "Down", "size": 8},
    ]
    row = mod.find_entry(ev, prints, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=0.0)
    assert row is not None and row["side"] == "Down" and row["bounce_45"] is True
    hold = mod.simulate(row, mode="hold")
    tp = mod.simulate(row, mode="tp45")
    assert hold["exit_why"] == "settle" and hold["won"] is False
    assert tp["scratched"] is True and tp["pnl"] > hold["pnl"]

    data = json.loads(path.with_name("cheap_bounce.json").read_text())
    assert data["ship"] is False
    assert data["do_not_default_on"] is True
    core = data["core_btc_eth_twap60"]
    assert core["hold"]["all"]["n"] >= 1000
    assert core["hold"]["path"]["bounce_45"] >= 0.5
    assert core["hold"]["path"]["settle_wr_if_hold"] < 0.40
    assert core["hold"]["all"]["pnl_usd"] < 0
    assert core["tp45"]["robust"] is False
    assert core["tp45"]["all"]["pnl_usd"] <= core["hold"]["all"]["pnl_usd"]
    assert core["hold"]["path"]["mid_band_frac"] > 0.5
    assert data["findings"]["top_strategy"].startswith("No")


def test_high_wr_research_is_not_a_live_patch():
    """First-cross / book-confirm can lift held WR; do not ship while 暫時不動."""
    import json
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "research" / "high_wr.py"
    spec = importlib.util.spec_from_file_location("high_wr_research", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    full = [
        {"ts": 1100, "px": 0.50, "outcome": "Up"},
        {"ts": 1180, "px": 0.51, "outcome": "Up"},
        {"ts": 1220, "px": 0.52, "outcome": "Up"},
        {"ts": 1280, "px": 0.53, "outcome": "Up"},
    ]
    mid = mod.path_features(full, "Up", 1100, 1300)
    assert mid["ever_62"] is False and mid["still_mid90"] is True and mid["still_in_band90"] is True
    assert mid["ever_62_by90"] is False
    conf = [
        {"ts": 1100, "px": 0.50, "outcome": "Up"},
        {"ts": 1180, "px": 0.70, "outcome": "Up"},
        {"ts": 1220, "px": 0.88, "outcome": "Up"},
    ]
    hit = mod.path_features(conf, "Up", 1100, 1300)
    assert hit["ever_62"] is True and hit["ever_85"] is True and hit["still_mid90"] is False

    hold = {
        "px": 0.50,
        "won": False,
        "scratched": False,
        "exit_why": "settle",
        "pnl": -5.0,
        "ever_62": False,
        "still_mid90": True,
        "last_after": 0.49,
        "px90": 0.48,
        "left": 130,
        "end": 1300,
        "ts": 1170,
        "side": "Up",
    }
    dumped = mod.overlay(hold, mode="dump_mid90", haircut=0.02)
    assert dumped["scratched"] is True and dumped["exit_why"] == "late_still_mid"
    assert dumped["pnl"] > hold["pnl"]
    kept = dict(hold)
    kept["ever_62"] = True
    kept["still_mid90"] = False
    kept["won"] = True
    kept["pnl"] = 4.8
    assert mod.overlay(kept, mode="dump_never_62")["scratched"] is False

    data = json.loads(path.with_name("high_wr.json").read_text())
    assert data["ship"] is False
    assert data["do_not_default_on"] is True
    assert "price_sl_8c" in data["findings"]["do_not"]
    assert "cheap_bounce_20_30" in data["findings"]["do_not"]
    live = data["live"]["holds"]
    assert live["n"] >= 20
    assert live["wr"] is not None and live["wr"] < 0.40
    be = data["btc_eth"]
    assert be["first_bm"]["n"] >= 100
    assert be["last_bm"]["take_win_rate"] >= 0.70
    names = {v["name"] for v in be["variants"]}
    assert "first_bm" in names and "last_bm" in names
    assert "last_dump_mid90_h2" in names
    assert "first_dump_by90_h2" in names
    assert data["recommendation"]["do_now"] == "nothing on live"
    assert data["findings"]["top_strategy"].startswith("No live patch")
    assert (data["btc_eth"]["last_bm"].get("hold_never_62") or {}).get("take_win_rate", 1) < 0.40
    assert (data["btc_eth"]["last_bm"].get("hold_confirmed") or {}).get("take_win_rate", 0) >= 0.80


def test_rev54_ship_json_is_plus_ev_btc_eth():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params, hunt_assets

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev54_ship.json").read_text())
    assert data["ship"] is True
    assert data["pick"] == "first_dump_by90_h2"
    assert data["hunt_assets"] == ["btc", "eth"]
    assert data["btc_eth"]["train"]["pnl_usd"] > 0
    assert data["btc_eth"]["holdout"]["pnl_usd"] > 0
    assert data["live_cf_dump_by90_h2"]["dumped_winners"] == 0
    assert "price_sl_8c" in data["do_not"]
    assert "dump_mid90" in data["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert hunt_assets(DEFAULT_SETTINGS) == ("btc", "eth")
    assert p.no_cheaper is True
    assert abs(p.confirm_px - 0.62) < 1e-9
    assert abs(p.confirm_fair - 0.60) < 1e-9
    assert DEFAULT_SETTINGS["twap_assets"] == ["btc", "eth"]
    assert DEFAULT_SETTINGS["strategy_rev"] == 60


def test_freq_params_does_not_relax_six_bps_or_band():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "freq_params.json").read_text())
    ship = json.loads((Path(__file__).resolve().parents[1] / "research" / "freq_params_ship.json").read_text())
    assert data["ship"] is False
    assert data["pick"] is None
    assert ship["ship"] is False
    assert ship["pick"] is None
    assert data["grid"]["max_left_300"]["delta_n"] == 0
    assert data["grid"]["lead_5_5"]["delta_n"] >= 50
    assert data["grid"]["lead_5"]["delta_n"] >= data["grid"]["lead_5_5"]["delta_n"]
    assert data["grid"]["lead_4"]["forbidden"] is True
    assert "lead_5_5bps" in data["do_not"]
    assert "chase_leftover" in data["do_not"]
    assert "min_left_below_120" in data["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert abs(p.min_price - 0.45) < 1e-9
    assert abs(p.max_price - 0.55) < 1e-9
    assert abs(p.min_left - 120.0) < 1e-9
    assert abs(p.max_left - 280.0) < 1e-9
    assert DEFAULT_SETTINGS["strategy_rev"] == 60


def test_rev59_ship_json_oracle_fair_beats_dump90():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev59_ship.json").read_text())
    assert data["ship"] is True
    assert data["pick"] == "oracle_fair_late_60"
    assert data["strategy_rev"] == 59
    assert abs(float(data["params"]["twap_confirm_fair"]) - 0.60) < 1e-9
    assert data["vs_shipped_dump90"]["holdout_pnl5"]["delta"] >= 5.0
    assert data["vs_shipped_dump90"]["train_pnl5"]["delta"] > 0
    assert "twap_reverse_on" in data["do_not"]
    assert "dump_mid90" in data["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert abs(p.confirm_fair - 0.60) < 1e-9
    assert DEFAULT_SETTINGS["strategy_rev"] == 60


def test_rev55_ship_json_independent_clock_is_plus_ev():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev55_clock.json").read_text())
    assert data["ship"] is True
    assert data["strategy_rev"] == 55
    indep = data["independent"]
    clock = data["clock_lock_lead"]
    assert indep["n"] > clock["n"]
    assert indep["train"]["pnl_usd"] > 0
    assert indep["holdout"]["pnl_usd"] > 0
    assert (indep["holdout"]["take_win_rate"] or 0) >= 0.90
    assert (indep.get("take_win_rate") or 0) >= 0.90
    assert data["n_delta"] >= 50
    assert "restore_alts" in data["do_not"]
    assert "chase_cheaper_leftover" in data["do_not"]
    assert "flip_live_trading" in data["do_not"]
    assert DEFAULT_SETTINGS["strategy_rev"] == 60


def test_rev55_btc_eth_independent_clocks_keep_same_coin_lock(tmp_path):
    import time

    from app.config import Env
    from app.main import apply_strategy_rev
    from app.runtime import Runtime, twap_conflict_open

    st = Store(tmp_path / "rev55clock.sqlite")
    st.ensure_paper(500)
    st.patch_settings(strategy_rev=54, live_trading=True, max_usd_per_trade=3.0, twap_reverse=False)
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert s["live_trading"] is True
    assert s["twap_reverse"] is False
    assert float(s["max_usd_per_trade"]) == 3.0
    now = int(time.time())
    start = now - (now % 300)
    st.add_inventory("c-btc", f"btc-updown-5m-{start}", 5.0, 0.0, kind="twap_live", cost=3.0)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    assert twap_conflict_open(rt, f"btc-updown-5m-{start}") is True
    assert twap_conflict_open(rt, f"btc-updown-15m-{start}") is True
    assert twap_conflict_open(rt, f"eth-updown-5m-{start}") is False
    st.take_inventory("c-btc", 5.0, 0.0)
    st.add_trade(
        slug=f"btc-updown-5m-{start}",
        kind="taker",
        shares=5,
        up_price=0.0,
        down_price=0.55,
        net=0.27,
        mode="live",
        status="dumped",
        payload={},
    )
    assert twap_conflict_open(rt, f"btc-updown-5m-{start}") is True
    assert twap_conflict_open(rt, f"eth-updown-5m-{start}") is False
    rt2 = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    assert twap_conflict_open(rt2, f"btc-updown-5m-{start}") is True
    assert twap_conflict_open(rt2, f"eth-updown-5m-{start}") is False
    assert apply_strategy_rev(st) == 0


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
    from app.twap import fair_p_up, lead_bps, lead_z, phi, settlement_tau

    assert abs(phi(0.0) - 0.5) < 1e-9
    assert lead_bps(100080.0, 100000.0) == 8.0
    assert fair_p_up(0.0, 2.0, 60.0) == 0.5
    assert fair_p_up(80.0, 1.0, 9.0) > 0.99
    assert fair_p_up(-80.0, 1.0, 9.0) < 0.01
    assert fair_p_up(8.0, None, 90.0) is None
    assert settlement_tau(160.0) == 130.0
    assert settlement_tau(40.0) == 40.0
    z = lead_z(6.62, 0.69, 160.0)
    assert z is not None and abs(z - 6.62 / (0.69 * (130.0 ** 0.5))) < 1e-9
    assert abs(fair_p_up(6.62, 0.69, 160.0) - phi(z)) < 1e-6


def test_twap_entry_reason_and_scratch():
    from app.twap import TwapParams, should_scratch, twap_entry_reason

    snap = _twap_snap()
    params = TwapParams()
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=180.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=None, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) == "twap_no_feed"
    assert twap_entry_reason(
        slug="eth-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=180.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="xrp-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=90.0, fee_rate=0.07, params=params
    ) == "twap_asset"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.97, bid=0.96, left=180.0, fee_rate=0.07, params=params
    ) == "twap_band"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=290.0, fee_rate=0.07, params=params
    ) == "twap_window"
    late = TwapParams(late_left=180.0, late_min_price=0.50)
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.45, bid=0.44, left=150.0, fee_rate=0.07, params=late
    ) == "twap_late_cheap"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.45, bid=0.44, left=150.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.51, bid=0.50, left=150.0, fee_rate=0.07, params=params
    ) is None
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.45, bid=0.44, left=200.0, fee_rate=0.07, params=params
    ) is None
    weak = _twap_snap(lead_bps=2.0)
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=weak, ask=0.50, bid=0.49, left=180.0, fee_rate=0.07, params=params
    ) == "twap_lead"
    alts = TwapParams(assets=("btc", "eth", "sol", "xrp"))
    assert twap_entry_reason(
        slug="xrp-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=150.0, fee_rate=0.07, params=alts
    ) is None
    assert twap_entry_reason(
        slug="xrp-updown-5m-1000",
        snap=snap,
        ask=0.50,
        bid=0.49,
        left=150.0,
        fee_rate=0.07,
        params=TwapParams(assets=("btc", "eth", "sol", "xrp"), alt_min_left=180.0),
    ) == "twap_window"
    assert twap_entry_reason(
        slug="sol-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=200.0, fee_rate=0.07, params=alts
    ) is None
    wild = _twap_snap(lead_bps=8664.0)
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=wild, ask=0.50, bid=0.49, left=200.0, fee_rate=0.07, params=params
    ) == "twap_lead_wild"

    go, why = should_scratch(fair_p=0.40, lead_bps_signed=8.0, bid=0.38, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_weak"
    go, why = should_scratch(fair_p=0.55, lead_bps_signed=-1.0, bid=0.30, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_flip"
    go, why = should_scratch(fair_p=0.60, lead_bps_signed=8.0, bid=0.30, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is False and why == "twap_hold"
    book = TwapParams(scratch_late_left=90.0)
    go, why = should_scratch(
        fair_p=0.70, lead_bps_signed=10.0, bid=0.35, shares=10, fee_rate=0.07, left=60.0, params=book, asset="sol"
    )
    assert go is True and why == "twap_scratch_book"
    go, why = should_scratch(
        fair_p=0.70, lead_bps_signed=10.0, bid=0.50, shares=10, fee_rate=0.07, left=60.0, params=book, asset="sol"
    )
    assert go is True and why == "twap_scratch_book"
    go, why = should_scratch(
        fair_p=0.70, lead_bps_signed=10.0, bid=0.50, shares=10, fee_rate=0.07, left=120.0, params=book, asset="sol"
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.70, lead_bps_signed=10.0, bid=0.35, shares=10, fee_rate=0.07, left=60.0, params=book, asset="btc"
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.70, lead_bps_signed=10.0, bid=0.50, shares=10, fee_rate=0.07, left=60.0, params=params, asset="sol"
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=1.0, lead_bps_signed=8664.0, bid=0.50, shares=10, fee_rate=0.07, left=120.0, params=params
    )
    assert go is True and why == "twap_scratch_wild"
    go, why = should_scratch(fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is False and why == "twap_hold"
    go, why = should_scratch(fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=5.0, params=params)
    assert go is False and why == "twap_scratch_late"
    go, why = should_scratch(fair_p=None, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_no_fair"
    go, why = should_scratch(fair_p=0.50, lead_bps_signed=8.0, bid=0.52, shares=10, fee_rate=0.07, left=40.0, params=params)
    assert go is True and why == "twap_scratch_better"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.44, shares=10, fee_rate=0.07, left=40.0, params=params, fill_px=0.54, adverse=0.08
    )
    assert go is True and why == "twap_scratch_stop"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=params, fill_px=0.54, adverse=0.08
    )
    assert go is False and why == "twap_hold"
    fade = TwapParams(reverse=True)
    go, why = should_scratch(fair_p=0.30, lead_bps_signed=-8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=fade)
    assert go is False and why == "twap_hold"
    go, why = should_scratch(fair_p=None, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=40.0, params=fade)
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.30, lead_bps_signed=8664.0, bid=0.50, shares=10, fee_rate=0.07, left=120.0, params=fade
    )
    assert go is True and why == "twap_scratch_wild"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.50, bid=0.49, left=180.0, fee_rate=0.07, params=fade
    ) is None
    from app.twap import entry_edge, twap_post_fok_net

    assert entry_edge(0.30, 0.45, 0.07) < 0
    assert twap_post_fok_net(reverse=True, shares=6.4, px=0.45, fair_p=0.30, fee_rate=0.07) > 0
    follow_net = twap_post_fok_net(reverse=False, shares=6.4, px=0.45, fair_p=0.70, fee_rate=0.07)
    assert follow_net > 0
    fade_bm = twap_post_fok_net(reverse=False, shares=6.4, px=0.45, fair_p=0.30, fee_rate=0.07)
    assert fade_bm < 0
    tp = TwapParams(take_profit=0.87)
    go, why = should_scratch(fair_p=0.95, lead_bps_signed=12.0, bid=0.87, shares=10, fee_rate=0.07, left=40.0, params=tp)
    assert go is True and why == "twap_scratch_tp"
    go, why = should_scratch(fair_p=0.95, lead_bps_signed=12.0, bid=0.84, shares=10, fee_rate=0.07, left=40.0, params=tp)
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.95, lead_bps_signed=12.0, bid=0.87, shares=10, fee_rate=0.07, left=40.0, params=TwapParams()
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.30, lead_bps_signed=-8.0, bid=0.87, shares=10, fee_rate=0.07, left=40.0, params=TwapParams(reverse=True, take_profit=0.87)
    )
    assert go is False and why == "twap_hold"
    from app.twap import default_params, take_profit_px
    from app.config import DEFAULT_SETTINGS

    live_p = default_params(DEFAULT_SETTINGS)
    assert abs(float(take_profit_px(live_p) or 0) - 0.87) < 1e-9
    assert take_profit_px(TwapParams(take_profit=0.0)) is None
    assert abs(live_p.confirm_px - 0.62) < 1e-9
    assert abs(live_p.confirm_left - 90.0) < 1e-9
    assert abs(live_p.confirm_fair - 0.60) < 1e-9
    assert live_p.no_cheaper is True
    assert abs(live_p.up_tick - 0.01) < 1e-9
    assert live_p.assets == ("btc", "eth")


def test_rev54_first_cross_and_unconfirmed_dump():
    from app.hunter import is_twap_setup
    from app.twap import TwapParams, cheaper_than_first, richer_than_up_tick, should_scratch, twap_entry_reason

    snap = _twap_snap()
    params = TwapParams(confirm_px=0.62, confirm_left=90.0, no_cheaper=True)
    assert cheaper_than_first(0.45, 0.53) is True
    assert cheaper_than_first(0.528, 0.53) is False
    assert cheaper_than_first(0.53, 0.53) is False
    assert cheaper_than_first(0.45, None) is False
    assert richer_than_up_tick(0.53, 0.52, 0.01) is False
    assert richer_than_up_tick(0.54, 0.52, 0.01) is True
    assert richer_than_up_tick(0.52, 0.52, 0.01) is False
    assert richer_than_up_tick(0.53, None, 0.01) is False
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.45, bid=0.44, left=180.0, fee_rate=0.07, params=params, first_px=0.53
    ) == "twap_no_cheaper"
    assert twap_entry_reason(
        slug="btc-updown-5m-1000", snap=snap, ask=0.53, bid=0.52, left=180.0, fee_rate=0.07, params=params, first_px=0.53
    ) is None
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=params, high_water=0.50
    )
    assert go is True and why == "twap_scratch_unconfirmed"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.10, shares=10, fee_rate=0.07, left=80.0, params=params, high_water=0.50
    )
    assert go is False and why == "twap_scratch_no_bid"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.22, shares=10, fee_rate=0.07, left=80.0, params=params, high_water=0.50
    )
    assert go is True and why == "twap_scratch_unconfirmed"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=5.0, params=params, high_water=0.50
    )
    assert go is False and why == "twap_scratch_late"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=params, high_water=0.70
    )
    assert go is False and why == "twap_hold"
    oracle = TwapParams(confirm_px=0.62, confirm_left=90.0, confirm_fair=0.60)
    go, why = should_scratch(
        fair_p=0.55, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=oracle, high_water=0.70
    )
    assert go is True and why == "twap_scratch_oracle"
    go, why = should_scratch(
        fair_p=0.55, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=120.0, params=oracle, high_water=0.70
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.85, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=oracle, high_water=0.70
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.55, lead_bps_signed=-8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=TwapParams(reverse=True, confirm_fair=0.60, confirm_left=90.0), high_water=0.70
    )
    assert go is False and why == "twap_hold"
    go, why = should_scratch(
        fair_p=0.60, lead_bps_signed=8.0, bid=0.50, shares=10, fee_rate=0.07, left=120.0, params=params, high_water=None
    )
    assert go is False and why == "twap_hold"
    fade = TwapParams(reverse=True, confirm_px=0.62, confirm_left=90.0)
    go, why = should_scratch(
        fair_p=0.30, lead_bps_signed=-8.0, bid=0.50, shares=10, fee_rate=0.07, left=80.0, params=fade, high_water=None
    )
    assert go is False and why == "twap_hold"
    kw = dict(
        slug="btc-updown-5m-1000",
        title="btc 5m",
        condition_id="0xtwap",
        up_token="u",
        down_token="d",
        up_bids=_L((0.52, 20)),
        down_bids=_L((0.48, 20)),
        max_usd=5,
        min_shares=5,
        min_edge=0.02,
        fee_rate=0.07,
        prefer_tail=True,
        tail_confirm=0.9,
        maker_first=False,
        end=_late_end(180),
        strategy_mode="twap",
        twap_snap=snap,
        twap_params=params,
    )
    leftover = hunt(**kw, up_asks=_L((0.45, 40)), down_asks=_L((0.52, 40)), first_px=0.53)
    assert leftover is None
    first = hunt(**kw, up_asks=_L((0.53, 40)), down_asks=_L((0.52, 40)), first_px=0.53)
    assert first is not None and is_twap_setup(first)
    from app.twap import MUST_DUMP_WHY, scratch_book_max_age_ms, scratch_rescore_seconds

    assert scratch_book_max_age_ms(80, confirm_left=90) == 2000.0
    assert scratch_book_max_age_ms(120, confirm_left=90) == 60000.0
    assert scratch_rescore_seconds(80, confirm_left=90) == 3.0
    assert scratch_rescore_seconds(120, confirm_left=90) == 15.0
    assert "twap_scratch_unconfirmed" in MUST_DUMP_WHY
    assert "twap_scratch_oracle" in MUST_DUMP_WHY
    assert "twap_scratch_tp" not in MUST_DUMP_WHY


def test_confirm_twap_rejects_leftover_cheaper_fill(tmp_path):
    from app.config import Env
    from app.hunter import Setup
    from app.runtime import Runtime, _confirm_twap

    st = Store(tmp_path / "first-cross-confirm.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.53,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=1.0,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "fill_px": 0.53},
    )
    ev = {
        "slug": setup.slug,
        "title": "btc",
        "condition_id": "c",
        "up_token": "u",
        "down_token": "d",
        "end": _late_end(180),
        "min_size": 5,
        "fee_rate": 0.07,
    }
    leftover = _confirm_twap(
        rt,
        ev,
        setup,
        {"asks": _L((0.45, 40)), "bids": _L((0.44, 20))},
        {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))},
        st.settings(),
        0.07,
        st.paper_state(),
    )
    assert leftover.ok is False
    assert leftover.reason == "twap_no_cheaper"
    same = _confirm_twap(
        rt,
        ev,
        setup,
        {"asks": _L((0.53, 40)), "bids": _L((0.52, 20))},
        {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))},
        st.settings(),
        0.07,
        st.paper_state(),
    )
    assert same.ok is True
    assert abs(float(same.up_price) - 0.53) < 1e-9


def test_confirm_twap_one_tick_up_fills_two_tick_and_leftover_still_kill(tmp_path):
    from app.config import Env
    from app.hunter import Setup
    from app.runtime import Runtime, _confirm_twap

    st = Store(tmp_path / "rev60-confirm.sqlite")
    st.ensure_paper(500)
    rt = Runtime(st, Env())
    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.52,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=1.0,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "fill_px": 0.52},
    )
    ev = {
        "slug": setup.slug,
        "title": "btc",
        "condition_id": "c",
        "up_token": "u",
        "down_token": "d",
        "end": _late_end(180),
        "min_size": 5,
        "fee_rate": 0.07,
    }
    s = st.settings()
    paper = st.paper_state()
    one = _confirm_twap(
        rt,
        ev,
        setup,
        {"asks": _L((0.53, 40)), "bids": _L((0.52, 20))},
        {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))},
        s,
        0.07,
        paper,
    )
    assert one.ok is True
    assert one.reason == "fok_up_tick"
    assert abs(float(one.up_price) - 0.53) < 1e-9
    two = _confirm_twap(
        rt,
        ev,
        setup,
        {"asks": _L((0.54, 40)), "bids": _L((0.53, 20))},
        {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))},
        s,
        0.07,
        paper,
    )
    assert two.ok is False
    assert two.reason == "twap_no_up_requote"
    mixed = _confirm_twap(
        rt,
        ev,
        setup,
        {"asks": _L((0.45, 2.0), (0.53, 20.0)), "bids": _L((0.44, 20))},
        {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))},
        s,
        0.07,
        paper,
    )
    assert mixed.ok is False
    assert mixed.reason == "twap_no_cheaper"
    band = _confirm_twap(
        rt,
        ev,
        Setup(
            slug=setup.slug,
            title="btc",
            condition_id="c",
            up_token="u",
            down_token="d",
            kind="taker",
            up_price=0.55,
            down_price=0.0,
            shares=10,
            fillable=10,
            gross=0.2,
            fees=0.1,
            net=1.0,
            tail=False,
            extra={"strategy": "twap", "leg": "up", "fill_px": 0.55},
        ),
        {"asks": _L((0.56, 40)), "bids": _L((0.55, 20))},
        {"asks": _L((0.45, 20)), "bids": _L((0.44, 20))},
        s,
        0.07,
        paper,
    )
    assert band.ok is False
    assert band.reason == "twap_no_up_requote"


def test_twap_gate_row_reports_window_and_signal():
    from app.runtime import _twap_gate_row
    from app.twap import TwapParams

    snap = _twap_snap()
    ev = {"slug": "btc-updown-5m-1000", "end": _late_end(180)}
    up = {"asks": _L((0.50, 20)), "bids": _L((0.49, 20))}
    dn = {"asks": _L((0.52, 20)), "bids": _L((0.48, 20))}
    gate = _twap_gate_row(ev, snap, up, dn, 0.07, TwapParams(), None)
    assert gate["reason"] == "ready"
    assert gate["ask"] == 0.50
    assert gate["lead_bps"] == 8.0
    early = dict(ev, end=_late_end(290))
    gate2 = _twap_gate_row(early, snap, up, dn, 0.07, TwapParams(), None)
    assert gate2["reason"] == "twap_window"
    late = dict(ev, end=_late_end(90))
    gate_late = _twap_gate_row(late, snap, up, dn, 0.07, TwapParams(), None)
    assert gate_late["reason"] == "twap_window"

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
        end=_late_end(180),
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


def test_twap_reverse_lifts_the_other_mid_band_leg():
    from app.hunter import is_twap_setup
    from app.twap import TwapParams, opposite_leg, trade_leg

    snap = _twap_snap(lead_bps=8.0)
    fade = TwapParams(reverse=True, assets=("btc", "eth"))
    assert trade_leg(snap, fade) == "down"
    assert opposite_leg("up") == "down"
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
        end=_late_end(180),
        strategy_mode="twap",
        twap_snap=snap,
        twap_params=fade,
    )
    setup = hunt(
        **kw,
        up_asks=_L((0.50, 40)),
        down_asks=_L((0.51, 40)),
    )
    assert setup is not None
    assert is_twap_setup(setup)
    assert setup.extra["leg"] == "down"
    assert setup.extra["reverse"] is True
    assert setup.extra["lead_side"] == "up"
    assert setup.up_price == 0.0
    assert 0.50 <= setup.down_price <= 0.52
    assert setup.net > 0
    follow = hunt(
        **{**kw, "twap_params": TwapParams(assets=("btc", "eth"))},
        up_asks=_L((0.50, 40)),
        down_asks=_L((0.51, 40)),
    )
    assert follow is not None
    assert follow.extra["leg"] == "up"
    assert follow.extra.get("reverse") is False


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
        end=_late_end(180),
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


def test_nudge_tp_bid_steps_off_and_band():
    from app.config import TP_BID_STEPS, format_tp_bid, nudge_tp_bid

    assert TP_BID_STEPS[0] == 0.0
    assert 0.87 in TP_BID_STEPS
    assert nudge_tp_bid(0.87, up=True) == 0.90
    assert nudge_tp_bid(0.87, up=False) == 0.85
    assert nudge_tp_bid(0.80, up=False) == 0.0
    assert nudge_tp_bid(0.0, up=False) == 0.0
    assert nudge_tp_bid(0.0, up=True) == 0.80
    assert nudge_tp_bid(0.95, up=True) == 0.95
    assert format_tp_bid(0.0) == "關"
    assert format_tp_bid(0.87) == "87¢"


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
        seconds_left=180,
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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


def test_rev56_live_skips_rtt_second_walk(tmp_path):
    import asyncio
    from datetime import datetime, timedelta, timezone

    from app.config import Env
    from app.hunter import Setup
    from app.runtime import Runtime, _fok_confirm

    st = Store(tmp_path / "rtt-live.sqlite")
    st.ensure_paper(500)
    st.patch_settings(
        fok_delay_ms=0,
        clob_rtt_ms=200,
        live_trading=True,
        strategy_mode="twap",
        min_shares=5,
    )
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    assert rt.mode() == "live"

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
    hit = asyncio.run(_fok_confirm(rt, ev, setup))
    assert hit.ok is True
    assert hit.up_price == 0.50
    assert rt.data.round == 1


def test_rev56_ship_json_keeps_delay_skips_live_rtt():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev56_fok.json").read_text())
    assert data["ship"] is True
    assert data["strategy_rev"] == 56
    assert (data["tape"]["take_win_rate"] or 0) >= 0.90
    assert data["tape"]["holdout"]["pnl_usd"] > 0
    assert (data["persistence"]["cheap_within_1s"] or 0) > 0.15
    assert "chase_cheaper_leftover" in data["do_not"]
    assert "turn_taker_fok_off" in data["do_not"]
    assert "flip_live_trading" in data["do_not"]
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert float(DEFAULT_SETTINGS["fok_delay_ms"]) == 250.0
    assert float(DEFAULT_SETTINGS["clob_rtt_ms"]) == 150.0


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
    assert s["strategy_rev"] == 60
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth"]
    assert float(s["twap_max_left"]) == 280.0
    assert float(s["twap_min_left"]) == 120.0
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["twap_min_price"]) == 0.45
    assert float(s["twap_max_price"]) == 0.55
    assert "eth" in s["twap_assets"]
    assert "sol" not in s["twap_assets"]
    assert s["twap_assets"] == ["btc", "eth"]
    assert s["twap_horizons"] == ["5m"]
    assert s["live_trading"] is False
    assert "sol" not in DEFAULT_SETTINGS["twap_assets"]
    assert DEFAULT_SETTINGS["twap_assets"] == ["btc", "eth"]
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
        end=_late_end(180),
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
    assert live.assets == ("btc", "eth")
    assert slug_allowed("btc-updown-5m-1000", live) is True
    assert slug_allowed("sol-updown-5m-1000", live) is False
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
        left=180.0,
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
    import time

    from app.config import Env
    from app.runtime import Runtime, twap_conflict_open

    st = Store(tmp_path / "conflict.sqlite")
    st.ensure_paper(500)
    now = int(time.time())
    start5 = now - (now % 300)
    st.add_inventory("c-btc5", f"btc-updown-5m-{start5}", 5.0, 0.0, kind="twap", cost=5.0)
    rt = Runtime(st, Env())
    assert twap_conflict_open(rt, f"btc-updown-5m-{start5}") is True
    assert twap_conflict_open(rt, f"btc-updown-15m-{start5}") is True
    assert twap_conflict_open(rt, f"eth-updown-5m-{start5}") is False
    assert twap_conflict_open(rt, f"sol-updown-5m-{start5}") is False
    assert twap_conflict_open(rt, f"xrp-updown-5m-{start5}") is False
    assert twap_conflict_open(rt, f"eth-updown-5m-{start5 + 300}") is False
    assert twap_conflict_open(rt, f"sol-updown-15m-{start5}") is False
    st.add_inventory("c-xrp-old", "xrp-updown-5m-1000", 5.0, 0.0, kind="twap", cost=2.6)
    # Ended leftover must not brick the next 5m clock (BTC is still open on start5).
    assert twap_conflict_open(rt, f"xrp-updown-5m-{start5 + 300}") is False


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


def test_should_recycle_rtds_only_after_ticks_then_silence():
    import time

    from app.chainlink import ChainlinkTape, should_recycle_rtds

    assert should_recycle_rtds(9e9) is False
    assert should_recycle_rtds(0) is False
    assert should_recycle_rtds(19_999) is False
    assert should_recycle_rtds(20_001) is True
    assert should_recycle_rtds(3.4e6) is True
    tape = ChainlinkTape(symbols=("eth/usd",))
    assert should_recycle_rtds(tape.age_ms("eth/usd")) is False
    tape.last_recv["eth/usd"] = 0.0
    assert should_recycle_rtds(tape.age_ms("eth/usd")) is False
    tape.last_recv["eth/usd"] = time.time() - 30
    assert should_recycle_rtds(tape.age_ms("eth/usd")) is True


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
    assert s["strategy_rev"] == 60
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


def test_ws_sub_plan_keep_resub_idle():
    import json

    from app.runtime import ws_sub_frames, ws_sub_plan

    cur = ["btc-up", "btc-dn", "eth-up", "eth-dn"]
    nxt = cur + ["btc2-up", "btc2-dn", "eth2-up", "eth2-dn"]
    assert ws_sub_plan(cur, cur)["action"] == "keep"
    assert ws_sub_plan(list(reversed(cur)), cur)["action"] == "keep"
    pre = ws_sub_plan(cur, nxt)
    assert pre["action"] == "resub"
    assert pre["add"] == ["btc2-up", "btc2-dn", "eth2-up", "eth2-dn"]
    assert pre["drop"] == []
    roll = ws_sub_plan(nxt, cur)
    assert roll["action"] == "resub"
    assert roll["add"] == []
    assert roll["drop"] == ["btc2-up", "btc2-dn", "eth2-up", "eth2-dn"]
    assert ws_sub_plan(cur, [])["action"] == "idle"
    frames = ws_sub_frames(ws_sub_plan(nxt, ["x"] + cur))
    assert json.loads(frames[0])["operation"] == "unsubscribe"
    assert json.loads(frames[1])["operation"] == "subscribe"
    assert json.loads(frames[1])["initial_dump"] is False
    assert json.loads(frames[0])["assets_ids"] == ["btc2-up", "btc2-dn", "eth2-up", "eth2-dn"]
    assert json.loads(frames[1])["assets_ids"] == ["x"]
    assert ws_sub_frames({"action": "keep", "add": [], "drop": []}) == []


def test_rev57_ship_json_ws_stay_alive_not_sleeve():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev57_ws.json").read_text())
    assert data["ship"] is True
    assert data["strategy_rev"] == 57
    assert data["ws"]["initial_dump"] is False
    assert "chase_cheaper_leftover" in data["do_not"]
    assert "bps_4" in data["do_not"]
    assert "restore_alts" in data["do_not"]
    assert "flip_live_trading" in data["do_not"]
    assert DEFAULT_SETTINGS["strategy_rev"] == 60


def test_rev57_apply_keeps_live_trading_and_stake(tmp_path):
    from app.main import apply_strategy_rev

    st = Store(tmp_path / "rev57.sqlite")
    st.ensure_paper(500)
    st.patch_settings(strategy_rev=56, live_trading=True, max_usd_per_trade=3.0, twap_reverse=False)
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert s["twap_reverse"] is False
    assert s["twap_no_cheaper"] is True
    assert apply_strategy_rev(st) == 0


def test_rev58_halt_backoff_keeps_live(tmp_path):
    from app.config import DEFAULT_SETTINGS, Env
    from app.main import apply_strategy_rev
    from app.runtime import Runtime, clob_halt_seconds

    st = Store(tmp_path / "rev58.sqlite")
    st.ensure_paper(500)
    st.patch_settings(strategy_rev=57, live_trading=True, max_usd_per_trade=3.0, twap_reverse=False)
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    rt = Runtime(st, Env())
    assert clob_halt_seconds("trading is disabled") == 300.0
    assert rt.trip_clob_halt("trading is disabled", seconds=300) is True
    rt._clob_halt_until = 0
    assert rt.trip_clob_halt("trading is disabled", seconds=300) is False
    assert abs(float(rt._clob_halt_backoff) - 600.0) < 1e-9
    rt._clob_halt_until = 0
    rt.trip_clob_halt("trading is disabled", seconds=300)
    assert abs(float(rt._clob_halt_backoff) - 1200.0) < 1e-9
    rt._clob_halt_until = 0
    rt.trip_clob_halt("trading is disabled", seconds=300)
    assert abs(float(rt._clob_halt_backoff) - 1800.0) < 1e-9
    assert apply_strategy_rev(st) == 0


def test_rev59_oracle_fair_dump_keeps_live_and_direction(tmp_path):
    from app.config import DEFAULT_SETTINGS
    from app.main import apply_strategy_rev
    from app.twap import default_params, should_scratch

    st = Store(tmp_path / "rev59.sqlite")
    st.ensure_paper(500)
    st.patch_settings(strategy_rev=58, live_trading=True, max_usd_per_trade=3.0, twap_reverse=False)
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert abs(float(s["twap_confirm_fair"]) - 0.60) < 1e-9
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert s["twap_reverse"] is False
    assert s["twap_assets"] == ["btc", "eth"]
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    p = default_params(s)
    assert abs(p.confirm_fair - 0.60) < 1e-9
    go, why = should_scratch(
        fair_p=0.55,
        lead_bps_signed=7.0,
        bid=0.50,
        shares=10,
        fee_rate=0.07,
        left=80.0,
        params=p,
        high_water=0.70,
    )
    assert go is True and why == "twap_scratch_oracle"
    assert apply_strategy_rev(st) == 0


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
    assert s["strategy_rev"] == 60
    assert s["tags"] == ["5M"]
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert float(s["max_usd_per_trade"]) == 5.0
    assert s["live_trading"] is False
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert apply_strategy_rev(st) == 0


def test_rev54_pins_btc_eth_hunt_keeps_telegram_coins_and_live(tmp_path):
    from app.main import apply_strategy_rev
    from app.twap import default_params, hunt_assets

    st = Store(tmp_path / "rev54.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(9)
    st.patch_settings(
        strategy_rev=53,
        strategy_mode="twap",
        tags=["5M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        twap_assets=["btc", "eth", "sol", "xrp", "bnb", "hype", "doge", "zec"],
        live_trading=True,
        max_usd_per_trade=3.0,
        twap_reverse=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert s["twap_assets"] == ["btc", "eth"]
    assert float(s["twap_confirm_px"]) == 0.62
    assert float(s["twap_confirm_left"]) == 90.0
    assert abs(float(s.get("twap_confirm_fair") or 0) - 0.60) < 1e-9
    assert s["twap_no_cheaper"] is True
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert s["twap_reverse"] is False
    assert hunt_assets(s) == ("btc", "eth")
    p = default_params(s)
    assert p.assets == ("btc", "eth")
    assert p.no_cheaper is True
    after = st.paper_state()
    assert after["cash"] == before["cash"]
    assert apply_strategy_rev(st) == 0


def test_rev54_pins_btc_eth_hunt_keeps_telegram_coins_and_live(tmp_path):
    from app.main import apply_strategy_rev
    from app.twap import default_params, hunt_assets

    st = Store(tmp_path / "rev54.sqlite")
    st.ensure_paper(500)
    st.paper_apply_buy(9)
    st.patch_settings(
        strategy_rev=53,
        strategy_mode="twap",
        tags=["5M"],
        assets=["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"],
        twap_assets=["btc", "eth", "sol", "xrp", "bnb", "hype", "doge", "zec"],
        live_trading=True,
        max_usd_per_trade=3.0,
        twap_reverse=False,
    )
    before = st.paper_state()
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert s["assets"] == ["btc", "eth", "sol", "hype", "bnb", "xrp", "doge"]
    assert s["twap_assets"] == ["btc", "eth"]
    assert float(s["twap_confirm_px"]) == 0.62
    assert float(s["twap_confirm_left"]) == 90.0
    assert abs(float(s.get("twap_confirm_fair") or 0) - 0.60) < 1e-9
    assert s["twap_no_cheaper"] is True
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert s["twap_reverse"] is False
    assert hunt_assets(s) == ("btc", "eth")
    p = default_params(s)
    assert p.assets == ("btc", "eth")
    assert p.no_cheaper is True
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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
    assert s["strategy_rev"] == 60
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


def test_rev46_late_entry_gate_and_same_clock_all_coins(tmp_path):
    from app.config import DEFAULT_SETTINGS, Env
    from app.hunter import Setup
    from app.main import apply_strategy_rev
    from app.risk import approve
    from app.runtime import Runtime, twap_conflict_open
    from app.twap import default_params

    st = Store(tmp_path / "rev46.sqlite")
    st.ensure_paper(500)
    st.patch_settings(strategy_rev=45, twap_min_left=12.0, live_trading=True, max_usd_per_trade=3.0)
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert float(s["twap_min_left"]) == 120.0
    assert float(s["twap_late_left"]) == 0.0
    assert float(s["twap_late_min_price"]) == 0.45
    assert float(s["twap_scratch_dump_floor"]) == 0.22
    assert float(s["twap_alt_min_left"]) == 120.0
    assert float(s["twap_max_lead_bps"]) == 40.0
    assert float(s["twap_scratch_late_left"]) == 0.0
    assert s.get("twap_reverse") is False
    assert float(s.get("twap_tp_bid") or 0) == 0.87
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert float(DEFAULT_SETTINGS["twap_min_left"]) == 120.0
    assert default_params(s).min_left == 120.0
    assert default_params(s).alt_min_left == 120.0
    assert default_params(s).reverse is False
    setup = Setup(
        slug="sol-updown-5m-1",
        title="sol",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.0,
        down_price=0.50,
        shares=5,
        fillable=5,
        gross=0.5,
        fees=0,
        net=0.2,
        tail=False,
        extra={"strategy": "twap", "leg": "down"},
    )
    kw = dict(
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
        twap_min_left=120.0,
        twap_max_left=280.0,
        twap_min_price=0.45,
        twap_max_price=0.55,
        twap_alt_min_left=120.0,
        twap_late_left=0.0,
        twap_late_min_price=0.45,
    )
    late = approve(setup, seconds_left=90, **kw)
    assert late.ok is False
    assert late.reason == "twap_window"
    alt_mid = approve(setup, seconds_left=150, **kw)
    assert alt_mid.ok is True
    setup.down_price = 0.45
    cheap = approve(setup, seconds_left=150, **kw)
    assert cheap.ok is True
    btc = Setup(
        slug="btc-updown-5m-1",
        title="btc",
        condition_id="b",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.0,
        down_price=0.45,
        shares=5,
        fillable=5,
        gross=0.55,
        fees=0,
        net=0.2,
        tail=False,
        extra={"strategy": "twap", "leg": "down"},
    )
    assert approve(btc, seconds_left=150, **kw).ok is True
    setup.down_price = 0.50
    import time as _time

    clock = int(_time.time())
    st.add_inventory("c-sol", f"sol-updown-5m-{clock}", 5.0, 0.0, kind="twap_live", cost=2.5)
    rt = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    assert rt.mode() == "live"
    assert twap_conflict_open(rt, f"sol-updown-5m-{clock}") is True
    assert twap_conflict_open(rt, f"xrp-updown-5m-{clock}") is False
    assert twap_conflict_open(rt, f"xrp-updown-5m-{clock + 300}") is False
    st.take_inventory("c-sol", 5.0, 0.0)
    assert twap_conflict_open(rt, f"eth-updown-5m-{clock}") is False
    assert twap_conflict_open(rt, f"sol-updown-5m-{clock}") is True
    st.add_trade(
        slug=f"sol-updown-5m-{clock}",
        kind="taker",
        shares=5,
        up_price=0.33,
        down_price=0.0,
        net=-0.71,
        mode="live",
        status="dumped",
        payload={},
    )
    rt2 = Runtime(st, Env(force_paper=False, private_key="0xabc"))
    assert twap_conflict_open(rt2, f"bnb-updown-5m-{clock}") is False
    assert twap_conflict_open(rt2, f"sol-updown-5m-{clock}") is True
    assert apply_strategy_rev(st) == 0


def test_rev60_ship_json_one_tick_not_leftover():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    data = json.loads((Path(__file__).resolve().parents[1] / "research" / "rev60_ship.json").read_text())
    assert data["ship"] is True
    assert data["strategy_rev"] == 60
    assert data["pick"] == "up1_tick_plus_unmatched_reconfirm"
    assert abs(float(data["params"]["twap_up_tick"]) - 0.01) < 1e-9
    assert data["params"]["twap_no_cheaper"] is True
    assert "chase_cheaper_leftover" in data["do_not"]
    assert "skip_fok_delay" in data["do_not"]
    assert "up_requote_2ticks" in data["do_not"]
    assert "flip_live_trading" in data["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert abs(p.up_tick - 0.01) < 1e-9
    assert p.no_cheaper is True
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert abs(p.min_price - 0.45) < 1e-9
    assert abs(p.max_price - 0.55) < 1e-9
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert abs(float(DEFAULT_SETTINGS["twap_up_tick"]) - 0.01) < 1e-9
    assert abs(float(DEFAULT_SETTINGS["fok_delay_ms"]) - 250.0) < 1e-9


def test_learn_fail_ship_json_online_does_not_beat_frozen():
    import json
    import sys
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "research" / "learn_fail.json").read_text())
    ship = json.loads((root / "research" / "learn_fail_ship.json").read_text())
    assert data["ship"] is False
    assert data["pick"] is None
    assert ship["ship"] is False
    assert ship["pick"] is None
    assert data["winners"] == []
    assert data["iid"]["clustered"] is False
    assert data["iid"]["hold_loss_n"] == 3
    assert data["iid"]["p_loss_given_loss"] == 0.0
    assert (data["shipped"]["holdout"]["take_wr"] or 0) >= 0.99
    dump = data["grid"]["pause_asset_1w_after_dump"]
    assert dump["beats"] is False
    assert dump["d_ho"] < 5.0
    neg = data["grid"]["pause_asset_1w_after_neg_pnl"]
    assert neg["beats"] is False
    ewma = data["grid"]["ewma10_skip_sum_pnl_neg"]
    assert ewma["skipped"] >= 500
    hour = data["grid"]["train_skip_toxic_hour"]
    assert hour["d_ho"] < 0
    assert "autodial_from_live_n9" in data["do_not"]
    assert "online_bandit_min_lead" in ship["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert abs(p.min_price - 0.45) < 1e-9
    assert abs(p.max_price - 0.55) < 1e-9
    assert bool(DEFAULT_SETTINGS.get("twap_reverse")) is False

    sys.path.insert(0, str(root / "research"))
    import learn_fail as lf

    assert lf.cooldown_active(1000, None, 1) is False
    assert lf.cooldown_active(1000, 800, 1) is True
    assert lf.cooldown_active(1100, 800, 1) is False
    hist = [
        {"asset": "btc", "end": 100, "scratched": False, "won": False, "pnl": -1},
        {"asset": "btc", "end": 400, "scratched": False, "won": True, "pnl": 1},
    ]
    assert lf.last_fail_end(hist, asset="btc", kind="hold_loss") is None
    assert lf.last_fail_end(hist[:1], asset="btc", kind="hold_loss") == 100


def test_trend_side_ship_json_htf_does_not_beat_t0():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "research" / "trend_side.json").read_text())
    ship = json.loads((root / "research" / "trend_side_ship.json").read_text())
    assert data["ship"] is False
    assert data["pick"] is None
    assert ship["ship"] is False
    assert ship["pick"] is None
    assert data["winners"] == []
    assert data["n_joined"] == 599
    assert data["n_prev_lead"] == 599
    bounce = data["anatomy"]["bounce"]
    assert bounce["orig_wr"] < 0.60
    assert bounce["shipped"]["ev_ok"] is True
    assert bounce["shipped"]["take_wr"] >= 0.95
    crash26 = data["grid"]["skip_crash26"]
    assert crash26["beats"] is False
    assert crash26["d_ho"] < 5.0
    assert data["findings"]["best_nonbeat"] == "skip_crash26"
    fade = data["grid"]["fade_crash20"]
    assert fade["beats"] is False
    assert fade["d_tr"] < 0
    assert fade["holdout"]["take_wr"] < 0.85
    assert data["grid"]["fade_disagree_15m"]["holdout"]["pnl5"] < 0
    assert data["grid"]["skip_disagree_15m"]["d_ho"] < 0
    assert "htf_pick_side" in ship["do_not"]
    assert "fade_bounce_after_crash" in data["do_not"]
    p = default_params(DEFAULT_SETTINGS)
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert bool(DEFAULT_SETTINGS.get("twap_reverse")) is False


def test_dump_exec_ship_json_hot_books_not_lower_floor():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params

    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "research" / "dump_exec.json").read_text())
    ship = json.loads((root / "research" / "dump_exec_ship.json").read_text())
    assert data["ship"] is True
    assert data["pick"] == "hot_books_last90"
    assert ship["ship"] is True
    assert ship["pick"] == "hot_books_last90"
    assert ship["strategy_rev"] == 60
    assert data["floor"]["ship"] is False
    assert ship["floor"]["ship"] is False
    assert data["floor"]["pick"] == "must_dump_floor_01"
    assert data["delta"]["train"] < 0
    assert data["unconfirmed"]["blocked22_n"] == 96
    assert data["unconfirmed"]["blocked22_orig_wr"] < 0.15
    assert abs(float(data["params"]["twap_scratch_dump_floor"]) - 0.22) < 1e-9
    assert abs(float(data["params"]["twap_scratch_hot_ms"]) - 2000.0) < 1e-9
    assert abs(float(data["params"]["twap_rescore_hot_seconds"]) - 3.0) < 1e-9
    assert data["live_fingerprint"]["blocked_by_floor"] is False
    assert "lower_soft_dump_floor_22" in ship["do_not"]
    assert "dump_mid90" in ship["do_not"]
    assert "skip_scratch_left_min_8" in ship["do_not"]
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert abs(float(DEFAULT_SETTINGS["twap_scratch_hot_ms"]) - 2000.0) < 1e-9
    assert abs(float(DEFAULT_SETTINGS["twap_rescore_hot_seconds"]) - 3.0) < 1e-9
    p = default_params(DEFAULT_SETTINGS)
    assert abs(p.scratch_dump_floor - 0.22) < 1e-9
    assert abs(p.scratch_left_min - 8.0) < 1e-9
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert abs(p.min_price - 0.45) < 1e-9
    assert abs(p.max_price - 0.55) < 1e-9
    assert bool(DEFAULT_SETTINGS.get("twap_reverse")) is False


def test_rev60_apply_keeps_live_and_does_not_chase_leftover(tmp_path):
    from app.config import DEFAULT_SETTINGS
    from app.main import apply_strategy_rev
    from app.twap import default_params

    st = Store(tmp_path / "rev60.sqlite")
    st.ensure_paper(500)
    st.patch_settings(
        strategy_rev=59,
        live_trading=True,
        max_usd_per_trade=3.0,
        twap_reverse=False,
        twap_no_cheaper=True,
        twap_confirm_fair=0.60,
    )
    apply_strategy_rev(st)
    s = st.settings()
    assert s["strategy_rev"] == 60
    assert abs(float(s["twap_up_tick"]) - 0.01) < 1e-9
    assert s["live_trading"] is True
    assert float(s["max_usd_per_trade"]) == 3.0
    assert s["twap_reverse"] is False
    assert s["twap_no_cheaper"] is True
    assert abs(float(s["twap_confirm_fair"]) - 0.60) < 1e-9
    assert float(s["twap_min_lead_bps"]) == 6.0
    assert s["twap_assets"] == ["btc", "eth"]
    assert abs(float(s["fok_delay_ms"]) - 250.0) < 1e-9
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    p = default_params(s)
    assert abs(p.up_tick - 0.01) < 1e-9
    assert p.no_cheaper is True
    assert apply_strategy_rev(st) == 0


def test_clob_unmatched_and_retry_skips_leftover(tmp_path):
    import asyncio

    from app.broker import FillResult
    from app.config import Env
    from app.hunter import Setup
    from app.runtime import Runtime, _maybe_retry_unmatched_twap, clob_unmatched

    miss = FillResult(False, "error", "live", "no orders found to match with FAK order", {})
    assert clob_unmatched(miss) is True
    assert clob_unmatched(FillResult(False, "rejected", "live", "taker FAK not matched (live)", {})) is False
    assert clob_unmatched(FillResult(True, "filled", "live", "已提交", {})) is False

    st = Store(tmp_path / "rev60-unmatched.sqlite")
    st.ensure_paper(500)
    st.patch_settings(fok_delay_ms=0, clob_rtt_ms=0)
    rt = Runtime(st, Env())

    class CheapBook:
        async def book(self, token):
            if token != "u":
                return {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))}
            return {"asks": _L((0.45, 40)), "bids": _L((0.44, 20))}

    class SameBook:
        async def book(self, token):
            if token != "u":
                return {"asks": _L((0.55, 20)), "bids": _L((0.54, 20))}
            return {"asks": _L((0.52, 40)), "bids": _L((0.51, 20))}

    class CountingBroker:
        def __init__(self):
            self.n = 0
            self.px = []

        async def execute_pair(self, setup):
            self.n += 1
            self.px.append(float(setup.up_price))
            return FillResult(True, "filled", "live", "已提交", {"shares": setup.shares, "cost": 3.0})

    setup = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.52,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=1.0,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "fill_px": 0.52, "fair_p": 0.80},
    )
    ev = {
        "slug": setup.slug,
        "title": "btc",
        "condition_id": "c",
        "up_token": "u",
        "down_token": "d",
        "end": _late_end(180),
        "min_size": 5,
        "fee_rate": 0.07,
    }
    s = st.settings()
    cheap_broker = CountingBroker()
    rt.data = CheapBook()
    killed = asyncio.run(
        _maybe_retry_unmatched_twap(
            rt, ev, setup, miss, broker=cheap_broker, s=s, fee_rate=0.07, paper_mode=False
        )
    )
    assert killed.ok is False
    assert cheap_broker.n == 0
    assert (killed.payload or {}).get("unmatched_retry") == "twap_no_cheaper"

    setup2 = Setup(
        slug="btc-updown-5m-1000",
        title="btc",
        condition_id="c",
        up_token="u",
        down_token="d",
        kind="taker",
        up_price=0.52,
        down_price=0.0,
        shares=10,
        fillable=10,
        gross=0.2,
        fees=0.1,
        net=1.0,
        tail=False,
        extra={"strategy": "twap", "leg": "up", "fill_px": 0.52, "fair_p": 0.80},
    )
    fill_broker = CountingBroker()
    rt.data = SameBook()
    filled = asyncio.run(
        _maybe_retry_unmatched_twap(
            rt, ev, setup2, miss, broker=fill_broker, s=s, fee_rate=0.07, paper_mode=False
        )
    )
    assert filled.ok is True
    assert fill_broker.n == 1
    assert abs(fill_broker.px[0] - 0.52) < 1e-9
    assert (filled.payload or {}).get("unmatched_retry") in {"fok_filled", "fok_fak"}
    paper = asyncio.run(
        _maybe_retry_unmatched_twap(
            rt, ev, setup2, miss, broker=fill_broker, s=s, fee_rate=0.07, paper_mode=True
        )
    )
    assert paper.ok is False
    assert fill_broker.n == 1


def test_two_alts_research_does_not_pin():
    import json
    from pathlib import Path

    from app.config import DEFAULT_SETTINGS
    from app.twap import default_params, hunt_assets

    root = Path(__file__).resolve().parents[1]
    data = json.loads((root / "research" / "two_alts.json").read_text())
    ship = json.loads((root / "research" / "two_alts_ship.json").read_text())
    assert data["ship"] is False
    assert data["pick"] is None
    assert data["recommend"] is None
    assert data["recommend_plus_one"] is None
    assert ship["ship"] is False
    assert ship["pick"] is None
    assert ship["recommend"] is None
    assert ship["recommend_plus_one"] is None
    assert ship["passing_alts"] == []
    assert ship["strategy_rev"] == 60
    assert DEFAULT_SETTINGS["twap_assets"] == ["btc", "eth"]
    assert hunt_assets(DEFAULT_SETTINGS) == ("btc", "eth")
    assert DEFAULT_SETTINGS["strategy_rev"] == 60
    assert "sol" not in DEFAULT_SETTINGS["twap_assets"]
    assert "pin_twap_assets_without_owner" in ship["do_not"]
    assert "15m" in ship["do_not"]
    assert "hype_no_prints" in ship["do_not"]
    assert "skip_dump90_on_alts_to_chase_print_hold_wr" in ship["do_not"]
    assert ship["ws_plus_two_fits"] is False
    assert ship["ws_plus_one_fits"] is True
    assert ship["hype_prints"] == 0
    assert ship["closest_pair"]["d_holdout"] < 0
    assert ship["closest_plus_one"]["d_holdout"] < 0
    assert ship["alt_dump_share"]["sol"] >= 0.9
    assert ship["alt_dump_share"]["xrp"] >= 0.9
    assert ship["alt_confirm_62"]["sol"] < 0.2
    assert data["core"]["holdout"]["ev_ok"] is True
    assert data["per_asset"]["sol"]["holdout"]["pnl5"] < 0
    assert data["per_asset"]["xrp"]["holdout"]["pnl5"] < 0
    assert data["overlay_cf"]["sol"]["bm_only"]["holdout"]["pnl5"] > 0
    assert data["overlay_cf"]["sol"]["dump90_oracle"]["holdout"]["pnl5"] < 0
    p = default_params(DEFAULT_SETTINGS)
    assert abs(p.min_lead_bps - 6.0) < 1e-9
    assert abs(p.min_price - 0.45) < 1e-9
    assert abs(p.max_price - 0.55) < 1e-9
    assert abs(p.min_left - 120.0) < 1e-9
    assert abs(p.max_left - 280.0) < 1e-9
    assert abs(p.confirm_px - 0.62) < 1e-9
    assert abs(p.confirm_fair - 0.60) < 1e-9
    assert bool(DEFAULT_SETTINGS.get("twap_reverse")) is False
