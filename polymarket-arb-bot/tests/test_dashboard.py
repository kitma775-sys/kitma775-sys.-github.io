import asyncio
import io
import json
import logging
import time
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

import polyarb.app as app_mod
import polyarb.web.server as server_mod
from polyarb.app import is_persistent, live_blockers, readiness, redact_logs
from polyarb.config import Config, env_overrides, load_config, use_data_dir
from polyarb.data.store import BookStore
from polyarb.engine import Engine
from polyarb.execution.base import ExecutionReport
from polyarb.fastjson import loads
from polyarb.journal import Journal
from polyarb.monitor import SERIES_FIELDS, Monitor, RuntimeInfo
from polyarb.risk import RiskManager
from polyarb.universe import build_strategies, build_universe
from polyarb.web.server import MONITOR_KEY, Auth, asset_version, create_app

from conftest import book, make_event, make_market

PASSWORD = "s3cret-pass"


def make_engine(tmp_path: Path, executor=None, yes_asks=(0.30, 0.30, 0.30), yes_bids=(0.29, 0.29, 0.29)):
    """Three-way NegRisk event: YES asks sum to 0.90 (basket +10c), NO asks sum to 2.13 (convert -13c)."""
    groups = ("Alice", "Bob", "Other")
    markets = [make_market(question=f"{g} wins?", group=g, neg_risk=True, index=i) for i, g in enumerate(groups)]
    event = make_event(markets, neg_risk=True, title="Who wins?")
    cfg = Config()
    cfg.engine.use_websocket = False
    cfg.risk.kill_file = str(tmp_path / "KILL")
    strategies = build_strategies(cfg)
    store = BookStore(max_age_s=1e9)
    for m, ask, bid in zip(markets, yes_asks, yes_bids):
        store.put(book(m.yes_token, bids=[(bid, 100)], asks=[(ask, 100)]))
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    engine = Engine(cfg, strategies, None, None, store, executor, risk, journal)
    engine.universe = build_universe([event], strategies, cfg.universe)
    engine.targets = {t.key: t for t in engine.universe.targets}
    return engine, event


def info() -> RuntimeInfo:
    return RuntimeInfo(mode="paper", requested_mode="paper", readiness=[], data_dir="data")


# ---------------------------------------------------------------- config / journal


def test_env_overrides_parse_yaml_scalars_and_apply_to_config():
    env = {
        "POLYARB__RISK__MAX_TRADE_USD": "50",
        "POLYARB__ENGINE__USE_WEBSOCKET": "false",
        "POLYARB__ENGINE__REST_SWEEP_S": "10",
        "POLYARB_MODE": "live",
        "POLYARB__": "ignored",
    }
    overrides = env_overrides(env)
    assert overrides == {"risk.max_trade_usd": 50, "engine.use_websocket": False, "engine.rest_sweep_s": 10}
    cfg = load_config(None, overrides)
    assert cfg.risk.max_trade_usd == 50 and cfg.engine.use_websocket is False and cfg.engine.rest_sweep_s == 10


def test_use_data_dir_keeps_paper_and_live_books_apart(tmp_path):
    cfg = Config()
    use_data_dir(cfg, tmp_path, "live")
    assert cfg.execution.journal_path == str(tmp_path / "live" / "journal.jsonl")
    assert cfg.execution.state_path == str(tmp_path / "live" / "state.json")
    assert cfg.risk.kill_file == str(tmp_path / "KILL")
    disabled = Config()
    disabled.risk.kill_file = ""
    use_data_dir(disabled, tmp_path, "paper")
    assert disabled.risk.kill_file == ""


def test_journal_rotates_by_size_and_survives_a_broken_listener(tmp_path):
    seen = []
    journal = Journal(tmp_path / "j.jsonl", max_bytes=400, backups=2)
    journal.listeners.append(lambda record: seen.append(record["n"]))
    journal.listeners.append(lambda record: 1 / 0)
    for n in range(40):
        journal.write("x", {"n": n, "pad": "y" * 40})
    assert seen == list(range(40))
    names = {p.name for p in tmp_path.iterdir()}
    assert {"j.jsonl.1", "j.jsonl.2"} <= names <= {"j.jsonl", "j.jsonl.1", "j.jsonl.2"}
    kept = [json.loads(line)["n"] for p in tmp_path.iterdir() for line in p.read_text().splitlines()]
    assert max(kept) == 39 and len(kept) < 40


def test_journal_tail_reads_only_the_end(tmp_path):
    journal = Journal(tmp_path / "t.jsonl", max_bytes=0)
    for n in range(100):
        journal.write("x", {"n": n})
    tail = journal.tail(max_bytes=200)
    assert 1 <= len(tail) < 100
    assert [r["n"] for r in tail] == list(range(100 - len(tail), 100))


