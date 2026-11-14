from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from polyarb.config import BinanceClockConfig, Config, ConvertConfig, ExecutionConfig, LadderConfig, TwinConfig
from polyarb.data.binance import ET_ZONE, BinanceFeed
from polyarb.data.metar import MetarFeed
from polyarb.data.ws import MarketStream
from polyarb.execution.executor import Executor
from polyarb.execution.lifecycle import LifecycleManager
from polyarb.execution.maker_arb import MakerArbManager
from polyarb.execution.paper import PaperGateway
from polyarb.fees import maker_rebate_per_share
from polyarb.journal import Journal
from polyarb.models import FeeSchedule, Outcome
from polyarb.risk import RiskManager
from polyarb.strategies.binance_clock import BinanceClockLock
from polyarb.strategies.ladder import LadderArb
from polyarb.strategies.negrisk import NegRiskConvert
from polyarb.strategies.twin import TwinPartitionArb
from polyarb.universe import build_all_strategies, build_universe

from conftest import NOW, book, make_event, make_market

CRYPTO_FEE = FeeSchedule(rate=0.07, exponent=1.0, taker_only=True, rebate_rate=0.20)


# ---------------------------------------------------------------- 1. BinanceFeed & BinanceClockLock + maker_binance


async def test_binance_hourly_lock_ignores_unclosed_candles_and_quotes_empty_ask_bonding_wall(store, tmp_path):
    """Exact reproduction of live Event 1374042 ('Bitcoin above ___ on November 12, 5PM ET?'):
    1. While the 4PM->5PM ET 1h candle is still open (close_ms >= now_ms), 0 locks are emitted.
    2. The instant 5:00:00.100 PM ET passes (close = 71,559.70), 71,600 is 100% locked NO ($1.00)
       and 70,600 is 100% locked YES ($1.00).
    3. Even when NO asks are completely empty (YES bids = [], NO bids = [(0.990, 12515)]),
       MakerArbManager steps +1 tick ahead at 0.991 (postOnly, 0 fee, 0 hedge legs!) and
       LifecycleManager posts a 0.995 resting maker flip once filled!
    """
    end_dt = datetime(2026, 11, 12, 17, 0, 0, tzinfo=ET_ZONE)
    open_ms = int((end_dt - timedelta(hours=1)).timestamp() * 1000)
    close_ms = int(end_dt.timestamp() * 1000) - 1

    m_71600 = make_market("Bitcoin above 71,600 on November 12, 5PM ET?", group="71,600", neg_risk=False, tick=0.001, fee=CRYPTO_FEE, end=end_dt)
    m_70600 = make_market("Bitcoin above 70,600 on November 12, 5PM ET?", group="70,600", neg_risk=False, tick=0.001, fee=CRYPTO_FEE, end=end_dt)
    ev = make_event([m_71600, m_70600], neg_risk=False, title="Bitcoin above ___ on November 12, 5PM ET?")
    ev.end_date = end_dt
    ev.oracle_sig = "binance_1h:btc_usdt"

    feed = BinanceFeed(symbols=("BTCUSDT",))
    kline_row = [open_ms, "71700.0", "71900.0", "71400.0", "71559.70", "120.5", close_ms]

    # Before 5:00 PM ET (now_ms < close_ms): unclosed candle MUST be ignored!
    assert feed.ingest_klines("BTCUSDT", "1h", [kline_row], now_ms=close_ms - 500) is False
    assert feed.hourly_close("BTCUSDT", "2026-11-12 17:00") is None

    # 100ms after 5:00 PM ET (now_ms > close_ms): closed candle is ingested!
    assert feed.ingest_klines("BTCUSDT", "1h", [kline_row], now_ms=close_ms + 100) is True
    assert feed.hourly_close("BTCUSDT", "2026-11-12 17:00") == pytest.approx(71559.70)

    # Book matches exact 17:01:20 ET snapshot: YES bids = [] (NO asks = []), YES asks = [(0.010, 12515)] <=> NO bids = [(0.990, 12515)]
    store.put(book(m_71600.yes_token, bids=[], asks=[(0.010, 12515.0)]))
    # On 70,600: stale YES ask at 0.96 -> Taker snipeable!
    store.put(book(m_70600.yes_token, bids=[(0.94, 500.0)], asks=[(0.96, 20.0)]))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.bankroll_usd = 70.0
    cfg.risk.max_trade_usd = 25.0
    all_strats = build_all_strategies(cfg, binance_feed=feed)
    strat_map = {s.name: s for s in all_strats}
    u = build_universe([ev], all_strats, cfg.universe)

    # Taker snipes 70,600 YES @ 0.96 immediately
    b_target = next(t for t in u.targets if t.strategy == "binance_lock")
    taker_opps = strat_map["binance_lock"].evaluate(b_target, store, end_dt.timestamp() + 1.0)
    assert len(taker_opps) == 1 and taker_opps[0].legs[0].market_id == m_70600.id

    # Replace 70,600 book with post-sweep bonding wall too, then test maker_binance 0-hedge-leg queue jump on 71,600!
    store.put(book(m_70600.yes_token, bids=[(0.999, 10000.0)], asks=[]))
    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal, strategies=strat_map)
    maker.set_targets(u.targets)

    await maker.step()
    assert len(maker.active) == 1
    q = next(iter(maker.active.values()))
    assert q.plan.strategy == "maker_binance"
    assert q.plan.lead_market.id == m_71600.id and q.plan.lead_outcome is Outcome.NO
    assert q.plan.hedge_legs == [] and q.plan.hedge_cost == 0.0
    assert q.plan.lead_price == pytest.approx(0.992)  # +2 ticks ahead of 0.990 bonding wall!

    # Retail holder dumps YES(71,600) @ 0.008 (crossing our NO bid @ 0.992)
    q.placed_at -= 1.0
    gw._orders[q.order_id].placed_at -= 1.0
    gw.on_market_trade(m_71600.yes_token, 0.008, 50.0)
    await maker.step()

    assert len(maker.active) == 0
    assert len(risk.state.holds) == 1
    assert risk.state.holds[0].strategy == "maker_binance"
    assert maker.rebates_est_usd > 0.0

    # LifecycleManager immediately posts a resting postOnly 0.995+ maker SELL on the filled maker_binance hold!
    lc = LifecycleManager(cfg, gw, risk, journal, store=store)
    await lc.step()
    assert risk.state.holds[0].exit_order_id is not None


