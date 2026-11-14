from datetime import datetime, timedelta, timezone

import pytest

from polyarb.config import MetarConfig
from polyarb.data.gamma import parse_event
from polyarb.data.metar import (
    MetarFeed,
    filter_glitches,
    parse_metar_temp_c,
    round_half_up,
    to_whole_degrees,
)
from polyarb.models import FeeSchedule, Outcome
from polyarb.strategies.metar import MetarLock, parse_temp_bracket

from conftest import NOW, book, make_event, make_market

WEATHER_FEE = FeeSchedule(rate=0.05)


def test_metar_temp_parsing_and_polymarket_half_up_rounding():
    # Exact examples from Polymarket's published weather market specification.
    assert parse_metar_temp_c("METAR KLGA 121451Z 31012KT 10SM FEW050 14/13 A3012 RMK AO2 SLP198 T01390133") == pytest.approx(13.9)
    assert parse_metar_temp_c("METAR CYYZ 120600Z 27008KT 15SM SCT040 M02/M02 A2998 RMK AO2 T10171022") == pytest.approx(-1.7)
    assert parse_metar_temp_c("METAR EGLC 121420Z AUTO 02009KT 9999 NCD 18/12 Q1006") == pytest.approx(18.0)
    assert parse_metar_temp_c("METAR UUWW 121400Z 29002MPS 9999 OVC023 M11/M17 Q1006") == pytest.approx(-11.0)
    # SPECI reports never count per Polymarket rules.
    assert parse_metar_temp_c("SPECI KLGA 121510Z 31018KT 10SM BKN040 19/12 A3010 RMK AO2 T01900120") is None

    assert round_half_up(13.9) == 14
    assert round_half_up(17.4) == 17
    assert round_half_up(16.5) == 17
    assert round_half_up(-1.7) == -2
    assert round_half_up(-8.5) == -8

    assert to_whole_degrees(18.3, "F") == 65
    assert to_whole_degrees(17.8, "F") == 64
    assert to_whole_degrees(19.0, "F") == 66
    assert to_whole_degrees(-22.5, "F") == -8


def test_metar_glitch_filter_drops_isolated_sensor_spike():
    pts = [(1000, 10.0), (4600, 48.0), (8200, 11.0)]
    clean = filter_glitches(pts, max_jump_c=12.0)
    assert clean == [(1000, 10.0), (8200, 11.0)]


def test_gamma_parse_event_extracts_awc_metar_spec():
    desc = (
        "This market will resolve to the temperature range that contains the highest temperature "
        "reported at London City Airport (EGLC) on 12 Nov '26.\n"
        "https://aviationweather.gov/api/data/metar?ids=EGLC&format=raw&date=2026-11-13T03:00:00Z&hours=30"
    )
    raw = {
        "id": "ev-eglc",
        "title": "Highest temperature in London on November 12?",
        "slug": "highest-temp-london-nov-12",
        "negRisk": True,
        "markets": [
            {
                "id": "m1",
                "conditionId": "0x" + "11" * 32,
                "question": "Will the highest temperature in London be 7°C on November 12?",
                "clobTokenIds": ["y1", "n1"],
                "outcomes": ["Yes", "No"],
                "groupItemTitle": "7°C",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "description": desc,
            }
        ],
    }
    ev = parse_event(raw)
    assert (ev.metar_icao, ev.metar_obs_date, ev.metar_mode) == ("EGLC", "2026-11-12", "high")
    brk = parse_temp_bracket(ev.markets[0])
    assert brk is not None and (brk.lo, brk.hi, brk.unit) == (7.0, 7.0, "C")