# ---------------------------------------------------------------- monitor


async def test_monitor_ranks_every_target_and_snapshot_is_json(tmp_path):
    engine, event = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    await monitor.rank()
    strategies = monitor.ranking["strategies"]
    basket = strategies["negrisk_yes_basket"]
    assert basket["priced"] == 1 and basket["positive"] == 1
    assert basket["top"][0]["edge"] == pytest.approx(0.10) and basket["top"][0]["slug"] == event.slug
    assert basket["hist"][-1] == 1  # the >= +1c bin
    convert = strategies["negrisk_convert"]
    assert convert["top"][0]["edge"] == pytest.approx(-0.13) and convert["hist"][0] == 1  # the < -10c bin

    snap = loads(server_mod.dumps(monitor.snapshot()))
    assert snap["mode"] == "paper" and snap["engine"]["targets"] == engine.universe.counts()
    assert set(snap["series"]) == set(SERIES_FIELDS) and snap["ranking"]["strategies"]


def test_monitor_restore_rebuilds_feeds_counters_and_pnl_curve(tmp_path):
    engine, _ = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    now = time.time()
    monitor.restore([
        {"ts": now - 100, "kind": "opportunity", "key": "yes:1", "strategy": "negrisk_yes_basket", "title": "A"},
        {"ts": now - 90, "kind": "execution", "key": "yes:1", "complete": True, "orphaned": False},
        {"ts": now - 80, "kind": "execution", "key": "yes:2", "complete": False, "orphaned": True},
        {"ts": now - 70, "kind": "heartbeat", "pnl_total": 1.5, "pnl_today": 0.5, "locked": 2.0, "hold_exposure": 10, "fresh": 5},
        {"ts": now - 90_000, "kind": "heartbeat", "pnl_total": 0.0},
    ])
    assert len(monitor.opportunities) == 1 and len(monitor.executions) == 2
    assert monitor.exec_counts == {"sent": 2, "complete": 1, "orphaned": 1}
    view = monitor.series_view()
    assert view["pnl"] == [1.5] and view["locked"] == [2.0] and view["today"] == [0.5]
    kpi = monitor.tick()["kpi"]
    assert kpi["complete_rate"] == 0.5 and kpi["unique_opportunities"] == 1


async def test_monitor_forwards_journal_records_with_slugs(tmp_path):
    engine, event = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    queue = monitor.subscribe()
    key = engine.universe.targets[0].key
    engine.journal.write("opportunity", {"key": key, "strategy": "negrisk_yes_basket", "title": "T"})
    engine.journal.write("heartbeat", {"books": 1})
    kind, payload = queue.get_nowait()
    assert kind == "opportunity" and loads(payload)["slug"] == event.slug
    assert queue.empty()
    monitor.unsubscribe(queue)
    engine.journal.write("opportunity", {"key": key})
    assert queue.empty()


def test_slug_for_strips_ladder_suffix_and_falls_back_to_event_id(tmp_path):
    engine, event = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    key = engine.universe.targets[0].key
    assert monitor.slug_for(key + ":123>456") == event.slug
    assert monitor.slug_for(f"ladder:{event.id}:up:gone") == event.slug
    assert monitor.slug_for("nothing") is None


def test_series_view_keeps_the_last_hour_at_full_resolution(tmp_path):
    engine, _ = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    t0 = 1_000_000.0
    for i in range(1000):
        monitor.series.append((t0 + 10 * i,) + (float(i),) * (len(SERIES_FIELDS) - 1))
    view = monitor.series_view(recent_s=3600, max_older=50)
    assert len(view["t"]) == 361 + 50
    assert view["t"] == sorted(view["t"]) and view["t"][-1] == t0 + 9990
    assert [t for t in view["t"] if t >= t0 + 9990 - 3600] == [t0 + 10 * i for i in range(639, 1000)]


# ---------------------------------------------------------------- graceful shutdown


class SlowExecutor:
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.finished = False

    async def execute(self, opp, target, approve=None):
        self.started.set()
        await self.release.wait()
        self.finished = True
        return ExecutionReport(opp=opp, mode="paper", started_at=time.time(), finished_at=time.time())


async def test_cancelling_the_engine_never_cuts_an_execution_in_half(tmp_path):
    executor = SlowExecutor()
    engine, _ = make_engine(tmp_path, executor=executor)
    target = next(t for t in engine.universe.targets if t.strategy == "negrisk_yes_basket")
    opp = engine.by_name[target.strategy].evaluate(target, engine.store, time.time())[0]

    handler = asyncio.create_task(engine._handle(opp, target))
    await executor.started.wait()
    handler.cancel()
    drain = asyncio.create_task(engine.drain(timeout=5))
    await asyncio.sleep(0.02)
    assert handler.cancelled() and not drain.done() and len(engine._inflight) == 1

    executor.release.set()
    await drain
    assert executor.finished and not engine._inflight and not engine.risk.inflight_events

    await engine._handle(opp, target)
    assert engine.stats["skip:shutting down"] == 1


