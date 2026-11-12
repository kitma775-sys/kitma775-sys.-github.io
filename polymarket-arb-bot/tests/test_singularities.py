from datetime import timedelta

import httpx
import pytest

from polyarb.config import Config, ConvertConfig, ExecutionConfig, TwinConfig
from polyarb.data.gamma import parse_event
from polyarb.execution.executor import Executor
from polyarb.execution.lifecycle import LifecycleManager
from polyarb.execution.maker_arb import MakerArbManager
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.models import Outcome
from polyarb.risk import HoldPosition, RiskManager
from polyarb.strategies.negrisk import NegRiskConvert
from polyarb.strategies.twin import TwinPartitionArb
from polyarb.universe import build_strategies, build_universe

from conftest import NOW, book, make_event, make_market


# ---------------------------------------------------------------- Singularity 1: Concentrated Convert + Surplus YES


async def test_concentrated_2_leg_convert_unlocks_small_cap_and_sells_surplus_yes(store, tmp_path, now_ts):
    """8-outcome event on a $25 trade cap:
    - Full 8-leg NO basket costs ~$6.89/sh -> 3.62 sh < 5.0 min_size (100% blocked without concentration!).
    - Concentrated 2-leg NO basket on favorites A+B costs $1.01/sh -> buys 24.75 sh (~7x volume!),
      converts {A, B} on-chain for $24.75 pUSD, and immediately sells free minted YES on C..H for +$2.97!
    """
    ms = [make_market(f"Will O{i} win?", group=f"O{i}", neg_risk=True, index=i, tick=0.001) for i in range(8)]
    ev = make_event(ms, neg_risk=True, title="8-Way Championship")

    # Favorites A (YES bid=0.51 => NO ask=0.49) and B (YES bid=0.48 => NO ask=0.52) -> sum 0.99 (< 1.00)
    store.put(book(ms[0].yes_token, bids=[(0.51, 200)], asks=[(0.53, 200)]))
    store.put(book(ms[1].yes_token, bids=[(0.48, 200)], asks=[(0.50, 200)]))
    # 6 longshots C..H each have deep YES bid = 0.008 (depth 200 >= 2x shares):
    # Without surplus valuation: 3 legs = 0.998 (<1.00), 4 legs = $0.05 (<$0.10 gate), 8 legs = $6.96/sh -> 3.59 sh < 5.0 min_size!
    for m in ms[2:]:
        store.put(book(m.yes_token, bids=[(0.008, 200)], asks=[(0.02, 200)]))

    disabled_strat = NegRiskConvert(ConvertConfig(min_profit_usd=0.10, min_edge=0.002, surplus_haircut=0.0), max_trade_usd=20.0)
    (t0,) = disabled_strat.targets([ev])
    assert disabled_strat.evaluate(t0, store, now_ts) == []

    # With conservative 0.85 surplus haircut + 2x depth guard, 2-leg concentrated convert fires at 19.80 shares on $20!
    strat = NegRiskConvert(ConvertConfig(min_profit_usd=0.10, min_edge=0.002, surplus_haircut=0.85), max_trade_usd=20.0)
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, now_ts)
    assert opp.meta["legs_used"] == 2
    assert opp.shares == pytest.approx(19.80)
    assert opp.meta["surplus_credit_usd"] > 0.55

    gw = PaperGateway(store)
    ex = Executor(gw, store, {strat.name: strat}, ExecutionConfig(recheck_books=False))
    rep = await ex.execute(opp, target)

    assert rep.complete and rep.convert_tx == "paper-convert"
    assert len(rep.sells) == 6  # all 6 free surplus YES tokens liquidated!
    assert rep.proceeds == pytest.approx(19.80)
    assert rep.realized_pnl == pytest.approx(19.80 + 6 * 19.80 * 0.008 - 19.80 * 1.01)
    assert rep.action == "converted + surplus sold"


# ---------------------------------------------------------------- Singularity 2: Cross-Event Twin Partition Arb