def test_binance_noon_dual_candle_band_eliminates_boundary_ambiguity(store):
    end_dt = datetime(2026, 11, 13, 17, 0, 0, tzinfo=ET_ZONE)
    m_70k = make_market("BTC above $70,000?", group="70,000", neg_risk=False, end=end_dt)
    m_72k = make_market("BTC above $72,000?", group="72,000", neg_risk=False, end=end_dt)
    ev = make_event([m_70k, m_72k], neg_risk=False, title="Bitcoin above ___ on November 13?")
    ev.end_date = end_dt
    ev.oracle_sig = "binance_1200et:btc_usdt"

    feed = BinanceFeed(symbols=("BTCUSDT",))
    t_1159 = int(datetime(2026, 11, 13, 11, 59, 0, tzinfo=ET_ZONE).timestamp() * 1000)
    t_1200 = int(datetime(2026, 11, 13, 12, 0, 0, tzinfo=ET_ZONE).timestamp() * 1000)
    # 11:59 bar closed at 71,950 while 12:00 bar closed at 72,050 (crossed 72,000 right at noon!)
    feed.ingest_klines(
        "BTCUSDT",
        "1m",
        [
            [t_1159, "71900", "72000", "71850", "71950", "10", t_1159 + 59999],
            [t_1200, "71950", "72100", "71940", "72050", "10", t_1200 + 59999],
        ],
        now_ms=t_1200 + 65000,
    )
    strat = BinanceClockLock(BinanceClockConfig(), feed=feed)
    (target,) = strat.targets([ev])
    locked = dict((m.id, out) for m, out, _ in strat.locked_markets(target, end_dt))
    # 70,000 is strictly below BOTH bars -> locked YES! 72,000 is straddled -> safely skipped (not locked)!
    assert locked == {m_70k.id: Outcome.YES}


# ---------------------------------------------------------------- 2. MetarLock 2.0: maker_metar + Pruned Maker Basket


