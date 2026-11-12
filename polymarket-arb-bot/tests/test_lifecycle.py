from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from polyarb.config import Config, ConvertConfig, ExecutionConfig, MetarConfig
from polyarb.data.gamma import parse_event
from polyarb.data.metar import MetarFeed
from polyarb.execution.executor import Executor
from polyarb.execution.lifecycle import LifecycleManager
from polyarb.execution.live import PolymarketGateway, resolve_gateway_api_key
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.models import Settlement
from polyarb.risk import HoldPosition, RiskManager
from polyarb.strategies.ladder import LadderArb
from polyarb.strategies.ladder_parse import detect_ladders
from polyarb.strategies.metar import MetarLock
from polyarb.strategies.negrisk import NegRiskConvert

from conftest import NOW, book, make_event, make_market


def test_somali_pirates_rolling_creation_date_trap_is_rejected(store, now_ts):
    """Exact reproduction of Event 916848: Oct 31 & Nov 30 created Sep 30, Dec 31 created Oct 30."""
    desc = "This market will resolve to Yes if Somali pirates seize control of a ship between market creation and the specified date."
    raw = {
        "id": "916848",
        "title": "Somali Pirates successfully seize a vessel by...?",
        "slug": "somali-pirates",
        "negRisk": False,
        "markets": [
            {
                "id": "5163167",
                "conditionId": "0x" + "aa" * 32,
                "question": "Will Somali pirates successfully seize a vessel by October 31, 2026?",
                "clobTokenIds": ["y_oct", "n_oct"],
                "outcomes": ["Yes", "No"],
                "groupItemTitle": "October 31",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "createdAt": "2026-09-30T20:22:26Z",
                "endDate": "2026-11-01T03:59:00Z",
                "description": desc,
            },
            {
                "id": "5163168",
                "conditionId": "0x" + "bb" * 32,
                "question": "Will Somali pirates successfully seize a vessel by November 30, 2026?",
                "clobTokenIds": ["y_nov", "n_nov"],
                "outcomes": ["Yes", "No"],
                "groupItemTitle": "November 30",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "createdAt": "2026-09-30T20:22:26Z",
                "endDate": "2026-12-01T04:59:00Z",
                "description": desc,
            },
            {
                "id": "6424796",
                "conditionId": "0x" + "cc" * 32,
                "question": "Will Somali pirates successfully seize a vessel by December 31, 2026?",
                "clobTokenIds": ["y_dec", "n_dec"],
                "outcomes": ["Yes", "No"],
                "groupItemTitle": "December 31",
                "active": True,
                "closed": False,
                "acceptingOrders": True,
                "createdAt": "2026-10-30T16:23:51Z",
                "endDate": "2027-01-01T04:59:00Z",
                "description": desc,
            },
        ],
    }
    ev = parse_event(raw)
    assert all(m.creation_scoped for m in ev.markets)

    # Suppose Oct 31 already closed, leaving ONLY Nov 30 (Sep 30) + Dec 31 (Oct 30): ladder must be completely rejected!
    ev.markets[0].accepting_orders = False
    assert detect_ladders(ev) == []

    # Even if Oct 31 is still open, (Nov 30 -> Dec 31) pair is never traded, while valid (Oct 31 -> Nov 30) still works!
    ev.markets[0].accepting_orders = True
    store.put(book("y_oct", bids=[(0.10, 50)], asks=[(0.12, 50)]))
    store.put(book("y_nov", bids=[(0.90, 50)], asks=[(0.92, 50)]))  # NO_nov = 0.10
    store.put(book("y_dec", bids=[(0.83, 50)], asks=[(0.85, 50)]))  # YES_dec = 0.85 -> fake +5c trap with Nov 30!
    strat = LadderArb(Config().ladder)
    (target,) = strat.targets([ev])
    assert strat.evaluate(target, store, datetime(2026, 10, 31, tzinfo=timezone.utc).timestamp()) == []