def test_twin_partition_arb_zero_tie_risk_and_oracle_guard(store, now_ts):
    end = NOW + timedelta(days=1)
    r_ms = [
        make_market("BTC < $80,000?", group="<80,000", neg_risk=True, index=0, end=end),
        make_market("BTC $80,000-$82,000?", group="80,000-82,000", neg_risk=True, index=1, end=end),
        make_market("BTC > $82,000?", group=">82,000", neg_risk=True, index=2, end=end),
    ]
    r_ev = make_event(r_ms, neg_risk=True, title="Bitcoin price on November 14?")
    r_ev.end_date = end
    r_ev.oracle_sig = "binance_1200et:btc_usdt"

    a_ms = [
        make_market("BTC above $80,000?", group="80,000", neg_risk=False, end=end),
        make_market("BTC above $82,000?", group="82,000", neg_risk=False, end=end),
    ]
    a_ev = make_event(a_ms, neg_risk=False, title="Bitcoin above ___ on November 14?")
    a_ev.end_date = end
    a_ev.oracle_sig = "binance_1200et:btc_usdt"

    # Top strike K=82,000: NO(above 82k) ask = 1 - 0.24 = 0.76, YES(>82k) ask = 0.22 => cost 0.98 (+2c/sh, 2 legs!)
    store.put(book(a_ms[0].yes_token, bids=[(0.55, 80)], asks=[(0.57, 80)]))
    store.put(book(a_ms[1].yes_token, bids=[(0.24, 80)], asks=[(0.26, 80)]))
    store.put(book(r_ms[0].yes_token, bids=[(0.40, 80)], asks=[(0.43, 80)]))
    store.put(book(r_ms[1].yes_token, bids=[(0.32, 80)], asks=[(0.35, 80)]))
    store.put(book(r_ms[2].yes_token, bids=[(0.20, 80)], asks=[(0.22, 80)]))

    strat = TwinPartitionArb(TwinConfig(min_profit_usd=0.10, min_edge=0.005), max_trade_usd=30.0)
    targets = strat.targets([r_ev, a_ev])
    assert len(targets) == 3  # 2 twin_upper (K=80k, K=82k) + 1 twin_lower (K=80k overlapping <82k)

    opps = [o for t in targets for o in strat.evaluate(t, store, now_ts)]
    assert len(opps) == 1
    opp = opps[0]
    assert opp.meta["strike"] == 82000.0 and len(opp.legs) == 2
    assert opp.meta["event_ids"] == [r_ev.id, a_ev.id]
    assert opp.profit > 0.50

    # Verify payoff >= 1.0*q at EVERY settlement price including exact boundary P == 82,000.00!
    for price in (79000.0, 80000.0, 81500.0, 82000.0, 85000.0):
        above_82k_yes = price > 82000.0
        range_gt_82k_yes = price >= 82000.0  # tie goes to higher bracket!
        unit_payout = (0.0 if above_82k_yes else 1.0) + (1.0 if range_gt_82k_yes else 0.0)
        assert unit_payout >= 1.0
        if price == 82000.0:
            assert unit_payout == 2.0  # boundary tie pays DOUBLE ($2.00)!

    # Mismatched oracle signature (e.g. non-Binance vs Binance) MUST refuse to pair!
    a_ev.oracle_sig = "pyth_close"
    assert strat.targets([r_ev, a_ev]) == []


def test_gamma_extracts_oracle_signatures_accurately():
    bin_raw = {
        "id": "1",
        "title": "Bitcoin price on November 14?",
        "negRisk": True,
        "markets": [
            {
                "id": "m1",
                "clobTokenIds": ["y1", "n1"],
                "outcomes": ["Yes", "No"],
                "active": True,
                "acceptingOrders": True,
                "description": 'Final "Close" price of the Binance 1 minute candle for BTC/USDT closing at 12:00 in the ET timezone at https://www.binance.com/en/trade/BTC_USDT.',
            }
        ],
    }
    pyth_raw = {
        "id": "2",
        "title": "NVIDIA (NVDA) closes week of Nov 9 at ___?",
        "negRisk": True,
        "markets": [
            {
                "id": "m2",
                "clobTokenIds": ["y2", "n2"],
                "outcomes": ["Yes", "No"],
                "active": True,
                "acceptingOrders": True,
                "description": "This market will resolve according to the Close price for NVIDIA. Closing prices will be used exactly as published by Pyth, without rounding.",
            }
        ],
    }
    assert parse_event(bin_raw).oracle_sig == "binance_1200et:btc_usdt"
    assert parse_event(pyth_raw).oracle_sig == "pyth_close"


