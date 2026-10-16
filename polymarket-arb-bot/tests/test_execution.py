from decimal import Decimal
from types import SimpleNamespace

import pytest

from polyarb.book import OrderBook
from polyarb.config import ConvertConfig, ExecutionConfig, RiskConfig, YesBasketConfig
from polyarb.execution.executor import Executor
from polyarb.execution.live import PolymarketGateway, _cents, _price
from polyarb.execution.paper import PaperGateway
from polyarb.models import FeeSchedule, Leg, Outcome, Side
from polyarb.risk import RiskManager
from polyarb.strategies.negrisk import NegRiskConvert, NegRiskYesBasket

from conftest import MARKET_ID, book, make_event, make_market

OPEN = ConvertConfig(min_profit_usd=0.0, min_edge=0.0)


def convert_setup(store, bids, asks, depth=20):
    ms = [make_market(f"Will {i} win?", group=f"O{i}", neg_risk=True, index=i) for i in range(len(bids))]
    ev = make_event(ms, neg_risk=True)
    for m, b, a in zip(ms, bids, asks):
        store.put(book(m.yes_token, bids=[(b, depth)], asks=[(a, depth)]))
    strat = NegRiskConvert(OPEN)
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, 0.0)
    return strat, target, opp, ms


def executor(store, strat, gateway=None, clob=None, **cfg):
    gw = gateway or PaperGateway(store)
    return Executor(gw, store, {strat.name: strat}, ExecutionConfig(**cfg), clob), gw


class FakeClob:
    """Returns new book objects on every request, like the REST endpoint."""

    def __init__(self, books):
        self.books = books

    async def get_books(self, tokens):
        return {
            t: OrderBook(t, [(l.price, l.size) for l in b.bids], [(l.price, l.size) for l in b.asks])
            for t, b in self.books.items()
            if t in tokens
        }


def exchange(ms, bids, depth):
    return {m.yes_token: book(m.yes_token, bids=[(b, depth)], asks=[(b + 0.02, depth)]) for m, b in zip(ms, bids)}


# ---------------------------------------------------------------- paper gateway


async def test_paper_buy_consumes_liquidity_and_rejects_fok(store):
    store.put(book("Y1", bids=[(0.40, 10)], asks=[(0.45, 10)]))
    leg = Leg("N1", "1", Outcome.NO, Side.BUY, 10, 6.0, 0.0, 0.60, 0.01, 5, True, "NO 1", yes_token="Y1")
    gw = PaperGateway(store)
    (first,) = await gw.buy([leg])
    assert first.ok and first.shares == pytest.approx(10) and first.spent == pytest.approx(6.0)
    assert store.raw("Y1").bids == []  # buying NO lifted the YES bid
    (second,) = await gw.buy([leg])
    assert not second.ok and second.status == "unmatched"


async def test_paper_sell_respects_min_price(store):
    store.put(book("Y1", bids=[(0.40, 5), (0.30, 5)]))
    gw = PaperGateway(store)
    res = await gw.sell("Y1", Outcome.YES, "Y1", 10, 0.35)
    assert res.ok and res.shares == pytest.approx(5) and res.proceeds == pytest.approx(2.0)
    assert (await gw.sell("Y1", Outcome.YES, "Y1", 10, 0.35)).ok is False


async def test_paper_fees_charged_on_fills(store):
    fee = FeeSchedule(rate=0.05)
    store.put(book("Y1", asks=[(0.50, 100)]))
    leg = Leg("Y1", "1", Outcome.YES, Side.BUY, 10, 5.0, 0.125, 0.50, 0.01, 5, False, "YES", yes_token="Y1", fee_schedule=fee)
    (res,) = await PaperGateway(store).buy([leg])
    assert res.fee == pytest.approx(10 * 0.05 * 0.25)


# ---------------------------------------------------------------- executor: convert paths


