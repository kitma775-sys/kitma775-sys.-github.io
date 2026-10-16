from datetime import date, datetime, timezone

from polyarb.strategies.ladder_parse import Direction, _resolve_year, detect_ladders, question_template

from conftest import make_event, make_market


def dt(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=timezone.utc)


def values(ladder) -> list[float]:
    return [r.value for r in ladder.rungs]


def test_up_ladder_orders_strongest_first():
    ev = make_event([make_market(f"Will Bitcoin reach ${p:,} by December 31?") for p in (110_000, 130_000, 120_000)])
    (ladder,) = detect_ladders(ev)
    assert ladder.direction is Direction.UP
    assert values(ladder) == [130_000, 120_000, 110_000]
    assert ladder.tokens == [r.market.yes_token for r in ladder.rungs]


def test_down_ladder_orders_lowest_first():
    ev = make_event([make_market(f"Will Bitcoin dip to ${p}k by October 31?") for p in (100, 80, 90)])
    (ladder,) = detect_ladders(ev)
    assert ladder.direction is Direction.DOWN
    assert values(ladder) == [80_000, 90_000, 100_000]


def test_by_date_ladder_orders_earliest_first():
    ends = [dt(2026, 12, 31), dt(2026, 6, 30), dt(2026, 9, 30)]
    ev = make_event([make_market(f"Will the ceasefire hold by {e:%B} {e.day}, 2026?", end=e) for e in ends])
    (ladder,) = detect_ladders(ev)
    assert ladder.direction is Direction.BY_DATE
    assert [r.market.end_date for r in ladder.rungs] == sorted(ends)


def test_label_arrows_and_plus_suffix_set_direction():
    up = make_event([make_market(f"Highest temperature {t}°F?", group=f"↑ {t}°F") for t in (80, 90)])
    down = make_event([make_market(f"Lowest temperature {t}°F?", group=f"↓ {t}°F") for t in (40, 30)])
    plus = make_event([make_market(f"Number of cuts {n}?", group=f"{n}+") for n in (2, 3)])
    assert detect_ladders(up)[0].direction is Direction.UP
    assert detect_ladders(down)[0].direction is Direction.DOWN
    assert values(detect_ladders(plus)[0]) == [3, 2]


def test_rejects_ranges_exactly_and_rank_questions():
    ranges = make_event([make_market(f"Will BTC be between ${a}k and ${a + 10}k?") for a in (100, 110)])
    dashes = make_event([make_market(f"Will BTC close at ${a}-{a + 5}k?") for a in (100, 105)])
    exact = make_event([make_market(f"Will the Fed cut exactly {n} times?") for n in (1, 2)])
    rank = make_event([make_market(f"Will the film be ranked {n} or higher?") for n in (1, 3)])
    for ev in (ranges, dashes, exact, rank):
        assert detect_ladders(ev) == []


def test_negrisk_events_are_never_ladders():
    markets = [make_market(f"Will BTC reach ${p}k?", neg_risk=True, index=i) for i, p in enumerate((100, 120))]
    assert detect_ladders(make_event(markets, neg_risk=True)) == []


def test_template_mismatch_and_no_direction():
    mixed = make_event([make_market("Will BTC reach $100k?"), make_market("Will ETH reach $5k?")])
    neutral = make_event([make_market(f"Will BTC be ${p}k on Friday?") for p in (100, 120)])
    assert detect_ladders(mixed) == []
    assert detect_ladders(neutral) == []


def test_duplicate_thresholds_are_dropped():
    qs = ["Will BTC reach $100k?", "Will BTC reach $120k?", "Will BTC reach $120k?", "Will BTC reach $140k?"]
    (ladder,) = detect_ladders(make_event([make_market(q) for q in qs]))
    assert values(ladder) == [140_000, 100_000]


def test_threshold_and_date_varying_together_is_not_a_ladder():
    both = make_event([
        make_market("Will BTC reach $100k by March 31?", end=dt(2027, 3, 31)),
        make_market("Will BTC reach $120k by May 31?", end=dt(2027, 5, 31)),
    ])
    assert detect_ladders(both) == []
    same_level = make_event([
        make_market("Will BTC reach $100k by March 31?", end=dt(2027, 3, 31)),
        make_market("Will BTC reach $100k by May 31?", end=dt(2027, 5, 31)),
    ])
    (ladder,) = detect_ladders(same_level)
    assert ladder.direction is Direction.BY_DATE


def test_on_date_questions_are_not_by_date_ladders():
    ev = make_event([
        make_market("Will it rain in NYC on June 30?", end=dt(2026, 6, 30)),
        make_market("Will it rain in NYC on July 31?", end=dt(2026, 7, 31)),
    ])
    assert detect_ladders(ev) == []


def test_by_date_ladder_rejected_when_end_dates_disagree():
    ev = make_event([
        make_market("Will X launch by June 30, 2026?", end=dt(2026, 12, 31)),
        make_market("Will X launch by December 31, 2026?", end=dt(2026, 6, 30)),
    ])
    assert detect_ladders(ev) == []


def test_year_inference_uses_market_end_date():
    assert _resolve_year(3, 31, None, dt(2027, 3, 31)) == date(2027, 3, 31)
    assert _resolve_year(1, 15, None, dt(2026, 12, 20)) == date(2027, 1, 15)
    assert _resolve_year(2, 30, 2026, None) == date(2026, 2, 28)
    ev = make_event([
        make_market("Will X happen by January 31?", end=dt(2027, 1, 31)),
        make_market("Will X happen by March 31?", end=dt(2027, 3, 31)),
    ])
    (ladder,) = detect_ladders(ev)
    assert [r.value for r in ladder.rungs] == [date(2027, 1, 31).toordinal(), date(2027, 3, 31).toordinal()]


def test_question_template_masks_numbers_and_dates():
    assert question_template("Will BTC reach $120,000 by Dec 31, 2026?") == "will btc reach # by @?"
    assert question_template("Will ETH dip to $2.5k in March 2027?") == "will eth dip to # in @?"


def test_ladder_key_is_deterministic():
    markets = [make_market(f"Will BTC reach ${p}k?") for p in (100, 120)]
    ev = make_event(markets)
    assert detect_ladders(ev)[0].key == detect_ladders(ev)[0].key
    assert detect_ladders(ev)[0].key.startswith(f"{ev.id}:up:")
