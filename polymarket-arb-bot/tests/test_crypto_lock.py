from polyarb.crypto_lock import locked_side, order_if_cheap
from polyarb.crypto_lock_run import _Feed


def test_a_large_lead_with_little_time_left_locks_up():
    # 20 bps above the open, 15s left. A 40 bp price shock moves the 60s TWAP by 10 bp.
    assert locked_side(100.0, 100.20, 15) == "up"


def test_a_small_lead_does_not_survive_the_shock():
    assert locked_side(100.0, 100.02, 15) is None


def test_the_same_gap_locks_when_less_time_remains():
    assert locked_side(100.0, 100.05, 5) == "up"
    assert locked_side(100.0, 100.05, 40) is None


def test_down_is_the_mirror():
    assert locked_side(100.0, 99.80, 15) == "down"


def test_order_requires_agreement_a_cheap_ask_and_the_entry_window():
    common = dict(twap_open=100.0, twap_now=100.25, spot_open=100.0, spot_now=100.30, ask_up=0.80, ask_down=0.25)
    assert order_if_cheap(**common, seconds_left=15).side == "up"
    assert order_if_cheap(**{**common, "ask_up": 0.97}, seconds_left=15) is None
    assert order_if_cheap(**{**common, "spot_now": 99.5}, seconds_left=15) is None
    assert order_if_cheap(**common, seconds_left=40) is None


def test_snapshot_keeps_the_latest_print_for_the_payload_symbol():
    feed = _Feed()
    feed.apply({
        "topic": "crypto_prices_twap_sixty",
        "type": "subscribe",
        "payload": {"symbol": "btc/usd", "data": [{"value": 100.0, "timestamp": 1}, {"value": 101.5, "timestamp": 2}]},
    })
    feed.apply({
        "topic": "crypto_prices",
        "type": "update",
        "payload": {"symbol": "btcusdt", "value": 102.0},
    })
    assert feed.twap["btc/usd"] == 101.5
    assert feed.spot["btcusdt"] == 102.0