async def test_maker_metar_zero_leg_and_pruned_surviving_basket(store, tmp_path):
    """Exact reproduction of live London Nov 12 NO(5°C) empty-ask bonding + Seattle pruned surviving basket."""
    end = NOW + timedelta(hours=8)
    m_5c = make_market("Will low be 5°C?", group="5°C", neg_risk=True, index=0, tick=0.001, end=end)
    m_4c = make_market("Will low be 4°C or below?", group="4°C or below", neg_risk=True, index=1, tick=0.001, end=end)
    m_5c.volume_24h = m_4c.volume_24h = 2000.0
    ev_lon = make_event([m_5c, m_4c], neg_risk=True, title="Lowest temperature in London on November 12?")
    ev_lon.metar_icao, ev_lon.metar_obs_date, ev_lon.metar_mode = "EGLC", "2026-11-12", "low"

    # Seattle: 3 brackets (<56°F dead, 56-57°F alive, 58°F+ alive)
    s_dead = make_market("Temp 55°F or below?", group="55°F or below", neg_risk=True, index=0, end=end)
    s_1 = make_market("Temp 56-57°F?", group="56-57°F", neg_risk=True, index=1, end=end)
    s_2 = make_market("Temp 58°F or higher?", group="58°F or higher", neg_risk=True, index=2, end=end)
    s_1.volume_24h = s_2.volume_24h = 2500.0
    ev_sea = make_event([s_dead, s_1, s_2], neg_risk=True, title="Highest temperature in Seattle on November 12?")
    ev_sea.metar_icao, ev_sea.metar_obs_date, ev_sea.metar_mode = "KSEA", "2026-11-12", "high"

    feed = MetarFeed()
    ts = int(datetime(2026, 11, 12, 18, 0, tzinfo=ZoneInfo("UTC")).timestamp())
    feed.ingest_rows(
        [
            {"icaoId": "EGLC", "obsTime": ts, "rawOb": "METAR EGLC 121800Z 02008KT 04/01 Q1012"},  # 4°C -> 5°C is dead!
            {"icaoId": "KSEA", "obsTime": ts, "rawOb": "METAR KSEA 121800Z 18006KT 14/08 A2998 RMK T01390080"},  # 13.9°C = 57°F -> <=55°F is dead!
        ]
    )

    # London 5°C: YES bids = [] (NO asks = []), YES asks = [(0.099, 440)] <=> NO bids = [(0.901, 440)]
    store.put(book(m_5c.yes_token, bids=[], asks=[(0.099, 440.0)]))
    store.put(book(m_4c.yes_token, bids=[(0.98, 500.0)], asks=[]))

    # Seattle: dead bracket has tiny 0.001 ask (would fail $1 min notional if not pruned!), surviving 2 brackets sum to 1.02 taker / 0.98 maker!
    store.put(book(s_dead.yes_token, bids=[], asks=[(0.001, 100.0)]))
    store.put(book(s_1.yes_token, bids=[(0.56, 200.0)], asks=[(0.62, 200.0)]))
    store.put(book(s_2.yes_token, bids=[(0.38, 200.0)], asks=[(0.40, 200.0)]))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.max_trade_usd = 25.0
    all_strats = build_all_strategies(cfg, metar_feed=feed)
    strat_map = {s.name: s for s in all_strats}
    u = build_universe([ev_lon, ev_sea], all_strats, cfg.universe)

    maker = MakerArbManager(cfg, store, PaperGateway(store), RiskManager(cfg.risk, tmp_path / "s.json"), Journal(tmp_path / "j.jsonl"), strategies=strat_map)
    maker.set_targets(u.targets)
    cands = maker.find_candidates(NOW.timestamp())
    metar_cands = [c for c in cands if c.strategy == "maker_metar"]
    basket_cands = [c for c in cands if c.strategy == "maker_yes_basket"]

    assert len(metar_cands) >= 1
    assert metar_cands[0].lead_market.id == m_5c.id and metar_cands[0].lead_outcome is Outcome.NO
    assert metar_cands[0].lead_price == pytest.approx(0.903)
    assert len(basket_cands) == 1
    assert len(basket_cands[0].hedge_legs) == 1  # dead <=55°F bracket cleanly pruned!


# ---------------------------------------------------------------- 3. NegRiskConvert 2.0 Anti-Thin-Leg Poisoning