async def test_instant_flip_and_resting_flip_lifecycle(store, tmp_path, now_ts):
    end = NOW + timedelta(hours=10)
    m = make_market("Will temp be 7°C?", group="7°C", neg_risk=True, index=0, end=end)
    m2 = make_market("Will temp be 8°C or higher?", group="8°C or higher", neg_risk=True, index=1, end=end)
    ev = make_event([m, m2], neg_risk=True, title="Highest temperature in London on November 12?")
    ev.metar_icao, ev.metar_obs_date, ev.metar_mode = "EGLC", "2026-11-12", "high"

    feed = MetarFeed()
    ts = int(datetime(2026, 11, 12, 13, 0, tzinfo=timezone.utc).timestamp())
    feed.ingest_rows([{"icaoId": "EGLC", "obsTime": ts, "rawOb": "METAR EGLC 121300Z 02009KT 08/01 Q1006"}])

    # Case A: Resting NO bid is ALREADY at 0.995 (YES ask = 0.005) while stale YES bid is at 0.08 (NO ask = 0.92)
    # Wait: on a single CLOB book, YES bid=0.08 and YES ask=0.005 would cross; in real life, after we lift YES bid=0.08
    # (buying NO @ 0.92), we place a resting SELL NO @ 0.995, and 5s later bonding bots post BUY NO @ 0.996 (YES ask = 0.004)!
    store.put(book(m.yes_token, bids=[(0.08, 30)], asks=[(0.12, 30)]))
    store.put(book(m2.yes_token, bids=[(0.90, 30)], asks=[(0.95, 30)]))

    strat = MetarLock(MetarConfig(), feed=feed, max_trade_usd=30.0)
    (target,) = strat.targets([ev])
    opps = strat.evaluate(target, store, now_ts)
    opp = next(o for o in opps if o.legs[0].token_id == m.no_token)

    gw = PaperGateway(store)
    ex = Executor(gw, store, {strat.name: strat}, ExecutionConfig(recheck_books=False))
    cfg = Config()
    cfg.risk.kill_file = ""
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")

    rep = await ex.execute(opp, target)
    risk.finish(rep)
    assert len(risk.state.holds) == 1

    # Lifecycle posts resting postOnly SELL NO @ 0.995
    lc = LifecycleManager(cfg, gw, risk, journal)
    await lc.step()
    assert risk.state.holds[0].exit_order_id is not None

    # Bonding bot posts BUY NO @ 0.996 (equivalent to YES ask dropping to 0.004) => resting flip fills!
    store.put(book(m.yes_token, bids=[(0.001, 100)], asks=[(0.004, 100)]))
    await lc.step()
    assert len(risk.state.holds) == 0
    assert lc.flips_completed == 1
    assert risk.state.realized_total == pytest.approx(30 * 0.995 - 30 * 0.92)


async def test_auto_redeem_and_external_hold_reconciliation(store, tmp_path):
    cfg = Config()
    cfg.risk.kill_file = ""
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")

    cond_win = "0x" + "ab" * 32
    risk.state.holds = [
        HoldPosition(
            key="ladder:win",
            event_id="e1",
            strategy="ladder",
            title="Gas Ladder",
            cost=20.46,
            payout=21.0,
            shares=21.0,
            resolves_at=(NOW - timedelta(hours=1)).isoformat(),
            opened_at=NOW.timestamp() - 3600,
            legs=[{"token": "tok_win", "outcome": "NO", "shares": 21.0}, {"token": "tok_lose", "outcome": "YES", "shares": 21.0}],
            condition_ids=[cond_win],
        ),
        HoldPosition(
            key="ladder:external",
            event_id="e2",
            strategy="ladder",
            title="Somali Pirates Sold Externally",
            cost=24.99,
            payout=26.3,
            shares=26.3,
            resolves_at=(NOW + timedelta(days=30)).isoformat(),
            opened_at=NOW.timestamp() - 86400,
            legs=[{"token": "tok_sold_1", "outcome": "YES", "shares": 26.3}, {"token": "tok_sold_2", "outcome": "NO", "shares": 26.3}],
        ),
    ]
    risk.save()

    redeemed_conds = []

    class LiveStub:
        mode = "live"

        async def balance(self):
            return 1.78 + (21.0 if redeemed_conds else 0.0)

        async def position(self, token_id: str):
            # Somali Pirates tokens have 0 balance on-chain because user sold them manually!
            return 0.0

        async def redeem(self, condition_id: str):
            redeemed_conds.append(condition_id)
            return True, "0xredeemtx"

        async def cancel(self, order_id: str):
            return True

    transport = httpx.MockTransport(
        lambda r: httpx.Response(
            200,
            json={
                "data": [
                    {"token_id": "tok_win", "condition_id": cond_win, "redeemable": True, "current_value": 21.0, "current_price": 1.0},
                    {"token_id": "btc_dead", "condition_id": "0x" + "00" * 32, "redeemable": True, "current_value": 0.0, "current_price": 0.0},
                ]
            },
        )
    )
    lc = LifecycleManager(cfg, LiveStub(), risk, journal, wallet_funder="0xWallet", http=httpx.AsyncClient(transport=transport))
    await lc.step()

    assert redeemed_conds == [cond_win]
    assert lc.redemptions == 1
    assert lc.reconciled == 1
    assert risk.state.holds == []
    assert risk.state.realized_total == pytest.approx(21.0 - 20.46)
    assert risk.wallet_balance == pytest.approx(22.78)


