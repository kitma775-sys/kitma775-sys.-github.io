import json
import time
from pathlib import Path

import httpx
import pytest

from polyarb.config import Config, load_config
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import parse_event, parse_fee, parse_market
from polyarb.data.store import BookStore
from polyarb.data.ws import MarketStream
from polyarb.engine import Engine, scan_once
from polyarb.execution.settlement import (
    NEG_RISK_ADAPTER,
    ChainReader,
    encode_convert_positions,
    expected_convert_proceeds,
    index_set,
    to_base_units,
)
from polyarb.journal import Journal
from polyarb.models import FEE_FREE
from polyarb.universe import build_strategies, build_universe

from conftest import MARKET_ID, book

ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- settlement


def test_convert_calldata_matches_abi_encoding():
    eth_abi = pytest.importorskip("eth_abi")
    eth_utils = pytest.importorskip("eth_utils")
    data = encode_convert_positions(MARKET_ID, 0b1011, 2_500_000)
    selector = eth_utils.keccak(text="convertPositions(bytes32,uint256,uint256)")[:4]
    expected = selector + eth_abi.encode(["bytes32", "uint256", "uint256"], [bytes.fromhex(MARKET_ID[2:]), 0b1011, 2_500_000])
    assert data == "0x" + expected.hex()


def test_convert_validation_and_proceeds():
    with pytest.raises(ValueError):
        encode_convert_positions(MARKET_ID, 0b100, 1)  # a single NO converts to nothing
    with pytest.raises(ValueError):
        encode_convert_positions(MARKET_ID, 0b11, 0)
    with pytest.raises(ValueError):
        encode_convert_positions("0x1234", 0b11, 1)
    with pytest.raises(ValueError):
        index_set([256])
    assert index_set([0, 2, 5]) == 0b100101
    assert to_base_units(1.234567) == 1_234_567
    assert expected_convert_proceeds(0b111, 1_000_000) == 2_000_000
    assert expected_convert_proceeds(0b111, 1_000_000, fee_bips=100) == 1_980_000


async def test_chain_reader_decodes_eth_call():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append(body["params"][0])
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": "0x" + format(25, "064x")})

    reader = ChainReader(http=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await reader.fee_bips(MARKET_ID) == 25
    assert seen[0]["to"] == NEG_RISK_ADAPTER and seen[0]["data"].startswith("0x2582cb5e")

    failing = ChainReader(http=httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": {"message": "revert"}}))))
    with pytest.raises(RuntimeError):
        await failing.question_count(MARKET_ID)


# ---------------------------------------------------------------- book store + websocket


def test_store_hides_stale_and_resolved_books():
    store = BookStore(max_age_s=5)
    b = book("Y1", bids=[(0.4, 10)])
    store.put(b)
    assert store["Y1"] is b
    b.received_at = time.time() - 10
    assert "Y1" not in store and store.get("Y1") is None and store.raw("Y1") is b
    store.touch("Y1")
    assert store["Y1"] is b
    store.resolved.add("Y1")
    assert store.get("Y1") is None
    assert store.stats() == {"books": 1, "fresh": 1, "resolved": 1}


def test_ws_handle_applies_book_deltas_and_resolution():
    store = BookStore()
    changed: list[str] = []
    stream = MarketStream(store, on_change=changed.append)
    snapshot = {"event_type": "book", "asset_id": "Y1", "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.45", "size": "7"}], "timestamp": "1782753357257"}
    assert stream.handle(json.dumps([snapshot])) == {"Y1"}
    delta = {"event_type": "price_change", "price_changes": [
        {"asset_id": "Y1", "side": "BUY", "price": "0.41", "size": "5"},
        {"asset_id": "Y1", "side": "SELL", "price": "0.45", "size": "0"},
        {"asset_id": "UNKNOWN", "side": "BUY", "price": "0.1", "size": "1"},
    ]}
    assert stream.handle(json.dumps(delta)) == {"Y1"}
    assert store["Y1"].best_bid == 0.41 and store["Y1"].best_ask is None
    stream.handle(json.dumps({"event_type": "tick_size_change", "asset_id": "Y1", "new_tick_size": "0.001"}))
    assert store.raw("Y1").tick_size == 0.001
    stream.handle(json.dumps({"event_type": "market_resolved", "assets_ids": ["Y1"]}))
    assert store.get("Y1") is None
    assert stream.handle("not json") == set() and stream.handle("PONG") == set()
    assert changed == ["Y1", "Y1"]