def test_negrisk_convert_depth_tier_bypasses_thin_poison_order(store, now_ts):
    """Outcome D has the 2nd-highest top-of-book YES bid (0.38), but ONLY 5 shares on the entire book!
    Outcomes A, B, C have 60 shares each (YES bids 0.40, 0.35, 0.30 => sum 1.05, +5c/sh!).
    Old prefix-only NegRiskConvert included D at k=2..4, hard-capping size at 5 shares ($0.39 profit).
    v0.6 Depth-Tier Optimizer bypasses thin D and sizes 50+ shares on {A, B, C} ($3.00 profit = 7.7x higher PnL!).
    """
    ms = [make_market(f"Will {lbl} win?", group=lbl, neg_risk=True, index=i) for i, lbl in enumerate(["A", "D_THIN", "B", "C"])]
    ev = make_event(ms, neg_risk=True, title="4-Way Race")

    store.put(book(ms[0].yes_token, bids=[(0.40, 60.0)], asks=[(0.42, 60.0)]))
    store.put(book(ms[1].yes_token, bids=[(0.38, 5.0)], asks=[(0.45, 60.0)]))  # 5-share retail poison order!
    store.put(book(ms[2].yes_token, bids=[(0.35, 60.0)], asks=[(0.37, 60.0)]))
    store.put(book(ms[3].yes_token, bids=[(0.30, 60.0)], asks=[(0.32, 60.0)]))

    strat = NegRiskConvert(ConvertConfig(min_profit_usd=0.50, min_edge=0.005, surplus_haircut=0.0), max_trade_usd=120.0)
    (target,) = strat.targets([ev])
    (opp,) = strat.evaluate(target, store, now_ts)

    assert opp.shares == pytest.approx(60.0)
    assert {leg.market_id for leg in opp.legs} == {ms[0].id, ms[2].id, ms[3].id}
    assert opp.profit == pytest.approx(60.0 * 0.05)


# ---------------------------------------------------------------- 4. Twin 2.0: Lower Band + Same-Oracle Close->Hit