def test_wallet_balance_cap_liquid_reserve_and_dynamic_sizing(store, tmp_path, now_ts):
    cfg = Config()
    cfg.risk.bankroll_usd = 70.0
    cfg.risk.max_trade_usd = 25.0
    cfg.risk.liquid_reserve_usd = 20.0
    risk = RiskManager(cfg.risk, tmp_path / "state.json")

    # Formula says $70, but real wallet only has $18.50 => available is $18.50!
    risk.wallet_balance = 18.50
    assert risk.available() == pytest.approx(18.50)
    # Slow resolution hold cannot dip into the $20 liquid reserve => budget is $0!
    assert risk.budget_for(Settlement.RESOLUTION, "e1", fast_turnover=False) == 0.0
    # Instant CONVERT and fast METAR flips CAN use the full $18.50!
    budget = risk.budget_for(Settlement.CONVERT, "e1", fast_turnover=True)
    assert budget == pytest.approx(18.50)

    # NegRiskConvert down-sizes cleanly to $18.50 instead of skipping at $25!
    ms = [make_market(f"Out {i}", group=f"O{i}", neg_risk=True, index=i) for i in range(3)]
    ev = make_event(ms, neg_risk=True)
    for m, bid in zip(ms, (0.40, 0.35, 0.30)):
        store.put(book(m.yes_token, bids=[(bid, 50)]))
    strat = NegRiskConvert(ConvertConfig(min_profit_usd=0.1, min_edge=0.002), max_trade_usd=25.0)
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, now_ts, max_cost=budget)
    assert opp.cost <= 18.50 + 1e-9
    assert risk.check_size(opp) is None


def test_relayer_api_key_and_warm_markets():
    pytest.importorskip("polymarket")
    priv = "0x" + "11" * 32
    rk = resolve_gateway_api_key(priv, {"POLYMARKET_RELAYER_API_KEY": "rk-123"})
    assert rk is not None and rk.key == "rk-123" and rk.address.startswith("0x")

    rk_explicit = resolve_gateway_api_key(
        priv,
        {"POLYMARKET_RELAYER_API_KEY": "rk-123", "POLYMARKET_RELAYER_ADDRESS": "0x" + "33" * 20},
    )
    assert rk_explicit.address == "0x" + "33" * 20

    from polymarket._internal.actions.orders.cache import AsyncOrderMetadataCache

    cache = AsyncOrderMetadataCache()
    gw = PolymarketGateway(SimpleNamespace(_ctx=SimpleNamespace(order_metadata=cache)), chain=SimpleNamespace())
    m = make_market("Q?", neg_risk=True, index=0)
    m.condition_id = "0x" + "22" * 32
    assert gw.warm_markets([m]) == 1
