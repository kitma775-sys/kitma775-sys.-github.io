from datetime import timedelta

import pytest

from polyarb.config import ConvertConfig, LadderConfig, UniverseConfig, YesBasketConfig
from polyarb.models import FeeSchedule, Outcome, Settlement
from polyarb.strategies.ladder import LadderArb
from polyarb.strategies.negrisk import NegRiskConvert, NegRiskYesBasket, exhaustive_reason
from polyarb.universe import build_universe, filter_events

from conftest import MARKET_ID, NOW, book, make_event, make_market

POLITICS = FeeSchedule(rate=0.04)
OPEN = dict(min_profit_usd=0.0, min_edge=0.0)


def negrisk_event(labels, **kw):
    markets = [make_market(f"Will {label} win?", group=label, neg_risk=True, index=i, **kw) for i, label in enumerate(labels)]
    return make_event(markets, neg_risk=True), markets


# ---------------------------------------------------------------- NO-basket convert


def test_convert_picks_most_profitable_subset(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "C", "D", "E"])
    for m, (bid, size) in zip(ms, [(0.40, 20), (0.35, 20), (0.30, 20), (0.02, 5)]):
        store.put(book(m.yes_token, bids=[(bid, size)]))
    store.put(book(ms[4].yes_token, asks=[(0.05, 100)]))  # no bids -> no NO liquidity
    strat = NegRiskConvert(ConvertConfig(**OPEN))
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, now_ts)
    # All four legs earn 0.07/unit but D's 5-share depth caps size; A+B+C earn 0.05 on 20 units.
    assert opp.meta["legs_used"] == 3 and opp.meta["index_set"] == 0b111
    assert opp.shares == 20
    assert opp.payout_floor == pytest.approx(40.0)
    assert opp.cost == pytest.approx(20 * (0.60 + 0.65 + 0.70))
    assert opp.profit == pytest.approx(1.0)
    assert opp.settlement is Settlement.CONVERT and opp.risk == "low"
    assert all(leg.outcome is Outcome.NO and leg.token_id == m.no_token for leg, m in zip(opp.legs, ms))
    assert [leg.neg_risk_index for leg in opp.legs] == [0, 1, 2]
    assert opp.meta["neg_risk_market_id"] == MARKET_ID
    assert opp.apr() is None and opp.days_to_resolution() == 0.0


def test_convert_top_edge_and_fees(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "C"], fee=POLITICS)
    for m, bid in zip(ms, (0.40, 0.35, 0.30)):
        store.put(book(m.yes_token, bids=[(bid, 50)]))
    strat = NegRiskConvert(ConvertConfig(**OPEN))
    (target,) = strat.targets([ev])
    fee_per_unit = sum(0.04 * p * (1 - p) for p in (0.60, 0.65, 0.70))
    assert strat.top_edge(target, store) == pytest.approx(0.05 - fee_per_unit)
    (opp,) = strat.evaluate(target, store, now_ts)
    assert opp.fees == pytest.approx(50 * fee_per_unit)
    assert opp.profit == pytest.approx(50 * (0.05 - fee_per_unit))


def test_convert_respects_gates_and_trade_cap(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "C"])
    for m, bid in zip(ms, (0.40, 0.35, 0.30)):
        store.put(book(m.yes_token, bids=[(bid, 100)]))
    (target,) = NegRiskConvert(ConvertConfig()).targets([ev])
    assert NegRiskConvert(ConvertConfig(min_profit_usd=10.0)).evaluate(target, store, now_ts) == []
    assert NegRiskConvert(ConvertConfig(min_edge=0.05)).evaluate(target, store, now_ts) == []
    (capped,) = NegRiskConvert(ConvertConfig(**OPEN), max_trade_usd=39.0).evaluate(target, store, now_ts)
    assert capped.cost <= 39.0 + 1e-9 and capped.shares == 20


def test_convert_no_opportunity_when_bids_underround(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "C"])
    for m, bid in zip(ms, (0.40, 0.30, 0.25)):
        store.put(book(m.yes_token, bids=[(bid, 100)]))
    strat = NegRiskConvert(ConvertConfig(**OPEN))
    (target,) = strat.targets([ev])
    assert strat.evaluate(target, store, now_ts) == []
    assert strat.top_edge(target, store) == pytest.approx(-0.05)


def test_convert_targets_require_v1_negrisk_with_indices():
    ev, _ = negrisk_event(["A", "B"])
    v2, _ = negrisk_event(["A", "B"])
    v2.version = "v2"
    no_index = make_event([make_market("x", neg_risk=True), make_market("y", neg_risk=True)], neg_risk=True)
    single, _ = negrisk_event(["A"])
    strat = NegRiskConvert(ConvertConfig())
    assert [t.key for t in strat.targets([ev, v2, no_index, single])] == [f"convert:{ev.id}"]