async def test_complete_convert(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    ex, gw = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.complete and rep.action == "converted"
    assert gw.converted == [(MARKET_ID, 0b111, 20.0)]
    assert rep.proceeds == pytest.approx(40.0)
    assert rep.realized_pnl == pytest.approx(opp.profit) == pytest.approx(1.0)


async def test_partial_fill_completed_by_retry(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    store.put(book(ms[2].yes_token, bids=[(0.29, 100)], asks=[(0.32, 100)]))  # NO ask moved 0.70 -> 0.71
    ex, gw = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.complete and rep.action == "completed after retry"
    assert [r.ok for r in rep.legs] == [True, True, False] and rep.retries[0].ok
    assert gw.converted[0][1:] == (0b111, 20.0)
    assert rep.realized_pnl == pytest.approx(40 - (12 + 13 + 14.2))


async def test_partial_fill_unwinds_when_cheapest(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    store.put(book(ms[2].yes_token, asks=[(0.32, 100)]))  # leg 3 has no NO liquidity left
    ex, gw = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert not rep.complete and rep.action == "unwind"
    assert gw.converted == []
    assert [s.ok for s in rep.sells] == [True, True]
    assert rep.realized_pnl == pytest.approx((20 * 0.58 + 20 * 0.63) - (12 + 13))


async def test_partial_fill_converts_filled_subset(store):
    strat, target, opp, ms = convert_setup(store, (0.30, 0.30, 0.25, 0.20), (0.40, 0.40, 0.35, 0.30))
    assert opp.meta["legs_used"] == 4
    store.put(book(ms[3].yes_token, asks=[(0.30, 100)]))
    ex, gw = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.action == "convert filled subset"
    assert gw.converted == [(MARKET_ID, 0b0111, 20.0)]
    assert rep.realized_pnl == pytest.approx(2 * 20 - (14 + 14 + 15))


async def test_expensive_unwind_keeps_legs_and_kills(store, tmp_path):
    strat, target, opp, ms = convert_setup(store, (0.55, 0.50), (0.99, 0.52))
    store.put(book(ms[1].yes_token, asks=[(0.52, 100)]))
    ex, gw = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.orphaned and rep.sells == [] and gw.converted == []
    assert rep.locked_cost == pytest.approx(9.0) and rep.expected_payout == pytest.approx(0.2)
    assert rep.realized_pnl == pytest.approx(0.0)
    risk = RiskManager(RiskConfig(kill_file=""), tmp_path / "s.json")
    risk.finish(rep)
    assert risk.state.killed and risk.hold_exposure == pytest.approx(9.0)


async def test_no_fills_is_a_clean_failure(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    for m in ms:
        store.put(book(m.yes_token, asks=[(0.5, 10)]))
    ex, _ = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.sent and not rep.complete and rep.action == "no fills" and rep.realized_pnl == 0


# ---------------------------------------------------------------- executor: recheck + holds


async def test_recheck_aborts_when_edge_vanished(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    fresh = {m.yes_token: book(m.yes_token, bids=[(0.30, 20)], asks=[(0.32, 20)]) for m in ms}
    ex, gw = executor(store, strat, clob=FakeClob(fresh))
    rep = await ex.execute(opp, target)
    assert rep.action == "abort: vanished on recheck" and not rep.sent
    assert gw.converted == []


async def test_recheck_resizes_to_fresh_depth(store):
    strat, target, opp, ms = convert_setup(store, (0.40, 0.35, 0.30), (0.42, 0.37, 0.32))
    fresh = {m.yes_token: book(m.yes_token, bids=[(b, 10)], asks=[(b + 0.02, 10)]) for m, b in zip(ms, (0.40, 0.35, 0.30))}
    ex, gw = executor(store, strat, clob=FakeClob(fresh))
    rep = await ex.execute(opp, target)
    assert rep.complete and rep.opp.shares == 10 and gw.converted[0][2] == 10.0


async def test_recheck_upsize_must_be_reapproved(store):
    bids = (0.40, 0.35, 0.30)
    strat, target, opp, ms = convert_setup(store, bids, (0.42, 0.37, 0.32), depth=10)
    ex, gw = executor(store, strat, clob=FakeClob(exchange(ms, bids, 40)))
    approved = opp.cost
    rep = await ex.execute(opp, target, approve=lambda o: "too big" if o.cost > approved + 1e-9 else None)
    assert rep.action == "abort: too big" and not rep.sent and gw.converted == []


async def test_paper_does_not_retrade_unchanged_liquidity(store):
    bids = (0.40, 0.35, 0.30)
    strat, target, opp, ms = convert_setup(store, bids, (0.42, 0.37, 0.32))
    clob = FakeClob(exchange(ms, bids, 20))
    ex, gw = executor(store, strat, clob=clob)
    assert (await ex.execute(opp, target)).complete
    again = await ex.execute(opp, target)
    assert again.action == "abort: paper liquidity already used" and not again.sent
    clob.books = exchange(ms, bids, 25)
    rep = await ex.execute(opp, target)
    assert rep.complete and [c[2] for c in gw.converted] == [20.0, 25.0]


async def test_yes_basket_locks_hold(store, tmp_path):
    ms = [make_market(f"Will {x} win?", group=x, neg_risk=True, index=i) for i, x in enumerate(["A", "B", "Other"])]
    ev = make_event(ms, neg_risk=True)
    for m, a in zip(ms, (0.30, 0.30, 0.35)):
        store.put(book(m.yes_token, asks=[(a, 50)]))
    strat = NegRiskYesBasket(YesBasketConfig())
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, ms[0].end_date.timestamp() - 10 * 86400)
    ex, _ = executor(store, strat)
    rep = await ex.execute(opp, target)
    assert rep.complete and rep.action == "holding to resolution"
    assert rep.locked_cost == pytest.approx(47.5) and rep.expected_payout == pytest.approx(50.0)
    assert rep.realized_pnl == pytest.approx(0.0)
    risk = RiskManager(RiskConfig(kill_file=""), tmp_path / "s.json")
    risk.finish(rep)
    assert risk.state.holds[0].payout == pytest.approx(50.0)


# ---------------------------------------------------------------- live gateway (fake SDK client)


class FakeSdk:
    def __init__(self, statuses=None, held=0.0, matched=None):
        self.statuses = statuses or {}
        self.batches: list[int] = []
        self.cancelled: list[str] = []
        self.held = held
        self.matched = matched  # None -> get_order fails and the gateway falls back to the balance

    async def create_market_order(self, **kw):
        return kw

    async def post_orders(self, orders):
        from polymarket.models.clob.order_response import AcceptedOrder, RejectedOrder

        self.batches.append(len(orders))
        out = []
        for o in orders:
            status = self.statuses.get(o["token_id"], "matched")
            if status == "fok_not_filled":
                out.append(RejectedOrder(code="fok_not_filled", message="killed"))
                continue
            spent = o["amount"]
            shares = Decimal("0") if status == "live" else (spent / o["max_price"]).quantize(Decimal("0.01"))
            out.append(AcceptedOrder(order_id=f"id-{o['token_id']}", status=status, making_amount=spent,
                                     taking_amount=shares, trade_ids=(), transactions_hashes=()))
        return out

    async def cancel_order(self, order_id):
        self.cancelled.append(order_id)

    async def get_order(self, order_id):
        if self.matched is None:
            raise RuntimeError("404")
        return SimpleNamespace(size_matched=Decimal(str(self.matched[order_id])))

    async def get_balance_allowance(self, asset_type, token_id=None):
        return SimpleNamespace(balance=int(self.held * 1e6))


def live_leg(i, notional=6.0, limit=0.6012):
    return Leg(f"T{i}", str(i), Outcome.YES, Side.BUY, 10, notional, 0.0, limit, 0.01, 5, False, f"leg {i}", yes_token=f"T{i}")


def test_price_and_amount_rounding():
    assert _price(0.6012, 0.01) == Decimal("0.6")
    assert _price(0.123, 0.001) == Decimal("0.123")
    assert _price(0.57, 0.01) == Decimal("0.57")
    assert _cents(6.239) == Decimal("6.23")


async def test_live_buy_batches_and_maps_results():
    pytest.importorskip("polymarket")
    sdk = FakeSdk(statuses={"T3": "fok_not_filled"})
    gw = PolymarketGateway(sdk, chain=SimpleNamespace())
    res = await gw.buy([live_leg(i) for i in range(20)])
    assert sdk.batches == [15, 5]
    assert sum(r.ok for r in res) == 19
    bad = res[3]
    assert not bad.ok and bad.status == "fok_not_filled"
    assert res[0].shares == pytest.approx(10.0) and res[0].spent == pytest.approx(6.0)


async def test_live_delayed_and_resting_orders_reconciled_from_order_status():
    pytest.importorskip("polymarket")
    sdk = FakeSdk(statuses={"T0": "delayed", "T1": "live"}, held=99.0, matched={"id-T0": 10, "id-T1": 4})
    gw = PolymarketGateway(sdk, chain=SimpleNamespace(), delayed_wait_s=0)
    res = await gw.buy([live_leg(0), live_leg(1)])
    assert sdk.cancelled == ["id-T0", "id-T1"]
    assert [(r.ok, r.status, r.shares) for r in res] == [(True, "reconciled", 10.0), (True, "reconciled", 4.0)]
    sdk.matched = {"id-T1": 0}
    (none,) = await gw.buy([live_leg(1)])
    assert not none.ok and none.error == "no fill after reconcile"


async def test_live_reconcile_falls_back_to_capped_balance():
    pytest.importorskip("polymarket")
    sdk = FakeSdk(statuses={"T0": "delayed"}, held=99.0)
    gw = PolymarketGateway(sdk, chain=SimpleNamespace(), delayed_wait_s=0)
    (res,) = await gw.buy([live_leg(0)])
    assert res.ok and res.shares == pytest.approx(10.5)  # pre-existing balance capped at planned size + 5%
