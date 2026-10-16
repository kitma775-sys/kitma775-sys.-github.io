from __future__ import annotations

import asyncio
import functools
import hashlib
import hmac
import logging
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlsplit

from aiohttp import web

from polyarb import __version__
from polyarb.fastjson import dumps
from polyarb.monitor import Monitor

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "polyarb_session"
FAILED_LOGIN_DELAY_S = 0.4
KEEPALIVE_S = 15.0
PUBLIC_PATHS = {"/login", "/api/login", "/healthz", "/favicon.ico"}
CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
    "connect-src 'self'; font-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def _now(now: float | None) -> float:
    return time.time() if now is None else now


class Auth:
    """Single shared password -> HMAC-signed, expiring session cookie; brute force is throttled."""

    def __init__(
        self,
        password: str,
        key: bytes,
        ttl_s: float = 14 * 86400,
        max_failures: int = 8,
        global_max_failures: int = 60,
        window_s: float = 600.0,
    ) -> None:
        self._digest = hashlib.sha256(password.encode()).digest()
        # Changing the password (or the persisted key) invalidates every session.
        self._secret = hashlib.sha256(key + b"\0" + password.encode()).digest()
        self.ttl_s = ttl_s
        self.max_failures = max_failures
        self.global_max_failures = global_max_failures
        self.window_s = window_s
        self._failures: dict[str, deque[float]] = {}
        self._all_failures: deque[float] = deque()

    def check_password(self, candidate: str) -> bool:
        return hmac.compare_digest(hashlib.sha256(candidate.encode()).digest(), self._digest)

    def issue(self, now: float | None = None) -> str:
        expiry = str(int(_now(now) + self.ttl_s))
        return f"{expiry}.{hmac.new(self._secret, expiry.encode(), hashlib.sha256).hexdigest()}"

    def valid(self, token: str | None, now: float | None = None) -> bool:
        if not token or "." not in token:
            return False
        expiry, sig = token.split(".", 1)
        if not expiry.isdigit() or int(expiry) < _now(now):
            return False
        return hmac.compare_digest(sig, hmac.new(self._secret, expiry.encode(), hashlib.sha256).hexdigest())

    def _recent(self, events: deque[float], now: float) -> int:
        while events and now - events[0] > self.window_s:
            events.popleft()
        return len(events)

    def throttled(self, client: str, now: float | None = None) -> bool:
        now = _now(now)
        mine = self._failures.get(client)
        if mine is not None and self._recent(mine, now) >= self.max_failures:
            return True
        return self._recent(self._all_failures, now) >= self.global_max_failures

    def record_failure(self, client: str, now: float | None = None) -> None:
        now = _now(now)
        if len(self._failures) > 10_000:
            self._failures.clear()
        self._failures.setdefault(client, deque()).append(now)
        self._all_failures.append(now)


class Controls:
    """Operator actions: pause/resume via the kill file, and clearing a tripped kill switch."""

    def __init__(self, monitor: Monitor) -> None:
        self.monitor = monitor

    @property
    def _kill_file(self) -> Path | None:
        path = self.monitor.engine.risk.cfg.kill_file
        return Path(path) if path else None

    def pause(self) -> str | None:
        path = self._kill_file
        if path is None:
            return "kill_file is disabled in the config"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"paused from dashboard at {datetime.now(timezone.utc).isoformat()}\n")
        return None

    def resume(self) -> None:
        path = self._kill_file
        if path is not None:
            path.unlink(missing_ok=True)

    def reset_kill(self) -> None:
        self.monitor.engine.risk.reset()


MONITOR_KEY = web.AppKey("monitor", Monitor)
AUTH_KEY = web.AppKey("auth", Auth)
CONTROLS_KEY = web.AppKey("controls", Controls)


def client_ip(request: web.Request) -> str:
    # The proxy in front of us appends the peer it saw; earlier hops are client-supplied and spoofable.
    forwarded = request.headers.get("X-Forwarded-For", "")
    hops = [h.strip() for h in forwarded.split(",") if h.strip()]
    return hops[-1] if hops else (request.remote or "?")


def _is_https(request: web.Request) -> bool:
    return request.secure or request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if not origin:
        return True  # browsers always send Origin on cross-site POSTs; tools like curl are not a CSRF vector
    host = urlsplit(origin).netloc
    return host in {request.host, request.headers.get("X-Forwarded-Host", "")}


def _json(data: object, status: int = 200) -> web.Response:
    return web.Response(body=dumps(data), status=status, content_type="application/json")


@functools.cache
def asset_version() -> str:
    digest = hashlib.sha256(__version__.encode())
    for path in sorted(STATIC_DIR.glob("*")):
        if path.suffix in (".css", ".js", ".svg"):
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


def _page(name: str) -> web.Response:
    html = (STATIC_DIR / name).read_text(encoding="utf-8").replace("{{V}}", asset_version())
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