# ---------------------------------------------------------------- YES basket


def basket_books(store, ms, asks, depth=50):
    for m, a in zip(ms, asks):
        store.put(book(m.yes_token, bids=[(round(a - 0.02, 2), depth)], asks=[(a, depth)]))


def test_exhaustive_reason():
    assert exhaustive_reason(negrisk_event(["A", "B", "Other"])[0]) == "catch-all-label"
    assert exhaustive_reason(negrisk_event(["<10", "10-20", "20+"])[0]) == "open-ended-partition"
    assert exhaustive_reason(negrisk_event(["A", "B"])[0]) is None
    ev, ms = negrisk_event(["A", "B"])
    ms[1].neg_risk_other = True
    assert exhaustive_reason(ev) == "other-market"


def test_yes_basket_buys_every_outcome(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "Other"])
    basket_books(store, ms, (0.30, 0.30, 0.35))
    strat = NegRiskYesBasket(YesBasketConfig())
    (target,) = strat.targets([ev])
    assert strat.top_edge(target, store) == pytest.approx(0.05)
    (opp,) = strat.evaluate(target, store, now_ts)
    assert opp.shares == 50 and opp.payout_floor == pytest.approx(50.0)
    assert opp.profit == pytest.approx(2.5)
    assert opp.settlement is Settlement.RESOLUTION and opp.risk == "medium"
    assert opp.resolves_at == NOW + timedelta(days=11)
    assert opp.apr(NOW) == pytest.approx(opp.edge * 365 / 11)
    assert {leg.token_id for leg in opp.legs} == {m.yes_token for m in ms}
    assert opp.meta["exhaustive"] == "catch-all-label"


def test_yes_basket_target_filters():
    strat = NegRiskYesBasket(YesBasketConfig())
    plain, _ = negrisk_event(["A", "B"])
    augmented, _ = negrisk_event(["A", "Other"])
    augmented.neg_risk_augmented = True
    paused, ms = negrisk_event(["A", "B", "Other"])
    ms[0].accepting_orders = False
    resolved_yes, ms2 = negrisk_event(["A", "B", "Other"])
    ms2[0].accepting_orders, ms2[0].closed, ms2[0].resolved_outcome = False, True, "YES"
    resolved_no, ms3 = negrisk_event(["A", "B", "Other"])
    ms3[0].accepting_orders, ms3[0].closed, ms3[0].resolved_outcome = False, True, "NO"
    keys = [t.key for t in strat.targets([plain, augmented, paused, resolved_yes, resolved_no])]
    assert keys == [f"yes:{resolved_no.id}"]
    assert len(NegRiskYesBasket(YesBasketConfig(require_catch_all=False)).targets([plain])) == 1


def test_yes_basket_gates(store, now_ts):
    strat = NegRiskYesBasket(YesBasketConfig())
    far, ms = negrisk_event(["A", "B", "Other"], end=NOW + timedelta(days=100))
    basket_books(store, ms, (0.30, 0.30, 0.35))
    assert strat.evaluate(strat.targets([far])[0], store, now_ts) == []  # beyond max_days

    decided, ms = negrisk_event(["A", "B", "Other"])
    basket_books(store, ms, (0.20, 0.20, 0.20))
    assert strat.evaluate(strat.targets([decided])[0], store, now_ts) == []  # ask sum below min_ask_sum

    thin, ms = negrisk_event(["A", "B", "Other"], end=NOW + timedelta(days=40))
    basket_books(store, ms, (0.33, 0.33, 0.33), depth=200)
    (target,) = strat.targets([thin])
    assert strat.evaluate(target, store, now_ts) == []  # ~9% APR < 25%
    (opp,) = NegRiskYesBasket(YesBasketConfig(min_apr=0.05)).evaluate(target, store, now_ts)
    assert opp.profit == pytest.approx(2.0)


def test_yes_basket_needs_every_book(store, now_ts):
    ev, ms = negrisk_event(["A", "B", "Other"])
    basket_books(store, ms[:2], (0.30, 0.30))
    strat = NegRiskYesBasket(YesBasketConfig())
    (target,) = strat.targets([ev])
    assert strat.evaluate(target, store, now_ts) == []
    assert strat.top_edge(target, store) is None


# ---------------------------------------------------------------- ladders


def ladder_event(prices, end=None):
    ms = [make_market(f"Will BTC reach ${p}k by December 31?", end=end) for p in prices]
    return make_event(ms), ms


