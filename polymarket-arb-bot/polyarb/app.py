"""``polyarb serve``: engine + dashboard in one process.

Paper is the default. Live starts only when every critical precondition holds;
otherwise the bot keeps running in paper mode and the dashboard says exactly
what is missing, so a half-configured deployment can never trade by accident.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import time
from pathlib import Path
from typing import Any, Mapping

from polyarb.config import Config, use_data_dir
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.store import BookStore
from polyarb.engine import Engine
from polyarb.execution.executor import Executor
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.monitor import Monitor, RuntimeInfo
from polyarb.risk import RiskManager
from polyarb.universe import build_strategies
from polyarb.web.server import Auth, create_app, start

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


def readiness(requested: str, environ: Mapping[str, str], geo: Mapping[str, Any], password_set: bool, persistent: bool) -> list[dict[str, Any]]:
    """Live-trading checklist; every ``critical`` item must pass before live mode is allowed."""
    country = geo.get("country") or geo.get("error") or "?"
    return [
        {"id": "mode", "ok": requested == "live", "critical": False, "detail": requested},
        {"id": "confirm", "ok": environ.get("POLYARB_LIVE_CONFIRM") == "yes", "critical": True, "detail": ""},
        {"id": "key", "ok": bool(environ.get("POLYMARKET_PRIVATE_KEY")), "critical": True, "detail": ""},
        {"id": "sdk", "ok": sdk_installed(), "critical": True, "detail": ""},
        {"id": "geo", "ok": geo.get("blocked") is False, "critical": True, "detail": str(country)},
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
    values = [key, key[2:] if key.lower().startswith("0x") else "", environ.get("POLYARB_DASHBOARD_PASSWORD", "")]
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


async def _refresh_info(info: RuntimeInfo, risk: RiskManager, gateway: Any, geo_every_s: float = 600.0, wallet_every_s: float = 60.0) -> None:
    next_geo = time.monotonic() + geo_every_s
    while True:
        if info.mode == "live" and hasattr(gateway, "balance"):
            try:
                info.wallet = {**(info.wallet or {}), "balance": round(await gateway.balance(), 2), "updated_at": round(time.time(), 1)}
            except Exception as exc:
                log.warning("wallet balance refresh failed: %s", exc)
        if time.monotonic() >= next_geo:
            info.geo = await risk.check_geo()
            for item in info.readiness:
                if item["id"] == "geo":
                    item["ok"] = info.geo.get("blocked") is False
                    item["detail"] = str(info.geo.get("country") or info.geo.get("error") or "?")
            next_geo = time.monotonic() + geo_every_s
        await asyncio.sleep(wallet_every_s)


async def serve(cfg: Config, environ: Mapping[str, str] | None = None, host: str = "0.0.0.0", port: int | None = None) -> int:
    environ = os.environ if environ is None else environ
    redactor = redact_logs(environ)
    data_dir = Path(environ.get("POLYARB_DATA_DIR") or "data")
    data_dir.mkdir(parents=True, exist_ok=True)
    port = port or int(environ.get("PORT") or 8080)
    requested = (environ.get("POLYARB_MODE") or cfg.execution.mode or "paper").strip().lower()
    password = environ.get("POLYARB_DASHBOARD_PASSWORD") or ""
    generated = not password
    if generated:
        password = secrets.token_urlsafe(12)
        log.warning("POLYARB_DASHBOARD_PASSWORD is not set; dashboard password for this run: %s", password)

    geo = await RiskManager(cfg.risk).check_geo()
    checks = readiness(requested, environ, geo, not generated, is_persistent(data_dir, environ))
    blocked = live_blockers(requested, checks)

    mode, gateway, wallet = "paper", None, None
    if requested == "live" and not blocked:
        key = environ["POLYMARKET_PRIVATE_KEY"]
        try:
            from polyarb.execution.live import PolymarketGateway

            gateway = await PolymarketGateway.create(key, wallet=environ.get("POLYMARKET_WALLET") or None)
            mode = "live"
            wallet = gateway.wallet_info()
        except Exception as exc:
            log.exception("live gateway failed to start; running PAPER")
            blocked = [f"gateway: {redactor.clean(str(exc)) if redactor else exc}"]
    if blocked:
        log.error("live mode refused (%s); running PAPER", ", ".join(blocked))

    use_data_dir(cfg, data_dir, mode)
    cfg.execution.mode = mode
    if mode == "paper":
        cfg.risk.require_geo_ok = False
    info = RuntimeInfo(
        mode=mode, requested_mode=requested, live_blocked=blocked, readiness=checks, wallet=wallet, geo=geo,
        data_dir=str(data_dir), password_generated=generated,
    )

    strategies = build_strategies(cfg)
    store = BookStore(max_age_s=cfg.universe.max_book_age_s)
    risk = RiskManager(cfg.risk, cfg.execution.state_path)
    journal = Journal(cfg.execution.journal_path)
    if gateway is None:
        gateway = PaperGateway(store)

    async with GammaClient() as gamma, ClobClient() as clob:
        executor = Executor(gateway, store, {s.name: s for s in strategies}, cfg.execution, clob)
        engine = Engine(cfg, strategies, gamma, clob, store, executor, risk, journal)
        monitor = Monitor(engine, info)
        monitor.restore(journal.tail())
        monitor.attach_logging()
        redact_logs(environ)
        runner = await start(create_app(monitor, Auth(password, session_key(data_dir))), host, port)
        log.info("dashboard on http://%s:%d | mode %s | data %s", host, port, mode.upper(), data_dir)

        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, stop.set)
            except (NotImplementedError, RuntimeError):
                pass
        engine_task = asyncio.create_task(engine.run(), name="engine")
        stopper = asyncio.create_task(stop.wait(), name="stop")
        helpers = [asyncio.create_task(monitor.run(), name="monitor"), asyncio.create_task(_refresh_info(info, risk, gateway), name="info")]
        code = 0
        try:
            await asyncio.wait({engine_task, stopper}, return_when=asyncio.FIRST_COMPLETED)
            if engine_task.done() and not engine_task.cancelled():
                log.error("engine stopped unexpectedly: %r", engine_task.exception())
                code = 1
        finally:
            log.info("shutting down: finishing in-flight executions")
            await engine.drain()
            engine_task.cancel()
            await asyncio.gather(engine_task, return_exceptions=True)
            for task in (stopper, *helpers):
                task.cancel()
            await asyncio.gather(stopper, *helpers, return_exceptions=True)
            monitor.detach_logging()
            await runner.cleanup()
            await gateway.close()
        return code