# ---------------------------------------------------------------- live readiness


def test_live_needs_every_critical_check(monkeypatch):
    monkeypatch.setattr(app_mod, "sdk_installed", lambda: True)
    env = {"POLYARB_LIVE_CONFIRM": "yes", "POLYMARKET_PRIVATE_KEY": "0x" + "11" * 32}
    checks = readiness("live", env, {"blocked": False, "country": "JP"}, True, True)
    assert live_blockers("live", checks) == []
    assert {c["id"]: c["detail"] for c in checks}["geo"] == "JP"

    bare = readiness("live", {"POLYARB_LIVE_CONFIRM": "YES"}, {"blocked": True, "country": "US"}, False, False)
    assert live_blockers("live", bare) == ["confirm", "key", "geo", "password", "volume"]
    assert live_blockers("live", readiness("live", env, {"error": "timeout", "blocked": True}, True, True)) == ["geo"]
    assert live_blockers("paper", bare) == []
    assert live_blockers("yolo", bare) == ["unknown mode 'yolo'"]


def test_private_key_never_reaches_logs_or_the_dashboard_console(tmp_path):
    key = "0x" + "ab12" * 16
    engine, _ = make_engine(tmp_path)
    monitor = Monitor(engine, info())
    logger = logging.getLogger("redact-test")
    logger.propagate = False
    out = io.StringIO()
    logger.addHandler(logging.StreamHandler(out))
    monitor.attach_logging(root=logger)
    redactor = redact_logs({"POLYMARKET_PRIVATE_KEY": key}, root=logger)
    try:
        logger.error("signer %s / bare %s", key, key[2:])
        try:
            raise ValueError(f"bad key {key}")
        except ValueError:
            logger.exception("gateway failed")
    finally:
        monitor.detach_logging(root=logger)
    dashboard = json.dumps(list(monitor.logs))
    assert key[2:] not in out.getvalue() and key[2:] not in dashboard
    assert out.getvalue().count("[redacted]") == 3 and "[redacted]" in dashboard
    assert redactor.clean(f"gateway: {key}") == "gateway: [redacted]"
    assert redact_logs({}, root=logger) is None


def test_volume_check_wants_a_mount_inside_containers(monkeypatch):
    monkeypatch.setattr(app_mod.os.path, "ismount", lambda p: str(p) == "/data")
    container = {"POLYARB_IN_CONTAINER": "1"}
    assert is_persistent(Path("/data/paper"), container)
    assert not is_persistent(Path("/app/data"), container)
    assert is_persistent(Path("/app/data"), {})


# ---------------------------------------------------------------- auth


def test_session_tokens_expire_and_resist_tampering():
    auth = Auth(PASSWORD, b"k" * 32, ttl_s=100)
    token = auth.issue(now=1000.0)
    assert auth.valid(token, now=1050) and not auth.valid(token, now=1101)
    expiry, sig = token.split(".")
    assert not auth.valid(f"{int(expiry) + 1000}.{sig}", now=1050)
    assert not auth.valid("garbage", now=1050) and not auth.valid(None)
    assert not Auth("changed", b"k" * 32).valid(token, now=1050)
    assert auth.check_password(PASSWORD) and not auth.check_password(PASSWORD.upper())


def test_login_failures_are_throttled_per_client_and_globally():
    auth = Auth(PASSWORD, b"k", max_failures=3, global_max_failures=5, window_s=60)
    for _ in range(3):
        auth.record_failure("a", now=0)
    assert auth.throttled("a", now=1) and not auth.throttled("b", now=1)
    assert not auth.throttled("a", now=61)
    for client in "bcdef":
        auth.record_failure(client, now=100)
    assert auth.throttled("someone-else", now=101)


# ---------------------------------------------------------------- web


@pytest.fixture
async def dashboard(tmp_path, monkeypatch):
    monkeypatch.setattr(server_mod, "FAILED_LOGIN_DELAY_S", 0.0)
    monkeypatch.setattr(server_mod, "KEEPALIVE_S", 0.05)
    engine, _ = make_engine(tmp_path)
    engine.phase = "running"
    monitor = Monitor(engine, info())
    app = create_app(monitor, Auth(PASSWORD, b"k" * 32, max_failures=3))
    async with TestClient(TestServer(app)) as client:
        yield client