def test_ladder_buys_yes_weak_and_no_strong(store, now_ts):
    ev, (strong, weak) = ladder_event((130, 120))
    store.put(book(strong.yes_token, bids=[(0.30, 50)], asks=[(0.32, 50)]))
    store.put(book(weak.yes_token, bids=[(0.23, 50)], asks=[(0.25, 50)]))
    strat = LadderArb(LadderConfig())
    (target,) = strat.targets([ev])
    assert strat.top_edge(target, store) == pytest.approx(0.05)
    (opp,) = strat.evaluate(target, store, now_ts)
    yes_leg, no_leg = opp.legs
    assert (yes_leg.token_id, yes_leg.outcome) == (weak.yes_token, Outcome.YES)
    assert (no_leg.token_id, no_leg.outcome) == (strong.no_token, Outcome.NO)
    assert yes_leg.avg_price == pytest.approx(0.25) and no_leg.avg_price == pytest.approx(0.70)
    assert opp.shares == 50 and opp.profit == pytest.approx(2.5)
    assert opp.meta["direction"] == "up"
    assert opp.key == f"{target.key}:{strong.id}>{weak.id}"


def test_ladder_returns_only_best_pair(store, now_ts):
    ev, (a, b, c) = ladder_event((140, 130, 120))
    store.put(book(a.yes_token, bids=[(0.30, 50)], asks=[(0.33, 50)]))
    store.put(book(b.yes_token, bids=[(0.31, 50)], asks=[(0.34, 50)]))
    store.put(book(c.yes_token, bids=[(0.20, 50)], asks=[(0.22, 50)]))
    opps = LadderArb(LadderConfig()).evaluate(LadderArb(LadderConfig()).targets([ev])[0], store, now_ts)
    assert len(opps) == 1
    assert opps[0].legs[0].token_id == c.yes_token and opps[0].legs[1].token_id == b.no_token


def test_ladder_consistent_prices_give_nothing(store, now_ts):
    ev, (strong, weak) = ladder_event((130, 120))
    store.put(book(strong.yes_token, bids=[(0.20, 50)], asks=[(0.22, 50)]))
    store.put(book(weak.yes_token, bids=[(0.30, 50)], asks=[(0.32, 50)]))
    strat = LadderArb(LadderConfig(**OPEN, min_apr=0.0))
    (target,) = strat.targets([ev])
    assert strat.evaluate(target, store, now_ts) == []
    assert strat.top_edge(target, store) == pytest.approx(1 - 0.80 - 0.32)


def test_ladder_time_gates(store, now_ts):
    far, (s1, w1) = ladder_event((130, 120), end=NOW + timedelta(days=120))
    store.put(book(s1.yes_token, bids=[(0.30, 50)]))
    store.put(book(w1.yes_token, asks=[(0.25, 50)]))
    strat = LadderArb(LadderConfig())
    assert strat.evaluate(strat.targets([far])[0], store, now_ts) == []  # beyond max_days

    slow, (s2, w2) = ladder_event((130, 120), end=NOW + timedelta(days=80))
    store.put(book(s2.yes_token, bids=[(0.30, 500)]))
    store.put(book(w2.yes_token, asks=[(0.28, 500)]))
    (target,) = strat.targets([slow])
    assert strat.evaluate(target, store, now_ts) == []  # 2% over 81 days ~ 9% APR
    assert len(LadderArb(LadderConfig(min_apr=0.05)).evaluate(target, store, now_ts)) == 1


def test_ladder_direction_filter():
    ev, _ = ladder_event((130, 120))
    assert LadderArb(LadderConfig(directions=["down"])).targets([ev]) == []


# ---------------------------------------------------------------- universe


def test_filter_events_drops_delayed_tagged_and_small_events():
    ok, _ = negrisk_event(["A", "B"])
    delayed, ms = negrisk_event(["A", "B"])
    ms[0].seconds_delay = 3
    tagged, _ = negrisk_event(["A", "B"])
    tagged.tags = ("sports",)
    small, _ = negrisk_event(["A"])
    kept = filter_events([ok, delayed, tagged, small], UniverseConfig(exclude_tags=["sports"]))
    assert kept == [ok]


def test_build_universe_indexes_targets_by_token():
    ev, ms = negrisk_event(["A", "B", "Other"])
    u = build_universe([ev], [NegRiskConvert(ConvertConfig()), NegRiskYesBasket(YesBasketConfig())], UniverseConfig())
    assert u.counts() == {"negrisk_convert": 1, "negrisk_yes_basket": 1}
    assert set(u.tokens) == {m.yes_token for m in ms}
    assert len(u.by_token[ms[0].yes_token]) == 2
