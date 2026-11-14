from datetime import timedelta

import pytest

from polyarb.config import Config
from polyarb.execution.maker_arb import MakerArbManager
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.models import Outcome
from polyarb.risk import RiskManager
from polyarb.universe import build_strategies, build_universe

from conftest import NOW, book, make_event, make_market


def _convert_event_with_wide_lead(store):
    """Taker convert is unprofitable (-1c), but quoting lead NO as maker (+2c inside spread) earns +1c/sh!"""
    ms = [
        make_market("Will A win?", group="A", neg_risk=True, index=0),
        make_market("Will B win?", group="B", neg_risk=True, index=1),
        make_market("Will C win?", group="C", neg_risk=True, index=2),
    ]
    for m in ms:
        m.volume_24h = 2000.0
    ev = make_event(ms, neg_risk=True, title="Tri-way Election")
    # Taker YES bids: 0.34 + 0.33 + 0.32 = 0.99 (< 1.00 => 0 taker convert opps!).
    # Lead market A has wide YES book [0.34 .. 0.38] => NO book is [bid=0.62 .. ask=0.66].
    # Stepping 1 tick above NO bid = BUY NO @ 0.63 (postOnly!), plus hedge NO on B (0.67) + C (0.68) = 1.98 < 2.00 (+2c/sh profit!).
    store.put(book(ms[0].yes_token, bids=[(0.34, 100)], asks=[(0.38, 100)]))
    store.put(book(ms[1].yes_token, bids=[(0.33, 100)], asks=[(0.35, 100)]))
    store.put(book(ms[2].yes_token, bids=[(0.32, 100)], asks=[(0.34, 100)]))
    return ev, ms


async def test_maker_convert_quotes_reserves_hedges_and_converts_on_fill(store, tmp_path):
    ev, ms = _convert_event_with_wide_lead(store)
    cfg = Config()
    cfg.risk.kill_file = str(tmp_path / "KILL")
    cfg.risk.max_trade_usd = 50.0
    strategies = build_strategies(cfg)
    universe = build_universe([ev], strategies, cfg.universe)

    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    # Step 1: Places resting postOnly BUY NO order @ 0.63 on market A and reserves capital.
    await maker.step()
    assert len(maker.active) == 1
    q = maker.active[ev.id]
    assert q.plan.strategy == "maker_convert"
    assert q.plan.lead_market.id == ms[0].id and q.plan.lead_outcome is Outcome.NO
    assert q.plan.lead_price == pytest.approx(0.63)
    assert risk.reserved_usd == pytest.approx(q.reserved_usd)
    assert risk.available() == pytest.approx(1000.0 - q.reserved_usd)

    # Step 2: Simulate a retail buyer lifting our YES ask at 0.37 (i.e. NO ask touches 0.63).
    store.put(book(ms[0].yes_token, bids=[(0.37, 50)], asks=[(0.38, 100)]))
    await maker.step()

    # Quote is now hedged via FOK on B+C and converted to pUSD on-chain!
    assert len(maker.active) == 0
    assert risk.reserved_usd == 0.0
    assert len(gw.converted) == 1
    assert risk.state.trades == 1
    assert risk.state.realized_total == pytest.approx(q.plan.shares * (2.0 - (0.63 + 0.67 + 0.68)))


async def test_maker_quote_cancels_immediately_when_hedge_drifts_or_paused(store, tmp_path):
    ev, ms = _convert_event_with_wide_lead(store)
    cfg = Config()
    cfg.risk.kill_file = str(tmp_path / "KILL")
    cfg.risk.max_trade_usd = 50.0
    universe = build_universe([ev], build_strategies(cfg), cfg.universe)
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    await maker.step()
    assert len(maker.active) == 1

    # Hedge leg B's YES bid drops from 0.33 to 0.25 => convert edge goes negative => immediate cancel!
    store.put(book(ms[1].yes_token, bids=[(0.25, 100)], asks=[(0.35, 100)]))
    await maker.step()
    assert len(maker.active) == 0 and maker.quotes_canceled == 1 and risk.reserved_usd == 0.0

    # Restore book, place quote again, then touch KILL file => cancels immediately!
    store.put(book(ms[1].yes_token, bids=[(0.33, 100)], asks=[(0.35, 100)]))
    await maker.step()
    assert len(maker.active) == 1
    (tmp_path / "KILL").write_text("")
    await maker.step()
    assert len(maker.active) == 0 and risk.reserved_usd == 0.0


async def test_maker_ladder_quotes_weak_yes_and_hedges_strong_no(store, tmp_path):
    end = NOW + timedelta(days=5)
    strong = make_market("Will BTC reach $130k by November 20?", end=end)
    weak = make_market("Will BTC reach $120k by November 20?", end=end)
    strong.volume_24h = weak.volume_24h = 1500.0
    ev = make_event([strong, weak], title="BTC Nov 20 Ladder")

    # Taker ladder: NO_strong ask = 1 - 0.32 = 0.68, YES_weak ask = 0.33 => 0.68 + 0.33 = 1.01 (no taker arb).
    # Weak YES book is [0.28 .. 0.33]: quoting BUY YES_weak @ 0.30 (inside spread) + hedging NO_strong @ 0.68 = 0.98 (+2c/sh!).
    store.put(book(strong.yes_token, bids=[(0.32, 100)], asks=[(0.34, 100)]))
    store.put(book(weak.yes_token, bids=[(0.28, 100)], asks=[(0.33, 100)]))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.max_trade_usd = 40.0
    universe = build_universe([ev], build_strategies(cfg), cfg.universe)
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    await maker.step()
    assert len(maker.active) == 1
    q = maker.active[ev.id]
    assert q.plan.strategy == "maker_ladder" and q.plan.lead_price == pytest.approx(0.30)

    # Seller hits our weak YES bid @ 0.30
    store.put(book(weak.yes_token, bids=[(0.27, 100)], asks=[(0.30, 50)]))
    await maker.step()
    assert len(maker.active) == 0
    assert len(risk.state.holds) == 1
    assert risk.state.holds[0].payout - risk.state.holds[0].cost == pytest.approx(q.plan.shares * 0.02)