async def login(client) -> None:
    resp = await client.post("/api/login", json={"password": PASSWORD})
    assert resp.status == 200


async def test_everything_but_login_and_health_needs_a_session(dashboard):
    resp = await dashboard.get("/", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"] == "/login"
    assert (await dashboard.get("/api/state")).status == 401
    assert (await dashboard.post("/api/control", json={"action": "pause"})).status == 401
    health = await dashboard.get("/healthz")
    assert health.status == 200 and (await health.json())["phase"] == "running"
    page = await dashboard.get("/login")
    assert page.status == 200 and "script-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Frame-Options"] == "DENY"
    assert f"login.js?v={asset_version()}" in await page.text()


async def test_login_sets_a_strict_cookie_and_unlocks_state_and_stream(dashboard):
    assert (await dashboard.post("/api/login", json={"password": "nope"})).status == 401
    resp = await dashboard.post("/api/login", json={"password": PASSWORD})
    cookie = resp.cookies[server_mod.COOKIE]
    assert resp.status == 200 and cookie["httponly"] and cookie["samesite"] == "Strict"

    page = await dashboard.get("/")
    assert page.status == 200 and f"app.js?v={asset_version()}" in await page.text()
    state = await (await dashboard.get("/api/state")).json()
    assert state["mode"] == "paper" and state["phase"] == "running" and "ranking" in state

    resp = await dashboard.get("/api/stream")
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    assert await resp.content.readuntil(b"\n\n") == b"retry: 3000\n\n"
    event = await resp.content.readuntil(b"\n\n")
    assert event.startswith(b"event: state\ndata: ")
    assert loads(event.split(b"data: ", 1)[1])["mode"] == "paper"
    dashboard.server.app[MONITOR_KEY].emit("log", {"msg": "hello"})
    assert await resp.content.readuntil(b"\n\n") == b'event: log\ndata: {"msg":"hello"}\n\n'
    resp.close()

    assert (await dashboard.post("/api/logout")).status == 200
    assert (await dashboard.get("/api/state")).status == 401


async def test_static_assets_are_cached_only_when_versioned(dashboard):
    versioned = await dashboard.get(f"/static/app.js?v={asset_version()}")
    assert versioned.status == 200 and "immutable" in versioned.headers["Cache-Control"]
    plain = await dashboard.get("/static/app.js")
    assert plain.status == 200 and "immutable" not in plain.headers.get("Cache-Control", "")
    for sneaky in ("/static/..%2Fserver.py", "/static/..%2Fapi/state", "/static/%2e%2e%2Fapi/state"):
        resp = await dashboard.get(sneaky, allow_redirects=False)
        assert resp.status in (403, 404), sneaky


async def test_controls_pause_resume_and_guarded_kill_reset(dashboard):
    await login(dashboard)
    engine = dashboard.server.app[MONITOR_KEY].engine
    kill = Path(engine.risk.cfg.kill_file)

    resp = await dashboard.post("/api/control", json={"action": "pause"})
    assert resp.status == 200 and kill.exists() and (await resp.json())["risk"]["paused"] is True
    resp = await dashboard.post("/api/control", json={"action": "resume"})
    assert resp.status == 200 and not kill.exists()

    engine.risk.kill("test")
    assert (await dashboard.post("/api/control", json={"action": "reset_kill"})).status == 400
    assert engine.risk.state.killed
    assert (await dashboard.post("/api/control", json={"action": "reset_kill", "confirm": "RESET"})).status == 200
    assert not engine.risk.state.killed

    assert (await dashboard.post("/api/control", json={"action": "boom"})).status == 400
    assert (await dashboard.post("/api/control", data=b"not json")).status == 400
    foreign = await dashboard.post("/api/control", json={"action": "pause"}, headers={"Origin": "https://evil.example"})
    assert foreign.status == 403 and not kill.exists()
    same = await dashboard.post("/api/control", json={"action": "pause"}, headers={"Origin": str(dashboard.make_url("/")).rstrip("/")})
    assert same.status == 200
    assert [r["action"] for r in engine.journal.read("control")] == ["pause", "resume", "reset_kill", "pause"]


async def test_pause_reports_a_disabled_kill_file(dashboard):
    await login(dashboard)
    dashboard.server.app[MONITOR_KEY].engine.risk.cfg.kill_file = ""
    resp = await dashboard.post("/api/control", json={"action": "pause"})
    assert resp.status == 409 and "kill_file" in (await resp.json())["error"]


async def test_brute_force_login_gets_429(dashboard):
    for _ in range(3):
        assert (await dashboard.post("/api/login", json={"password": "wrong"})).status == 401
    assert (await dashboard.post("/api/login", json={"password": PASSWORD})).status == 429