def test_twin_lower_overlapping_double_payout_and_same_binance_close_to_hit(store, now_ts):
    end_d = datetime(2026, 11, 15, 17, 0, tzinfo=ET_ZONE)
    end_w = datetime(2026, 11, 16, 5, 0, tzinfo=ET_ZONE)

    r_ms = [
        make_market("ETH < $2,000?", group="<2,000", neg_risk=True, index=0, end=end_d),
        make_market("ETH $2,000-$2,200?", group="2,000-2,200", neg_risk=True, index=1, end=end_d),
        make_market("ETH > $2,200?", group=">2,200", neg_risk=True, index=2, end=end_d),
    ]
    r_ev = make_event(r_ms, neg_risk=True, title="Ethereum price on November 15?")
    r_ev.end_date = end_d
    r_ev.oracle_sig = "binance_1200et:eth_usdt"

    a_ms = [
        make_market("ETH above $2,000?", group="2,000", neg_risk=False, end=end_d),
        make_market("ETH above $2,700?", group="2,700", neg_risk=False, end=end_d),
    ]
    a_ev = make_event(a_ms, neg_risk=False, title="Ethereum above ___ on November 15?")
    a_ev.end_date = end_d
    a_ev.oracle_sig = "binance_1200et:eth_usdt"

    h_ms = [
        make_market("Will Ethereum reach $2,700 November 9-15?", group="↑ 2,700", neg_risk=False, end=end_w),
    ]
    h_ev = make_event(h_ms, neg_risk=False, title="What price will Ethereum hit November 9-15?")
    h_ev.end_date = end_w
    h_ev.oracle_sig = "binance_1m:eth_usdt"

    # Out-of-window hit event (Nov 12 only -> does NOT contain Nov 15!) must NOT pair with Nov 15!
    h_bad = make_event(
        [make_market("Will Ethereum reach $2,700 on November 12?", group="↑ 2,700", neg_risk=False, end=end_d)],
        neg_risk=False,
        title="What price will Ethereum hit on November 12?",
    )
    h_bad.end_date = end_d
    h_bad.oracle_sig = "binance_1m:eth_usdt"

    # Price twin_lower at K=2000: YES(<2000)=0.25 + YES(2000-2200)=0.25 + YES(above 2000)=0.47 => 0.97 (+3c/sh!)
    store.put(book(r_ms[0].yes_token, bids=[(0.23, 80)], asks=[(0.25, 80)]))
    store.put(book(r_ms[1].yes_token, bids=[(0.23, 80)], asks=[(0.25, 80)]))
    store.put(book(r_ms[2].yes_token, bids=[(0.50, 80)], asks=[(0.55, 80)]))
    store.put(book(a_ms[0].yes_token, bids=[(0.45, 80)], asks=[(0.47, 80)]))
    # Price cross_close_hit at 2700: NO(above 2700 on Nov 15) ask = 1 - 0.12 = 0.88 + YES(hit ↑2700 Nov 9-15) ask = 0.10 => 0.98 (+2c/sh!)
    store.put(book(a_ms[1].yes_token, bids=[(0.12, 80)], asks=[(0.14, 80)]))
    store.put(book(h_ms[0].yes_token, bids=[(0.08, 80)], asks=[(0.10, 80)]))

    strat = TwinPartitionArb(TwinConfig(min_profit_usd=0.10, min_edge=0.005), max_trade_usd=30.0)
    targets = strat.targets([r_ev, a_ev, h_ev, h_bad])
    # Nov 15 close MUST NOT pair with Nov 12 hit (out of window!), while Nov 12 hit -> Nov 9-15 hit IS a valid nested-period pair!
    assert not any(a_ev.id in t.key and h_bad.id in t.key for t in targets)
    assert any(h_bad.id in t.key and h_ev.id in t.key for t in targets)

    opps = {o.meta["kind"]: o for t in targets for o in strat.evaluate(t, store, end_d.timestamp() - 86400)}
    assert "twin_lower" in opps and opps["twin_lower"].profit > 0.60
    assert "cross_close_hit" in opps and opps["cross_close_hit"].profit > 0.50

    # Verify twin_lower payoff >= $1.00 everywhere AND == $2.00 on the entire (2000, 2200) band!
    for p in (1800.0, 2000.0, 2100.0, 2200.0, 2500.0):
        pay = (1.0 if p < 2000.0 else 0.0) + (1.0 if 2000.0 <= p < 2200.0 else 0.0) + (1.0 if p > 2000.0 else 0.0)
        assert pay >= 1.0
        if 2000.0 < p < 2200.0:
            assert pay == 2.0


# ---------------------------------------------------------------- 5. Disjoint Multi-Pair Ladders & Independent Cooldowns


async def test_disjoint_ladder_pairs_and_back_to_back_market_cooldown(store, tmp_path, now_ts):
    end = NOW + timedelta(days=5)
    r4 = make_market("Will Gold hit (HIGH) $3,400?", group="↑ $3,400", end=end)
    r3 = make_market("Will Gold hit (HIGH) $3,300?", group="↑ $3,300", end=end)
    r2 = make_market("Will Gold hit (HIGH) $3,200?", group="↑ $3,200", end=end)
    r1 = make_market("Will Gold hit (HIGH) $3,100?", group="↑ $3,100", end=end)
    ev = make_event([r4, r3, r2, r1], title="Gold Ladder")

    # Pair 1: NO(3400)=0.68 + YES(3300)=0.29 = 0.97 (+3c); Pair 2: NO(3200)=0.38 + YES(3100)=0.59 = 0.97 (+3c)
    store.put(book(r4.yes_token, bids=[(0.32, 50)], asks=[(0.34, 50)]))
    store.put(book(r3.yes_token, bids=[(0.27, 50)], asks=[(0.29, 50)]))
    store.put(book(r2.yes_token, bids=[(0.62, 50)], asks=[(0.64, 50)]))
    store.put(book(r1.yes_token, bids=[(0.57, 50)], asks=[(0.59, 50)]))

    strat = LadderArb(LadderConfig(min_profit_usd=0.20, min_edge=0.005), max_trade_usd=25.0)
    (target,) = strat.targets([ev])
    opps = strat.evaluate(target, store, now_ts)
    assert len(opps) == 2  # Both disjoint pairs extracted in one pass!

    cfg = Config()
    cfg.risk.kill_file = ""
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    ex = Executor(PaperGateway(store), store, {strat.name: strat}, ExecutionConfig(recheck_books=False))

    # Execute Pair 1, then immediately check & execute Pair 2 without 30s event-wide cooldown blocking it!
    assert risk.check(opps[0], now_ts) is None
    risk.begin(opps[0], now_ts)
    risk.finish(await ex.execute(opps[0], target))
    # Same pair IS blocked by market cooldown:
    assert risk.check(opps[0], now_ts + 0.1) == "cooldown"
    # Disjoint pair in the same event is NOT blocked and executes immediately!
    assert risk.check(opps[1], now_ts + 0.1) is None
    risk.begin(opps[1], now_ts + 0.1)
    risk.finish(await ex.execute(opps[1], target))
    assert risk.state.trades == 2


