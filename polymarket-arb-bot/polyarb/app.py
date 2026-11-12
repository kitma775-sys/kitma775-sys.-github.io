"""``polyarb serve``: engine + web dashboard + Telegram command center in one process.

Paper is the default. Live starts only when every critical precondition holds;
otherwise the bot keeps running in paper mode and the dashboard / Telegram bot
says exactly what is missing, so a half-configured deployment can never trade
by accident. Runtime settings and mode switches persist in ``runtime_settings.json``
and apply immediately across both Web and Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from polyarb.config import Config, use_data_dir
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.metar import MetarFeed
from polyarb.data.store import BookStore
from polyarb.engine import Engine
from polyarb.execution.executor import Executor
from polyarb.execution.lifecycle import LifecycleManager
from polyarb.execution.maker_arb import MakerArbManager
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.monitor import Monitor, RuntimeInfo
from polyarb.risk import RiskManager, api_allowed
from polyarb.settings import SettingsManager
from polyarb.tg import TelegramBot, parse_allowed_users
from polyarb.universe import build_all_strategies
from polyarb.web.server import Auth, Controls, create_app, start

log = logging.getLogger(__name__)


def sdk_installed() -> bool:
    try:
        import polymarket  # noqa: F401
    except ImportError:
        return False
    return True


def is_persistent(path: Path, environ: Mapping[str, str]) -> bool:
    """In a container only a mounted volume survives a redeploy; on a normal host the local disk does."""
    if environ.get("POLYARB_IN_CONTAINER") != "1":
        return True
    p = path.resolve()
    while p != p.parent:
        if os.path.ismount(p):
            return True
        p = p.parent
    return False


def geo_check(geo: Mapping[str, Any]) -> dict[str, Any]:
    """Judged on API access: a frontend-only restriction (e.g. JP) still passes."""
    return {"id": "geo", "ok": api_allowed(geo), "critical": True, "detail": str(geo.get("country") or geo.get("error") or "?")}


def readiness(requested: str, environ: Mapping[str, str], geo: Mapping[str, Any], password_set: bool, persistent: bool) -> list[dict[str, Any]]:
    """Live-trading checklist; every ``critical`` item must pass before live mode is allowed."""
    return [
        {"id": "mode", "ok": requested == "live", "critical": False, "detail": requested},
        {"id": "confirm", "ok": environ.get("POLYARB_LIVE_CONFIRM") == "yes", "critical": True, "detail": ""},
        {"id": "key", "ok": bool(environ.get("POLYMARKET_PRIVATE_KEY")), "critical": True, "detail": ""},
        {"id": "sdk", "ok": sdk_installed(), "critical": True, "detail": ""},
        geo_check(geo),
        {"id": "password", "ok": password_set, "critical": True, "detail": ""},
        {"id": "volume", "ok": persistent, "critical": True, "detail": ""},
    ]


def live_blockers(requested: str, checks: list[dict[str, Any]]) -> list[str]:
    """Why ``requested`` cannot run live; empty means go (or that paper was asked for)."""
    if requested == "live":
        return [c["id"] for c in checks if c["critical"] and not c["ok"]]
    if requested != "paper":
        return [f"unknown mode {requested!r}"]
    return []


class RedactSecrets(logging.Filter):
    """Scrub secret values from a record before any handler (stdout, dashboard console) formats it."""

    def __init__(self, values: list[str]) -> None:
        super().__init__()
        self.values = sorted({v for v in values if len(v) >= 8}, key=len, reverse=True)

    def clean(self, text: str) -> str:
        for value in self.values:
            text = text.replace(value, "[redacted]")
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.clean(record.exc_text)
        try:
            msg = record.getMessage()
        except Exception:
            return True
        cleaned = self.clean(msg)
        if cleaned != msg:
            record.msg, record.args = cleaned, None
        return True


def redact_logs(environ: Mapping[str, str], root: logging.Logger | None = None) -> RedactSecrets | None:
    """Attach the filter to every handler on the root logger (call again after adding handlers)."""
    key = environ.get("POLYMARKET_PRIVATE_KEY", "").strip()
    tg_tok = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    tg_secret_part = tg_tok.split(":", 1)[1] if ":" in tg_tok else ""
    values = [
        key,
        key[2:] if key.lower().startswith("0x") else "",
        environ.get("POLYARB_DASHBOARD_PASSWORD", ""),
        environ.get("POLYMARKET_RELAYER_API_KEY", ""),
        environ.get("POLYMARKET_BUILDER_API_KEY", ""),
        environ.get("POLYMARKET_BUILDER_SECRET", ""),
        environ.get("POLYMARKET_BUILDER_PASSPHRASE", ""),
        tg_tok,
        tg_secret_part,
        environ.get("PASSWORD", ""),
    ]
    if not any(values):
        return None
    root = root or logging.getLogger()
    for handler in root.handlers:
        existing = next((f for f in handler.filters if isinstance(f, RedactSecrets)), None)
        if existing is None:
            handler.addFilter(RedactSecrets(values))
    return next((f for h in root.handlers for f in h.filters if isinstance(f, RedactSecrets)), None)


def session_key(data_dir: Path) -> bytes:
    path = data_dir / ".session_key"
    try:
        if path.exists():
            return path.read_bytes()
        key = secrets.token_bytes(32)
        path.write_bytes(key)
        path.chmod(0o600)
        return key
    except OSError:
        return secrets.token_bytes(32)


@dataclass
class _ActiveRuntime:
    engine: Engine
    engine_task: asyncio.Task
    gateway: Any
    lifecycle: LifecycleManager
    risk: RiskManager
    info: RuntimeInfo


async def _refresh_info(
    target: list[_ActiveRuntime] | RuntimeInfo,
    risk: Any = None,
    gateway: Any = None,
    *,
    geo_every_s: float = 600.0,
    wallet_every_s: float = 30.0,
) -> None:
    next_geo = time.monotonic() + geo_every_s
    while True:
        if isinstance(target, list):
            rt = target[0] if target else None
            info = rt.info if rt else None
            cur_risk = rt.risk if rt else None
            cur_gw = rt.gateway if rt else None
        else:
            info, cur_risk, cur_gw = target, risk, gateway
        if info is not None and cur_risk is not None:
            if info.mode == "live" and hasattr(cur_gw, "balance"):
                try:
                    bal = float(await cur_gw.balance())
                    if hasattr(cur_risk, "wallet_balance"):
                        cur_risk.wallet_balance = bal
                    info.wallet = {**(info.wallet or {}), "balance": round(bal, 2), "updated_at": round(time.time(), 1)}
                except Exception as exc:
                    log.warning("wallet balance refresh failed: %s", exc)
            if time.monotonic() >= next_geo:
                info.geo = await cur_risk.check_geo()
                for item in info.readiness:
                    if item["id"] == "geo":
                        item.update(geo_check(info.geo))
                next_geo = time.monotonic() + geo_every_s
        await asyncio.sleep(wallet_every_s)


async def serve(cfg: Config, environ: Mapping[str, str] | None = None, host: str = "0.0.0.0", port: int | None = None) -> int:
    environ = os.environ if environ is None else environ
    redactor = redact_logs(environ)
    data_dir = Path(environ.get("POLYARB_DATA_DIR") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    port = port or int(environ.get("PORT") or 8080)
    password = environ.get("POLYARB_DASHBOARD_PASSWORD") or ""
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
        log.warning("POLYARB_DASHBOARD_PASSWORD is not set; dashboard password for this run: %s", password)

    settings = SettingsManager(cfg, data_dir / "runtime_settings.json")
    env_requested = (environ.get("POLYARB_MODE") or cfg.execution.mode or "paper").strip().lower()
    initial_requested = settings.preferred_mode or env_requested
    persistent = is_persistent(data_dir, environ)

    shared_store = BookStore(max_age_s=cfg.universe.max_book_age_s)
    metar_feed = MetarFeed(
        fast_poll_s=cfg.metar.fast_poll_s,
        idle_poll_s=cfg.metar.idle_poll_s,
        glitch_delta_c=cfg.metar.glitch_delta_c,
    )
    switch_lock = asyncio.Lock()
    holder: list[_ActiveRuntime] = []

    async def _build_runtime(
        requested_mode: str,
        gamma: GammaClient,
        clob: ClobClient,
        cached_events: list | None = None,
        strict_live: bool = False,
    ) -> tuple[_ActiveRuntime | None, str]:
        run_cfg = settings.build_fresh_config()
        geo = await RiskManager(run_cfg.risk).check_geo()
        checks = readiness(requested_mode, environ, geo, not generated, persistent)
        blocked = live_blockers(requested_mode, checks)

        mode, gateway, wallet = "paper", None, None
        if requested_mode == "live" and not blocked:
            key = environ["POLYMARKET_PRIVATE_KEY"]
            try:
                from polyarb.execution.live import PolymarketGateway

                gateway = await PolymarketGateway.create(key, wallet=environ.get("POLYMARKET_WALLET") or None, environ=environ)
                mode = "live"
                wallet = gateway.wallet_info()
            except Exception as exc:
                log.exception("live gateway failed to start")
                err_msg = f"gateway: {redactor.clean(str(exc)) if redactor else exc}"
                if strict_live:
                    return None, f"連接實盤閘道失敗：{err_msg}"
                blocked = [err_msg]
        elif requested_mode == "live" and blocked and strict_live:
            return None, f"實盤安全條件未通過：{', '.join(blocked)}"

        if blocked:
            log.error("live mode refused (%s); running PAPER", ", ".join(blocked))

        use_data_dir(run_cfg, data_dir, mode)
        run_cfg.execution.mode = mode
        if mode == "paper":
            run_cfg.risk.require_geo_ok = False
        info = RuntimeInfo(
            mode=mode,
            requested_mode=requested_mode,
            live_blocked=blocked,
            readiness=checks,
            wallet=wallet,
            geo=geo,
            data_dir=str(data_dir),
            password_generated=generated,
        )

        shared_store.max_age_s = run_cfg.universe.max_book_age_s
        all_strats = build_all_strategies(run_cfg, metar_feed=metar_feed)
        active_strats = [s for s in all_strats if s.enabled]
        risk = RiskManager(run_cfg.risk, run_cfg.execution.state_path)
        risk.geo = geo
        journal = Journal(run_cfg.execution.journal_path)
        if gateway is None:
            gateway = PaperGateway(shared_store)
        lifecycle = LifecycleManager(run_cfg, gateway, risk, journal, wallet_funder=(wallet or {}).get("funder"), store=shared_store)
        maker_arb = MakerArbManager(run_cfg, shared_store, gateway, risk, journal, clob=clob)
        executor = Executor(gateway, shared_store, {s.name: s for s in all_strats}, run_cfg.execution, clob)
        engine = Engine(
            run_cfg,
            active_strats,
            gamma,
            clob,
            shared_store,
            executor,
            risk,
            journal,
            all_strategies=all_strats,
            initial_events=cached_events,
            metar_feed=metar_feed,
            maker_arb=maker_arb,
            lifecycle=lifecycle,
        )
        task = asyncio.create_task(engine.run(), name=f"engine-{mode}")
        return _ActiveRuntime(engine, task, gateway, lifecycle, risk, info), mode

    async with GammaClient() as gamma, ClobClient() as clob:
        first_rt, actual_mode = await _build_runtime(initial_requested, gamma, clob, strict_live=False)
        assert first_rt is not None
        holder.append(first_rt)

        monitor = Monitor(first_rt.engine, first_rt.info)
        monitor.settings = settings
        monitor.restore(first_rt.engine.journal.tail())
        monitor.attach_logging()
        redact_logs(environ)

        async def _switch_mode(target_mode: str, pause_first: bool | None = None) -> tuple[bool, str]:
            async with switch_lock:
                cur = holder[0]
                if target_mode == cur.info.mode:
                    settings.set_preferred_mode(target_mode)
                    return True, f"目前已經是 {'實盤 LIVE' if target_mode == 'live' else '紙盤 PAPER'} 模式。"
                # Pre-validate live preconditions before stopping the running engine!
                if target_mode == "live":
                    checks = readiness("live", environ, cur.info.geo or await cur.risk.check_geo(), not generated, persistent)
                    blk = live_blockers("live", checks)
                    if blk:
                        return False, f"實盤條件未通過：{', '.join(blk)}"

                cached_events = cur.engine._raw_events
                log.warning("hot-switching mode from %s to %s", cur.info.mode, target_mode)
                await cur.engine.drain()
                cur.engine_task.cancel()
                await asyncio.gather(cur.engine_task, return_exceptions=True)
                await cur.lifecycle.close()
                await cur.gateway.close()

                if pause_first is True:
                    kill_p = Path(cur.risk.cfg.kill_file) if cur.risk.cfg.kill_file else None
                    if kill_p is not None:
                        kill_p.parent.mkdir(parents=True, exist_ok=True)
                        kill_p.write_text("paused during live mode switch\n")
                elif pause_first is False:
                    kill_p = Path(cur.risk.cfg.kill_file) if cur.risk.cfg.kill_file else None
                    if kill_p is not None:
                        kill_p.unlink(missing_ok=True)

                new_rt, msg = await _build_runtime(
                    target_mode, gamma, clob, cached_events=cached_events, strict_live=(target_mode == "live")
                )
                if new_rt is None:
                    # Roll back cleanly to paper so the bot never stays down!
                    fallback_rt, _ = await _build_runtime("paper", gamma, clob, cached_events=cached_events, strict_live=False)
                    assert fallback_rt is not None
                    holder[0] = fallback_rt
                    monitor.rebind(fallback_rt.engine, fallback_rt.info, fallback_rt.engine.journal.tail())
                    return False, msg

                settings.set_preferred_mode(target_mode)
                holder[0] = new_rt
                monitor.rebind(new_rt.engine, new_rt.info, new_rt.engine.journal.tail())
                mode_label = "實盤 LIVE" if new_rt.info.mode == "live" else "紙盤 PAPER"
                pause_note = "（已保持暫停，確認餘額後可按恢復）" if pause_first is True else ""
                return True, f"已成功熱切換至 {mode_label}{pause_note}，無需重啟容器，35,000+ 訂單簿即時沿用。"

        controls = Controls(monitor, settings=settings, mode_switcher=_switch_mode)

        tg_token = (environ.get("TELEGRAM_BOT_TOKEN") or "").strip()
        tg_users = parse_allowed_users(environ.get("POLYARB_TG_ALLOWED_USERS") or environ.get("TELEGRAM_CHAT_ID"))
        dash_url = (environ.get("POLYARB_PUBLIC_URL") or "").strip()
        tg_bot: TelegramBot | None = None
        if tg_token and tg_users:
            tg_bot = TelegramBot(tg_token, tg_users, monitor, controls, settings, dashboard_url=dash_url)
            monitor.tg_bot = tg_bot

        runner = await start(create_app(monitor, Auth(password, session_key(data_dir)), controls=controls), host, port)
        log.info("dashboard on http://%s:%d | mode %s | data %s", host, port, actual_mode.upper(), data_dir)
        if tg_bot is not None:
            await tg_bot.start()

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        stopper = asyncio.create_task(stop.wait(), name="stop")
        helpers = [asyncio.create_task(monitor.run(), name="monitor"), asyncio.create_task(_refresh_info(holder), name="info")]
        code = 0
        try:
            while not stopper.done():
                cur_task = holder[0].engine_task
                done, _ = await asyncio.wait({cur_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
                if stopper in done:
                    break
                # If cur_task finished ONLY because _switch_mode replaced holder[0], keep running!
                if holder[0].engine_task is cur_task and cur_task.done() and not cur_task.cancelled():
                    log.error("engine stopped unexpectedly: %r", cur_task.exception())
                    code = 1
                    break
        finally:
            log.info("shutting down: finishing in-flight executions")
            cur = holder[0]
            await cur.engine.drain()
            cur.engine_task.cancel()
            await asyncio.gather(cur.engine_task, return_exceptions=True)
            for task in (stopper, *helpers):
                task.cancel()
            await asyncio.gather(stopper, *helpers, return_exceptions=True)
            if tg_bot is not None:
                await tg_bot.stop()
            monitor.detach_logging()
            await runner.cleanup()
            await metar_feed.close()
            await cur.lifecycle.close()
            await cur.gateway.close()
        return code