@web.middleware
async def _guard(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]) -> web.StreamResponse:
    if request.method == "POST" and not _same_origin(request):
        return _json({"error": "cross-origin request refused"}, 403)
    path = request.path
    if path in PUBLIC_PATHS or path.startswith("/static/"):
        return await handler(request)
    if not request.app[AUTH_KEY].valid(request.cookies.get(COOKIE)):
        if path.startswith("/api/"):
            return _json({"error": "unauthorized"}, 401)
        raise web.HTTPFound("/login")
    return await handler(request)


async def _headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if _is_https(request):
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    if request.path.startswith("/static/") and request.query.get("v") == asset_version() and response.status == 200:
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"


async def index(request: web.Request) -> web.Response:
    return _page("index.html")


async def login_page(request: web.Request) -> web.StreamResponse:
    if request.app[AUTH_KEY].valid(request.cookies.get(COOKIE)):
        raise web.HTTPFound("/")
    return _page("login.html")


async def login(request: web.Request) -> web.Response:
    auth = request.app[AUTH_KEY]
    client = client_ip(request)
    if auth.throttled(client):
        return _json({"error": "too many attempts, try again later"}, 429)
    try:
        data = await request.json()
    except ValueError:
        data = dict(await request.post())
    if not auth.check_password(str(data.get("password", ""))):
        auth.record_failure(client)
        log.warning("dashboard login failed from %s", client)
        await asyncio.sleep(FAILED_LOGIN_DELAY_S)
        return _json({"error": "wrong password"}, 401)
    resp = _json({"ok": True})
    resp.set_cookie(COOKIE, auth.issue(), max_age=int(auth.ttl_s), httponly=True, samesite="Strict", secure=_is_https(request), path="/")
    log.info("dashboard login from %s", client)
    return resp


async def logout(request: web.Request) -> web.Response:
    resp = _json({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def state(request: web.Request) -> web.Response:
    return _json(request.app[MONITOR_KEY].snapshot())


async def stream(request: web.Request) -> web.StreamResponse:
    monitor = request.app[MONITOR_KEY]
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    await resp.prepare(request)
    queue = monitor.subscribe()
    try:
        await resp.write(b"retry: 3000\n\nevent: state\ndata: " + dumps(monitor.snapshot()) + b"\n\n")
        while True:
            try:
                kind, payload = await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_S)
            except asyncio.TimeoutError:
                await resp.write(b": keep-alive\n\n")
                continue
            await resp.write(b"event: " + kind.encode() + b"\ndata: " + payload + b"\n\n")
    except (ConnectionResetError, RuntimeError):
        pass  # client went away
    finally:
        monitor.unsubscribe(queue)
    return resp


async def control(request: web.Request) -> web.Response:
    controls = request.app[CONTROLS_KEY]
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "expected JSON"}, 400)
    action = body.get("action") if isinstance(body, dict) else None
    if action == "pause":
        error = controls.pause()
        if error:
            return _json({"error": error}, 409)
    elif action == "resume":
        controls.resume()
    elif action == "reset_kill":
        if body.get("confirm") != "RESET":
            return _json({"error": 'reset_kill needs {"confirm": "RESET"}'}, 400)
        controls.reset_kill()
    else:
        return _json({"error": f"unknown action {action!r}"}, 400)
    monitor = request.app[MONITOR_KEY]
    monitor.engine.journal.write("control", {"action": action, "client": client_ip(request)})
    log.warning("dashboard control: %s from %s", action, client_ip(request))
    return _json({"ok": True, "risk": monitor.tick()["risk"]})


async def healthz(request: web.Request) -> web.Response:
    monitor = request.app[MONITOR_KEY]
    engine = monitor.engine
    healthy = engine.phase != "stopped"
    return _json({"ok": healthy, "phase": engine.phase, "mode": monitor.info.mode, "version": __version__}, 200 if healthy else 503)


async def favicon(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "favicon.svg", headers={"Content-Type": "image/svg+xml"})


def create_app(monitor: Monitor, auth: Auth) -> web.Application:
    app = web.Application(middlewares=[_guard], client_max_size=64 * 1024)
    app[MONITOR_KEY] = monitor
    app[AUTH_KEY] = auth
    app[CONTROLS_KEY] = Controls(monitor)
    app.on_response_prepare.append(_headers)
    app.router.add_get("/", index)
    app.router.add_get("/login", login_page)
    app.router.add_post("/api/login", login)
    app.router.add_post("/api/logout", logout)
    app.router.add_get("/api/state", state)
    app.router.add_get("/api/stream", stream)
    app.router.add_post("/api/control", control)
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/favicon.ico", favicon)
    app.router.add_static("/static/", STATIC_DIR)
    return app


async def start(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None, shutdown_timeout=3.0)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    return runner
