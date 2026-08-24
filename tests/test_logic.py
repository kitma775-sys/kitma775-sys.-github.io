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