def test_ws_deltas_below_the_top_levels_do_not_trigger_evaluation():
    store = BookStore()
    changed: list[str] = []
    stream = MarketStream(store, on_change=changed.append)
    store.put(book("Y1", bids=[(0.40, 1), (0.39, 1), (0.38, 1), (0.37, 1)]))
    deep = {"event_type": "price_change", "price_changes": [{"asset_id": "Y1", "side": "BUY", "price": "0.36", "size": "5"}]}
    assert stream.handle(json.dumps(deep)) == set() and store.raw("Y1").bids[-1] == (0.36, 5.0)
    top = {"event_type": "price_change", "price_changes": [{"asset_id": "Y1", "side": "BUY", "price": "0.38", "size": "0"}]}
    assert stream.handle(json.dumps(top)) == {"Y1"} and changed == ["Y1"]
    assert [lvl.price for lvl in store.raw("Y1").bids] == [0.40, 0.39, 0.37, 0.36]


# ---------------------------------------------------------------- config + journal


def test_default_yaml_matches_dataclass_defaults():
    assert load_config(ROOT / "config" / "default.yaml") == Config()


def test_config_overrides_and_validation(tmp_path):
    cfg = load_config(None, {"risk.max_trade_usd": 50, "ladder.directions": ["up"]})
    assert cfg.risk.max_trade_usd == 50.0 and isinstance(cfg.risk.max_trade_usd, float)
    assert cfg.ladder.directions == ["up"]
    with pytest.raises(ValueError, match="unknown keys"):
        load_config(None, {"risk.max_trade": 1})
    with pytest.raises(ValueError, match="risk.max_trade_usd"):
        load_config(None, {"risk.max_trade_usd": "lots"})
    bad = tmp_path / "bad.yaml"
    bad.write_text("convert: 5\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        load_config(bad)


def test_journal_roundtrip(tmp_path):
    j = Journal(tmp_path / "j.jsonl")
    j.write("opportunity", {"profit": 1.5})
    j.write("execution", {"pnl": 1.2})
    assert [r["kind"] for r in j.read()] == ["opportunity", "execution"]
    assert j.read("execution")[0]["pnl"] == 1.2


# ---------------------------------------------------------------- gamma parsing


def raw_market(i, **kw):
    base = {
        "id": str(i), "conditionId": f"0xc{i}", "question": f"Will {i} win?", "slug": f"m{i}",
        "clobTokenIds": json.dumps([f"Y{i}", f"N{i}"]), "outcomes": '["Yes", "No"]', "groupItemTitle": f"Cand {i} ",
        "active": True, "closed": False, "acceptingOrders": True, "enableOrderBook": True,
        "orderPriceMinTickSize": 0.001, "orderMinSize": 5, "endDate": "2026-11-03T12:00:00Z",
        "negRisk": True, "negRiskMarketID": MARKET_ID, "questionID": "0x" + format(int(MARKET_ID, 16) | i, "064x"),
        "feesEnabled": True, "feeSchedule": {"rate": 0.04, "exponent": 1, "takerOnly": True, "rebateRate": 0.25},
    }
    base.update(kw)
    return base


def test_parse_market_fields():
    m = parse_market(raw_market(3, secondsDelay=3))
    assert (m.yes_token, m.no_token, m.group_title) == ("Y3", "N3", "Cand 3")
    assert m.neg_risk and m.neg_risk_index == 3 and m.accepting_orders
    assert m.fee.rate == 0.04 and m.fee.rebate_rate == 0.25
    assert m.tick_size == 0.001 and m.seconds_delay == 3
    assert m.end_date.isoformat() == "2026-11-03T12:00:00+00:00"


def test_parse_market_states_and_fees():
    resolved = parse_market(raw_market(1, closed=True, outcomePrices='["0", "1"]'))
    assert not resolved.accepting_orders and resolved.resolved_outcome == "NO"
    assert not parse_market(raw_market(2, enableOrderBook=False)).accepting_orders
    assert parse_market(raw_market(3, clobTokenIds="[]")) is None
    assert parse_fee({"feesEnabled": False, "feeSchedule": {"rate": 0.07}}) is FEE_FREE
    assert parse_fee({"feeSchedule": {"rate": 0}}) is FEE_FREE
    assert parse_fee({}) is FEE_FREE


def test_parse_event():
    ev = parse_event({
        "id": 9, "slug": "e", "title": "Election", "negRisk": True, "negRiskAugmented": True,
        "negRiskMarketID": MARKET_ID, "endDate": "2026-11-03", "tags": [{"slug": "politics"}],
        "markets": [raw_market(0), raw_market(1, acceptingOrders=False), {"id": "broken"}],
    })
    assert ev.id == "9" and ev.neg_risk and ev.neg_risk_augmented and ev.tags == ("politics",)
    assert len(ev.markets) == 2 and len(ev.tradeable_markets) == 1


# ---------------------------------------------------------------- scan pipeline


async def test_scan_once_finds_convert_opportunity():
    ev = parse_event({
        "id": 1, "title": "Who wins?", "negRisk": True, "negRiskMarketID": MARKET_ID,
        "markets": [raw_market(i, feesEnabled=False, endDate="2026-10-20T00:00:00Z") for i in range(3)],
    })
    books = {"Y0": book("Y0", bids=[(0.40, 100)]), "Y1": book("Y1", bids=[(0.35, 100)]), "Y2": book("Y2", bids=[(0.30, 100)])}

    class Clob:
        async def get_books(self, tokens):
            return {t: books[t] for t in tokens if t in books}

    cfg = Config()
    result = await scan_once(cfg, build_strategies(cfg), gamma=None, clob=Clob(), events=[ev])
    (opp, target) = result.opportunities[0]
    assert opp.strategy == "negrisk_convert" and opp.shares == pytest.approx(51.28)
    assert result.edge_stats()["negrisk_convert"]["best"] == pytest.approx(0.05)


def books_transport(payload: dict, calls: list | None = None) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        asked = [row["token_id"] for row in json.loads(request.content)]
        if calls is not None:
            calls.append(len(asked))
        return httpx.Response(200, json=[payload[t] for t in asked if t in payload])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def levels(*rows):
    return [{"price": str(p), "size": str(s)} for p, s in rows]


async def test_get_books_batches_and_skips_by_hash():
    tokens = [str(i) for i in range(1203)]
    payload = {t: {"asset_id": t, "hash": f"h{t}", "bids": levels((0.5, 1)), "asks": []} for t in tokens}
    calls: list[int] = []
    seen: list[tuple] = []
    clob = ClobClient(http=books_transport(payload, calls), books_rps=1000)
    books = await clob.get_books(tokens + tokens[:5], skip=lambda t, h: seen.append((t, h)) or int(t) % 2 == 0)
    assert sorted(calls) == [203, 500, 500]
    assert sorted(seen) == sorted((t, f"h{t}") for t in tokens)
    assert set(books) == {t for t in tokens if int(t) % 2} and books["7"].hash == "h7"


async def test_sweep_parses_only_books_whose_hash_moved():
    ev = parse_event({
        "id": 1, "title": "Who wins?", "negRisk": True, "negRiskMarketID": MARKET_ID,
        "markets": [raw_market(i, feesEnabled=False) for i in range(3)],
    })
    payload = {f"Y{i}": {"asset_id": f"Y{i}", "hash": "a", "bids": levels((0.30, 10), (0.29, 10), (0.28, 10)), "asks": []}
               for i in range(3)}
    cfg = Config()
    cfg.engine.use_websocket = False
    strategies = build_strategies(cfg)
    store = BookStore(max_age_s=20)
    engine = Engine(cfg, strategies, None, ClobClient(http=books_transport(payload), books_rps=1000), store, None, None, None)
    engine.universe = build_universe([ev], strategies, cfg.universe)
    engine.targets = {t.key: t for t in engine.universe.targets}
    assert await engine.sweep() == 3 and engine.stats["books_parsed"] == 3

    first = store.raw("Y0")
    first.received_at -= 30
    payload["Y1"] = {**payload["Y1"], "hash": "b", "bids": levels((0.31, 10))}
    payload["Y2"] = {**payload["Y2"], "hash": "c", "bids": payload["Y2"]["bids"] + levels((0.10, 5))}
    engine.dirty.clear()
    assert await engine.sweep() == 1  # Y2 only moved below the top three levels
    assert engine.stats["books_parsed"] == 5
    assert store.raw("Y0") is first and store["Y0"] is first  # same hash: kept and re-stamped fresh
    assert store.raw("Y2").bids[-1] == (0.10, 5.0)
    assert engine.dirty == {t.key for t in engine.universe.by_token["Y1"]}