# ---------------------------------------------------------------- Singularity 3: Ladder Middle-Zone Double Payout


async def test_ladder_middle_zone_early_harvest_locks_principal_and_captures_windfall(store, tmp_path):
    """WTI ladder: bought YES($70) + NO($75) for $19.70 (20 sh, expected base profit +$0.30).
    Mid-month WTI touches $70.20:
    - YES($70) immediately bids 0.995 ($19.90 > $19.70 cost => 100% risk-free atomic principal lock!).
    - NO($75) is still live and bidding 0.72 ($14.40!).
    LifecycleManager locks $19.90 on YES($70) first, then harvests $14.40 on NO($75) => +$14.60 (+74% return)!
    """
    cfg = Config()
    cfg.risk.kill_file = ""
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")

    # YES($70) bids 0.995; NO($75) bid = 0.72 <=> YES($75) ask = 0.28
    store.put(book("y_70", bids=[(0.995, 50)], asks=[(0.999, 50)]))
    store.put(book("y_75", bids=[(0.25, 50)], asks=[(0.28, 50)]))

    risk.state.holds = [
        HoldPosition(
            key="maker_ladder:wti",
            event_id="ev_wti",
            strategy="maker_ladder",
            title="What will Crude Oil (CL) hit in November?",
            cost=19.70,
            payout=20.00,
            shares=20.0,
            resolves_at=(NOW + timedelta(days=17)).isoformat(),
            opened_at=NOW.timestamp() - 300,
            legs=[
                {"token": "y_70", "yes_token": "y_70", "market_id": "m70", "outcome": "YES", "shares": 20.0, "tick": 0.001, "min_size": 5.0},
                {"token": "n_75", "yes_token": "y_75", "market_id": "m75", "outcome": "NO", "shares": 20.0, "tick": 0.01, "min_size": 5.0},
            ],
        )
    ]
    gw = PaperGateway(store)
    lc = LifecycleManager(cfg, gw, risk, journal, store=store)
    await lc.step()

    assert lc.early_harvests == 1
    assert risk.state.holds == []
    # $19.90 primary + $14.40 middle-zone windfall - $19.70 cost = +$14.60 realized PnL!
    assert risk.state.realized_total == pytest.approx(19.90 + 14.40 - 19.70)


async def test_paper_partial_resolution_redeems_winner_and_sells_live_sibling(store, tmp_path):
    """When weak barrier resolves to YES on Gamma while strong barrier is still open,
    paper auto-redeem credits $1.00/sh on the resolved winner AND sells the live sibling!"""
    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.execution.early_exit_enabled = False  # isolate _auto_redeem_paper path
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")

    # Strong rung YES ask = 0.30 <=> NO bid = 0.70
    store.put(book("y_strong", bids=[(0.28, 50)], asks=[(0.30, 50)]))
    risk.state.holds = [
        HoldPosition(
            key="ladder:wti",
            event_id="ev_wti",
            strategy="ladder",
            title="WTI November Ladder",
            cost=19.60,
            payout=20.0,
            shares=20.0,
            resolves_at=(NOW + timedelta(days=15)).isoformat(),
            opened_at=NOW.timestamp() - 600,
            legs=[
                {"token": "y_weak", "yes_token": "y_weak", "market_id": "mw", "outcome": "YES", "shares": 20.0, "tick": 0.01, "min_size": 5.0},
                {"token": "n_strong", "yes_token": "y_strong", "market_id": "ms", "outcome": "NO", "shares": 20.0, "tick": 0.01, "min_size": 5.0},
            ],
        )
    ]
    gamma_payload = {
        "id": "ev_wti",
        "title": "WTI November Ladder",
        "markets": [
            {"id": "mw", "clobTokenIds": ["y_weak", "n_weak"], "outcomes": ["Yes", "No"], "closed": True, "acceptingOrders": False, "outcomePrices": ["1", "0"]},
            {"id": "ms", "clobTokenIds": ["y_strong", "n_strong"], "outcomes": ["Yes", "No"], "closed": False, "acceptingOrders": True, "outcomePrices": ["0.29", "0.71"]},
        ],
    }
    gw = PaperGateway(store)
    lc = LifecycleManager(
        cfg,
        gw,
        risk,
        journal,
        store=store,
        http=httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json=gamma_payload))),
    )
    lc._last_paper_redeem_at = -100.0
    await lc.step()

    assert lc.redemptions == 1
    assert risk.state.holds == []
    assert risk.state.realized_total == pytest.approx(20.0 + 14.0 - 19.60)