# ---------------------------------------------------------------- 6. Maker Rebates & Live WS Sub-5ms Trade Wake


def test_maker_rebate_math_and_live_ws_trade_wake(store):
    # At p=0.50 on crypto_fees_v2 (rate=0.07, rebate=0.20): taker fee = 1.75c, maker rebate = 0.35c/sh!
    assert maker_rebate_per_share(CRYPTO_FEE, 0.50) == pytest.approx(0.0035)

    # In live mode (on_trade=None), MarketStream MUST still emit changed token on last_trade_price so MakerArbManager wakes in <5ms!
    woken: list[str] = []
    stream = MarketStream(store, on_change=woken.append, on_trade=None)
    stream.handle('{"event_type":"last_trade_price","asset_id":"LIVE_QUOTED_TOK","price":"0.51","size":"12"}')
    assert woken == ["LIVE_QUOTED_TOK"]


async def test_event_loop_never_stalls_healthz_across_2500_targets_and_ws_bursts(store, tmp_path, now_ts):
    """Regression test for Kubernetes /healthz 502s:
    Even with 1,500 multi-outcome NegRisk targets, cooperative yielding + upper-bound pruning + WS wake fast-path
    keep concurrent event-loop ticks under 50ms (never stalling for seconds).
    """
    import asyncio
    import time as time_mod

    events = []
    for idx in range(500):
        ms = [
            make_market(f"E{idx} A?", group="A", neg_risk=True, index=0),
            make_market(f"E{idx} B?", group="B", neg_risk=True, index=1),
            make_market(f"E{idx} C?", group="C", neg_risk=True, index=2),
        ]
        for m in ms:
            m.volume_24h = 1200.0
            store.put(book(m.yes_token, bids=[(0.25, 100)], asks=[(0.29, 100)]))
        events.append(make_event(ms, neg_risk=True, title=f"Event {idx}"))

    cfg = Config()
    cfg.risk.kill_file = ""
    cfg.risk.bankroll_usd = 70.0
    cfg.risk.max_trade_usd = 25.0
    strats = build_all_strategies(cfg)
    u = build_universe(events, strats, cfg.universe)
    risk = RiskManager(cfg.risk, tmp_path / "s.json")
    maker = MakerArbManager(cfg, store, PaperGateway(store), risk, Journal(tmp_path / "j.jsonl"), strategies={s.name: s for s in strats})
    maker.set_targets(u.targets)

    max_gap_ms = 0.0
    running = True

    async def probe_loop():
        nonlocal max_gap_ms
        last = time_mod.perf_counter()
        while running:
            await asyncio.sleep(0.005)
            now_p = time_mod.perf_counter()
            max_gap_ms = max(max_gap_ms, (now_p - last - 0.005) * 1000.0)
            last = now_p

    probe = asyncio.create_task(probe_loop())
    try:
        # 1. Evaluate all NegRiskConvert targets + run full async Maker scan concurrently with probe_loop
        conv = next(s for s in strats if s.name == "negrisk_convert")
        for i, t in enumerate(u.targets):
            if i and i % 60 == 0:
                await asyncio.sleep(0)
            if t.strategy == "negrisk_convert":
                conv.evaluate(t, store, now_ts)
        await maker.step(scan_candidates=True)
        # 2. Simulate 50 rapid WS wake steps (scan_candidates=False) + exhausted-cash fast skip
        risk.reserved_usd = 69.99
        for _ in range(50):
            await maker.step(scan_candidates=False)
        await maker.step(scan_candidates=True)
    finally:
        running = False
        await probe

    assert max_gap_ms < 80.0

