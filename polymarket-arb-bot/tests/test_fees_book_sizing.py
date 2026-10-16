import math

import pytest

from polyarb.book import Level, OrderBook, depth, spend, take
from polyarb.fees import fee_per_share, round_fee, taker_fee
from polyarb.models import FEE_FREE, FeeSchedule
from polyarb.sizing import LegBook, floor_shares, max_profitable_units, size_basket

CRYPTO = FeeSchedule(rate=0.07)
SPORTS = FeeSchedule(rate=0.05)


def test_fee_formula_matches_docs_table():
    # docs: 100 shares at 50c, crypto 0.07 -> $1.75; sports 0.05 -> $1.25
    assert taker_fee(CRYPTO, 100, 0.5) == pytest.approx(1.75)
    assert taker_fee(SPORTS, 100, 0.5) == pytest.approx(1.25)


def test_fee_symmetric_and_zero_at_extremes():
    assert fee_per_share(SPORTS, 0.3) == pytest.approx(fee_per_share(SPORTS, 0.7))
    assert fee_per_share(SPORTS, 0.0) == 0.0
    assert fee_per_share(SPORTS, 1.0) == 0.0
    assert fee_per_share(FEE_FREE, 0.5) == 0.0


def test_fee_exponent_and_rounding():
    sched = FeeSchedule(rate=0.1, exponent=2.0)
    assert fee_per_share(sched, 0.5) == pytest.approx(0.1 * 0.0625)
    assert round_fee(0.000004) == 0.0
    assert round_fee(0.123456) == 0.12346


def test_book_parses_unsorted_strings_and_mirrors():
    b = OrderBook.from_api(
        {
            "asset_id": "T",
            "bids": [{"price": "0.08", "size": "10"}, {"price": "0.09", "size": "5"}],
            "asks": [{"price": "0.99", "size": "7"}, {"price": "0.11", "size": "3"}],
            "timestamp": "1782753357257",
        }
    )
    assert b.best_bid == 0.09 and b.best_ask == 0.11
    assert [l.price for l in b.no_asks()] == [0.91, 0.92]
    assert [l.price for l in b.no_bids()] == [0.89, 0.01]
    assert b.timestamp == pytest.approx(1782753357.257)


def test_set_level_updates_and_deletes():
    b = OrderBook("T", bids=[(0.4, 10)], asks=[(0.6, 10)])
    b.set_level("BUY", "0.45", "3")
    b.set_level("SELL", 0.6, 0)
    assert b.best_bid == 0.45
    assert b.best_ask is None


def test_set_level_keeps_handed_out_ladders_unchanged():
    b = OrderBook.from_api({"asset_id": "T", "hash": "h1", "bids": [{"price": "0.40", "size": "10"}, {"price": "0.38", "size": "4"}],
                            "asks": [{"price": "0.45", "size": "7"}]})
    bids, no_asks = b.bids, b.no_asks()
    assert b.hash == "h1" and b.no_asks() is no_asks  # mirror is cached until the bids move
    b.set_level("BUY", "0.39", "2")
    b.set_level("BUY", "0.40", "0")
    assert bids == [(0.40, 10), (0.38, 4)] and [lvl.price for lvl in no_asks] == [0.60, 0.62]
    assert b.bids == [(0.39, 2), (0.38, 4)] and [lvl.price for lvl in b.no_asks()] == [0.61, 0.62]
    assert b.hash is None  # a locally edited book no longer matches any server hash
    b.set_level("SELL", "0.44", "1")
    b.set_level("SELL", "0.50", "0")  # deleting an absent level is a no-op
    assert b.asks == [(0.44, 1), (0.45, 7)] and b.fingerprint(1) == (((0.39, 2),), ((0.44, 1),))


def test_take_walks_levels_with_fees():
    levels = [Level(0.40, 10), Level(0.50, 10)]
    f = take(levels, 15, SPORTS)
    assert f.shares == 15
    assert f.notional == pytest.approx(10 * 0.40 + 5 * 0.50)
    assert f.fee == pytest.approx(10 * 0.05 * 0.24 + 5 * 0.05 * 0.25)
    assert f.worst_price == 0.50 and f.levels == 2
    assert take(levels, 50, SPORTS).shares == 20


def test_spend_respects_max_price():
    levels = [Level(0.40, 10), Level(0.50, 10)]
    f = spend(levels, 6.0, 0.45, FEE_FREE)
    assert f.notional == pytest.approx(4.0)  # only the 0.40 level is within the cap
    f2 = spend(levels, 6.0, 0.50, FEE_FREE)
    assert f2.notional == pytest.approx(6.0) and f2.shares == pytest.approx(14.0)


def test_depth():
    levels = [Level(0.40, 10), Level(0.50, 10), Level(0.60, 1)]
    assert depth(levels, 0.5) == 20


def test_max_profitable_units_stops_where_marginal_cost_crosses_payout():
    a = LegBook([Level(0.30, 10), Level(0.45, 100)], FEE_FREE)
    b = LegBook([Level(0.50, 20), Level(0.60, 100)], FEE_FREE)
    # 0.30+0.50=0.80 for 10 units, then 0.45+0.50=0.95 for 10 more, then 0.45+0.60=1.05 > 1
    assert max_profitable_units([a, b], 1.0) == pytest.approx(20)
    assert max_profitable_units([a, b], 1.0, min_unit_edge=0.1) == pytest.approx(10)
    assert max_profitable_units([a, b], 1.0, max_shares=7) == pytest.approx(7)
    assert max_profitable_units([a, b], 1.0, max_cost=4.0) == pytest.approx(5)


def test_size_basket_profit_and_minimums():
    legs = [LegBook([Level(0.30, 50)], FEE_FREE, 5), LegBook([Level(0.60, 50)], FEE_FREE, 5)]
    fill = size_basket(legs, 1.0)
    assert fill.shares == 50
    assert fill.profit == pytest.approx(50 * 0.10)
    tiny = [LegBook([Level(0.30, 3)], FEE_FREE, 5), LegBook([Level(0.60, 50)], FEE_FREE, 5)]
    assert size_basket(tiny, 1.0) is None


def test_size_basket_bumps_cheap_leg_to_one_dollar_notional():
    legs = [LegBook([Level(0.02, 500)], FEE_FREE, 5), LegBook([Level(0.90, 30)], FEE_FREE, 5)]
    fill = size_basket(legs, 1.0, max_shares=10)
    assert fill is None  # 10 shares of a 2c leg is $0.20 < $1 and cannot be bumped past max_shares
    # 30 shares (the 0.90 leg's depth) puts only $0.60 on the 2c leg; reaching $1 needs 50 shares.
    assert size_basket(legs, 1.0) is None
    legs2 = [LegBook([Level(0.02, 500)], FEE_FREE, 5), LegBook([Level(0.90, 100)], FEE_FREE, 5)]
    fill2 = size_basket(legs2, 1.0)
    assert fill2.shares == 100
    assert fill2.fills[0].notional >= 1.0
    legs3 = [LegBook([Level(0.02, 500)], FEE_FREE, 5), LegBook([Level(0.90, 40), Level(0.97, 100)], FEE_FREE, 5)]
    fill3 = size_basket(legs3, 1.0, min_unit_edge=0.02)
    assert fill3 is not None and fill3.shares == 50  # bumped from 40 into the 0.97 level, still profitable
    assert fill3.fills[0].notional == pytest.approx(1.0)


def test_floor_shares():
    assert floor_shares(12.3499) == 12.34
    assert floor_shares(0.009) == 0.0
    assert math.isclose(floor_shares(5.0), 5.0)