# ---------------------------------------------------------------- Singularity 4: MakerArbManager 2.0 Volume Multipliers


async def test_maker_dual_orientation_1tick_join_bid_on_no_strong(store, tmp_path):
    """High-volume liquid ladder where spread is 1 tick and NO_strong is the maker leg:
    Strong YES book [0.74 .. 0.75] => NO_strong book [0.25 .. 0.26] (1-tick spread!).
    Weak YES book [0.73 .. 0.74] => hedge YES_weak @ 0.74.
    Joining best NO_strong bid @ 0.25 (postOnly, 0 fee) + YES_weak ask @ 0.74 = 0.99 (+1c/sh profit!).
    """
    end = NOW + timedelta(days=35)
    strong = make_market("Will WTI hit (HIGH) $75 in November?", group="↑ $75", end=end)
    weak = make_market("Will WTI hit (HIGH) $70 in November?", group="↑ $70", end=end)
    strong.volume_24h = weak.volume_24h = 25000.0
    ev = make_event([strong, weak], title="WTI Crude Oil Ladder")

    store.put(book(strong.yes_token, bids=[(0.74, 200)], asks=[(0.75, 200)]))
    store.put(book(weak.yes_token, bids=[(0.73, 200)], asks=[(0.74, 200)]))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.max_trade_usd = 25.0
    universe = build_universe([ev], build_strategies(cfg), cfg.universe)
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    await maker.step()
    assert len(maker.active) == 1
    q = maker.active[ev.id]
    assert q.plan.lead_market.id == strong.id and q.plan.lead_outcome is Outcome.NO
    assert q.plan.lead_price == pytest.approx(0.25)


async def test_maker_multi_lead_complementary_quotes_and_cofill_zero_fee_self_hedge(store, tmp_path):
    """2-way NegRisk event where BOTH outcomes A and B are quoted simultaneously as postOnly BUY NO @ 0.49.
    When a burst fills BOTH resting orders at once, MakerArbManager absorbs the sibling maker fill directly
    into convertPositions({A, B}) with ZERO taker hedge orders and ZERO taker fees (+2c/sh double profit)!"""
    ms = [
        make_market("Will A win?", group="A", neg_risk=True, index=0),
        make_market("Will B win?", group="B", neg_risk=True, index=1),
    ]
    for m in ms:
        m.volume_24h = 5000.0
    ev = make_event(ms, neg_risk=True, title="Head-to-Head Final")

    # Both A and B have YES book [0.50 .. 0.52] => NO book [0.48 .. 0.50].
    # Maker BUY NO(A) @ 0.49 + Taker NO(B) @ 0.50 = 0.99 (+1c/sh); symmetric for B!
    store.put(book(ms[0].yes_token, bids=[(0.50, 200)], asks=[(0.52, 200)]))
    store.put(book(ms[1].yes_token, bids=[(0.50, 200)], asks=[(0.52, 200)]))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.max_trade_usd = 25.0
    cfg.maker.max_quotes_per_event = 2
    universe = build_universe([ev], build_strategies(cfg), cfg.universe)
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    await maker.step()
    assert len(maker.active) == 2  # Both NO(A) @ 0.49 and NO(B) @ 0.49 resting simultaneously!

    # Simulate simultaneous burst hitting BOTH resting NO bids @ 0.49 (YES trades at 0.51 on both!)
    for q in maker.active.values():
        gw._orders[q.order_id].placed_at -= 1.0
    gw.on_market_trade(ms[0].yes_token, 0.51, 100.0)
    gw.on_market_trade(ms[1].yes_token, 0.51, 100.0)

    await maker.step()
    assert len(maker.active) == 0
    assert len(gw.converted) == 1
    # Both legs filled at 0.49 maker (0.49 + 0.49 = 0.98) -> converted for $1.00 => +2c/sh (DOUBLE profit, 0 taker fee)!
    conv_sh = gw.converted[0][2]
    assert risk.state.realized_total == pytest.approx(conv_sh * (1.00 - 0.98))