def test_metar_lock_buys_eliminated_no_and_surviving_basket(store, now_ts):
    end = NOW + timedelta(hours=12)
    groups = ["6°C or below", "7°C", "8°C", "9°C or higher"]
    ms = [
        make_market(
            f"Will the highest temperature in London be {g}?",
            group=g,
            neg_risk=True,
            index=i,
            fee=WEATHER_FEE,
            end=end,
        )
        for i, g in enumerate(groups)
    ]
    ev = make_event(ms, neg_risk=True, title="Highest temperature in London on November 12?")
    ev.metar_icao = "EGLC"
    ev.metar_obs_date = "2026-11-12"
    ev.metar_mode = "high"

    feed = MetarFeed()
    # 2026-11-12 13:20 UTC in London -> 8°C observed! So "6°C or below" and "7°C" are 100% eliminated!
    ts_1320 = int(datetime(2026, 11, 12, 13, 20, tzinfo=timezone.utc).timestamp())
    changed = feed.ingest_rows([{"icaoId": "EGLC", "obsTime": ts_1320, "rawOb": "METAR EGLC 121320Z 02009KT 9999 NCD 08/01 Q1006"}])
    assert changed == {"EGLC"}

    # Dead "6°C or below" already repriced to NO=0.999 (YES bid=0.001, YES ask=0.002 — would break $1 min notional in normal yes_basket!).
    store.put(book(ms[0].yes_token, bids=[(0.001, 500)], asks=[(0.002, 500)]))
    # Newly killed "7°C" still has a stale YES bid at 0.12 => NO ask = 0.88!
    store.put(book(ms[1].yes_token, bids=[(0.12, 40)], asks=[(0.15, 40)]))
    # Surviving "8°C" and "9°C or higher" sum to 0.55 + 0.41 = 0.96 (< $1 even after fees!).
    store.put(book(ms[2].yes_token, bids=[(0.53, 50)], asks=[(0.55, 50)]))
    store.put(book(ms[3].yes_token, bids=[(0.39, 50)], asks=[(0.41, 50)]))

    strat = MetarLock(MetarConfig(), feed=feed, max_trade_usd=50.0)
    (target,) = strat.targets([ev])
    opps = strat.evaluate(target, store, now_ts)
    assert len(opps) == 2

    single = next(o for o in opps if len(o.legs) == 1)
    assert single.legs[0].outcome is Outcome.NO and single.legs[0].token_id == ms[1].no_token
    assert single.profit > 4.0 and single.meta["fast_flip"] is True

    basket = next(o for o in opps if len(o.legs) == 2)
    assert {leg.token_id for leg in basket.legs} == {ms[2].yes_token, ms[3].yes_token}
    assert basket.meta["surviving_legs"] == 2 and basket.profit > 0.5


def test_metar_lock_post_midnight_locks_exact_day_winner(store, now_ts):
    end = NOW + timedelta(hours=6)
    groups = ["7°C", "8°C", "9°C or higher"]
    ms = [make_market(f"Temp {g}?", group=g, neg_risk=True, index=i, end=end) for i, g in enumerate(groups)]
    ev = make_event(ms, neg_risk=True, title="Highest temperature in London on November 12?")
    ev.metar_icao, ev.metar_obs_date, ev.metar_mode = "EGLC", "2026-11-12", "high"

    feed = MetarFeed()
    t_day1 = int(datetime(2026, 11, 12, 14, 20, tzinfo=timezone.utc).timestamp())
    t_day2 = int(datetime(2026, 11, 13, 0, 20, tzinfo=timezone.utc).timestamp())
    feed.ingest_rows([
        {"icaoId": "EGLC", "obsTime": t_day1, "rawOb": "METAR EGLC 121420Z 02009KT 08/01 Q1006"},
        {"icaoId": "EGLC", "obsTime": t_day2, "rawOb": "METAR EGLC 130020Z 02009KT 05/01 Q1008"},
    ])
    assert feed.get("EGLC", "2026-11-12").day_finished is True

    store.put(book(ms[0].yes_token, bids=[(0.001, 100)], asks=[(0.002, 100)]))
    store.put(book(ms[1].yes_token, bids=[(0.94, 50)], asks=[(0.96, 50)]))  # exact winner 8°C priced at 96c!
    store.put(book(ms[2].yes_token, bids=[(0.03, 50)], asks=[(0.05, 50)]))  # 9°C+ lost -> NO ask 97c!

    strat = MetarLock(MetarConfig(), feed=feed, max_trade_usd=50.0)
    (target,) = strat.targets([ev])
    opps = strat.evaluate(target, store, now_ts)
    keys = {o.key.rsplit(":", 2)[-2] + ":" + o.legs[0].outcome.value for o in opps if len(o.legs) == 1}
    assert f"{ms[1].id}:YES" in keys
    assert f"{ms[2].id}:NO" in keys
