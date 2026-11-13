import json
import logging
from pathlib import Path

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from polyarb.app import redact_logs
from polyarb.config import Config
from polyarb.data.metar import MetarFeed
from polyarb.data.store import BookStore
from polyarb.engine import Engine
from polyarb.execution.executor import Executor
from polyarb.execution.lifecycle import LifecycleManager
from polyarb.execution.maker_arb import MakerArbManager
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.monitor import Monitor, RuntimeInfo
from polyarb.risk import RiskManager
from polyarb.settings import SPECS, SettingsManager
from polyarb.tg import TelegramBot, parse_allowed_users
from polyarb.universe import build_all_strategies, build_universe
from polyarb.web.server import Auth, Controls, create_app

from conftest import book, make_event, make_market


def _setup_synced_stack(tmp_path: Path):
    cfg = Config()
    cfg.engine.use_websocket = False
    cfg.risk.kill_file = str(tmp_path / "KILL")
    cfg.risk.max_trade_usd = 25.0
    cfg.risk.bankroll_usd = 70.0

    settings = SettingsManager(cfg, tmp_path / "runtime_settings.json")
    store = BookStore(max_age_s=1e9)
    metar_feed = MetarFeed()
    all_strats = build_all_strategies(cfg, metar_feed=metar_feed)
    active_strats = [s for s in all_strats if s.enabled]

    ms = [make_market(f"Will O{i} win?", group=f"O{i}", neg_risk=True, index=i) for i in range(3)]
    for m in ms:
        m.volume_24h = 1500.0
    ev = make_event(ms, neg_risk=True, title="Tri-way Election")
    store.put(book(ms[0].yes_token, bids=[(0.34, 100)], asks=[(0.38, 100)]))
    store.put(book(ms[1].yes_token, bids=[(0.33, 100)], asks=[(0.35, 100)]))
    store.put(book(ms[2].yes_token, bids=[(0.32, 100)], asks=[(0.34, 100)]))

    gw = PaperGateway(store)
    risk = RiskManager(cfg.risk, tmp_path / "state.json")
    journal = Journal(tmp_path / "journal.jsonl")
    maker = MakerArbManager(cfg, store, gw, risk, journal)
    lc = LifecycleManager(cfg, gw, risk, journal)
    ex = Executor(gw, store, {s.name: s for s in all_strats}, cfg.execution)
    engine = Engine(
        cfg, active_strats, None, None, store, ex, risk, journal,
        all_strategies=all_strats, metar_feed=metar_feed, maker_arb=maker, lifecycle=lc,
    )
    engine._raw_events = [ev]
    engine.universe = build_universe([ev], active_strats, cfg.universe)
    engine.targets = {t.key: t for t in engine.universe.targets}
    maker.set_targets(engine.universe.targets)

    info = RuntimeInfo(
        mode="paper",
        requested_mode="paper",
        readiness=[
            {"id": "mode", "ok": False, "critical": False, "detail": "paper"},
            {"id": "confirm", "ok": True, "critical": True, "detail": ""},
            {"id": "key", "ok": True, "critical": True, "detail": ""},
            {"id": "sdk", "ok": True, "critical": True, "detail": ""},
            {"id": "geo", "ok": True, "critical": True, "detail": "JP"},
            {"id": "password", "ok": True, "critical": True, "detail": ""},
            {"id": "volume", "ok": True, "critical": True, "detail": ""},
        ],
        data_dir=str(tmp_path),
    )
    monitor = Monitor(engine, info)
    monitor.settings = settings

    switched_modes: list[tuple[str, bool | None]] = []

    async def fake_switcher(target_mode: str, pause_first: bool | None = None) -> tuple[bool, str]:
        switched_modes.append((target_mode, pause_first))
        info.mode = target_mode
        settings.set_preferred_mode(target_mode)
        return True, f"已切換至 {target_mode}"

    controls = Controls(monitor, settings=settings, mode_switcher=fake_switcher)

    sent_calls: list[tuple[str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content.decode()) if request.content else {}
        sent_calls.append((method, body))
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"id": 1001, "is_bot": True, "username": "test_polyarb_bot"}})
        return httpx.Response(200, json={"ok": True, "result": {"message_id": len(sent_calls)}})

    tg = TelegramBot(
        "123456:TEST_SECRET_TOKEN_VALUE_XYZ",
        allowed_users={777888},
        monitor=monitor,
        controls=controls,
        settings=settings,
        dashboard_url="https://example.zeabur.app",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    monitor.tg_bot = tg
    return cfg, settings, engine, monitor, controls, tg, sent_calls, switched_modes


def test_every_setting_spec_maps_to_valid_config_field_and_short_callback():
    cfg = Config()
    assert len(SPECS) >= 40
    codes = set()
    for spec in SPECS:
        assert len(spec.code) <= 6 and spec.code not in codes
        codes.add(spec.code)
        section, attr = spec.key.split(".", 1)
        assert hasattr(getattr(cfg, section), attr)
        for idx, _ in enumerate(spec.presets):
            assert len(f"sp:{spec.code}:{idx}".encode()) <= 64


def test_parse_allowed_users_and_secret_log_redaction():
    assert parse_allowed_users("6567897986, 12345 ; ") == {6567897986, 12345}
    assert parse_allowed_users("") == set()

    tok = "99999999:SuperSecretTelegramBotPart12345"
    logger = logging.getLogger("tg-redact-test")
    logger.handlers.clear()
    logger.propagate = False
    records = []

    class Grab(logging.Handler):
        def emit(self, rec):
            records.append(rec.getMessage())

    logger.addHandler(Grab())
    redact_logs({"TELEGRAM_BOT_TOKEN": tok}, root=logger)
    logger.error("url https://api.telegram.org/bot%s/getUpdates failed", tok)
    assert "SuperSecretTelegramBotPart12345" not in records[0]
    assert "[redacted]" in records[0]


async def test_telegram_blocks_unauthorized_users_silently(tmp_path):
    _, _, _, _, controls, tg, sent_calls, _ = _setup_synced_stack(tmp_path)
    await tg.handle_update({
        "update_id": 1,
        "message": {"from": {"id": 999999}, "chat": {"id": 999999}, "text": "/pause"},
    })
    assert tg.unauthorized_blocked == 1
    assert sent_calls == []
    assert controls.is_paused() is False

    # Group chat leak protection: authorized user inside an unauthorized group chat is also blocked!
    await tg.handle_update({
        "update_id": 2,
        "message": {"from": {"id": 777888}, "chat": {"id": -100555555}, "text": "/status"},
    })
    assert tg.unauthorized_blocked == 2
    assert sent_calls == []


async def test_telegram_and_web_share_pause_settings_and_mode_switch(tmp_path):
    cfg, settings, engine, monitor, controls, tg, sent_calls, switched = _setup_synced_stack(tmp_path)

    # Maker places a quote while active
    await engine.maker_arb.step()
    assert len(engine.maker_arb.active) == 1

    # 1. Pause from Telegram -> cancels open maker quote & reflects on web state immediately!
    await tg.handle_update({
        "update_id": 10,
        "callback_query": {"id": "cq1", "from": {"id": 777888}, "message": {"message_id": 5, "chat": {"id": 777888}}, "data": "act:pause"},
    })
    assert controls.is_paused() is True
    assert len(engine.maker_arb.active) == 0
    assert monitor.tick()["risk"]["paused"] is True

    # 2. Change numeric setting via Telegram preset + custom text input -> updates Config & persists to disk!
    await tg.handle_update({
        "update_id": 11,
        "callback_query": {"id": "cq2", "from": {"id": 777888}, "message": {"message_id": 5, "chat": {"id": 777888}}, "data": "sa:r_tr"},
    })
    await tg.handle_update({
        "update_id": 12,
        "message": {"from": {"id": 777888}, "chat": {"id": 777888}, "text": "38.5"},
    })
    assert cfg.risk.max_trade_usd == pytest.approx(38.5)
    assert engine.by_name["negrisk_convert"].max_trade_usd == pytest.approx(38.5)

    # Reload SettingsManager from disk -> persisted!
    reloaded = SettingsManager(Config(), tmp_path / "runtime_settings.json")
    assert reloaded.overrides["risk.max_trade_usd"] == pytest.approx(38.5)

    # 3. Toggle strategy enable flag from Telegram -> hot-rebuilds active strategies without restart!
    await tg.handle_update({
        "update_id": 13,
        "callback_query": {"id": "cq3", "from": {"id": 777888}, "message": {"message_id": 5, "chat": {"id": 777888}}, "data": "st:c_on:cat"},
    })
    assert cfg.convert.enabled is False
    assert "negrisk_convert" not in {s.name for s in engine.strategies}

    # 4. Safe hot-switch to live (paused) from Telegram!
    await tg.handle_update({
        "update_id": 14,
        "callback_query": {"id": "cq4", "from": {"id": 777888}, "message": {"message_id": 5, "chat": {"id": 777888}}, "data": "md:go_live_paused"},
    })
    assert switched == [("live", True)]
    assert monitor.info.mode == "live"
    assert settings.preferred_mode == "live"

    # 5. Web /api/settings endpoint reads & updates the exact same SettingsManager!
    app = create_app(monitor, Auth("pw123", b"k" * 32), controls=controls)
    async with TestClient(TestServer(app)) as client:
        await client.post("/api/login", json={"password": "pw123"})
        r_get = await client.get("/api/settings")
        assert r_get.status == 200
        snap = await r_get.json()
        assert snap["overrides"]["risk.max_trade_usd"] == pytest.approx(38.5)

        r_post = await client.post("/api/settings", json={"key": "maker.min_edge", "value": 0.012})
        assert r_post.status == 200
        assert cfg.maker.min_edge == pytest.approx(0.012)

        r_rst = await client.post("/api/settings", json={"action": "reset"})
        assert r_rst.status == 200
        assert settings.overrides == {}
        assert cfg.convert.enabled is True

        # 6. 5-Skin Studio bidirectional sync between Web Dashboard and Telegram (/skin)!
        for skin_id in ("spatial", "obsidian", "cyber", "titanium", "orbital"):
            r_sk = await client.post("/api/control", json={"action": "set_skin", "skin": skin_id})
            assert r_sk.status == 200 and (await r_sk.json())["skin"] == skin_id
            assert settings.preferred_skin == skin_id

        assert (await client.post("/api/control", json={"action": "set_skin", "skin": "unknown"})).status == 400

        await tg.handle_update({
            "update_id": 15,
            "callback_query": {"id": "cq5", "from": {"id": 777888}, "message": {"message_id": 5, "chat": {"id": 777888}}, "data": "sk:obsidian"},
        })
        assert settings.preferred_skin == "obsidian"
        reloaded2 = SettingsManager(Config(), tmp_path / "runtime_settings.json")
        assert reloaded2.preferred_skin == "obsidian"