async def test_pooled_hedge_buffer_fits_6_quotes_on_70_bankroll_and_repegs_when_outbid(store, tmp_path):
    """On a $70 bankroll ($25 max_trade_usd), old full reservation capped out at 2 quotes (~$50 reserved).
    Pooled hedge reservation (100% of all 6 lead_costs + top 2 full hedge_costs) fits 6 simultaneous
    convert quotes safely while keeping 100% cash solvency, and immediately re-pegs when outbid!"""
    events = []
    first_lead_yes = None
    for e_idx in range(6):
        ms = [
            make_market(f"E{e_idx} A?", group="A", neg_risk=True, index=0),
            make_market(f"E{e_idx} B?", group="B", neg_risk=True, index=1),
        ]
        for m in ms:
            m.volume_24h = 3000.0
        ev = make_event(ms, neg_risk=True, title=f"Liquid Event {e_idx}")
        events.append(ev)
        # Lead A: YES [0.78 .. 0.82] => NO [0.18 .. 0.22] (lead_price = 0.20 -> cheap ~$5.1 lead lock!).
        # Hedge B: YES [0.22 .. 0.24] => NO ask = 0.78 (0.20 + 0.78 = 0.98 < 1.00, +2c/sh!).
        store.put(book(ms[0].yes_token, bids=[(0.78, 200)], asks=[(0.82, 200)]))
        store.put(book(ms[1].yes_token, bids=[(0.22, 200)], asks=[(0.24, 200)]))
        if e_idx == 0:
            first_lead_yes = ms[0].yes_token

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.bankroll_usd = 70.0
    cfg.risk.max_trade_usd = 25.0
    cfg.maker.max_open_quotes = 6
    cfg.maker.max_quotes_per_event = 1
    cfg.maker.pool_hedge_slots = 2
    cfg.maker.repeg_after_s = 2.0

    universe = build_universe(events, build_strategies(cfg), cfg.universe)
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    maker.set_targets(universe.targets)

    await maker.step()
    assert len(maker.active) == 6  # All 6 events quoted simultaneously on $70 bankroll (Quote #6 auto-sized to remaining pool)!
    assert risk.reserved_usd <= 70.0 + 1e-6

    ev0_q = maker.active[events[0].id]
    assert ev0_q.plan.lead_price == pytest.approx(0.20)
    ev0_q.placed_at -= 4.0
    # Competitor steps NO bid to 0.21 (YES ask = 0.79) while hedge B improves YES bid to 0.24 (NO ask = 0.76)
    store.put(book(first_lead_yes, bids=[(0.77, 200)], asks=[(0.79, 200)]))  # NO book is now [0.21 .. 0.23] > our 0.20!
    store.put(book(events[0].markets[1].yes_token, bids=[(0.24, 200)], asks=[(0.26, 200)]))  # NO ask = 0.76

    await maker.step()
    assert maker.quotes_repegged == 1
    # Immediately re-pegged in the same step() at the new optimal price 0.22 (+1c above 0.21)!
    assert maker.active[events[0].id].plan.lead_price == pytest.approx(0.22)
