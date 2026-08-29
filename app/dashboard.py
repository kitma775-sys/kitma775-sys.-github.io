from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from app.config import clamp_paper_cash, favorite_window_label, favorite_window_of, live_keys_ready, strategy_mode_of
from app.runtime import Runtime

PAGE = Path(__file__).with_name("dashboard.html")


def create_app(rt: Runtime) -> FastAPI:
    app = FastAPI(title="Surf Arb Dashboard")
    token = rt.env.dashboard_token

    def _ok(request: Request, q: str | None) -> bool:
        if not token:
            return True
        if q == token:
            return True
        return request.cookies.get("dash") == token

    @app.get("/health")
    async def health():
        s = rt.settings()
        paper = rt.store.paper_state() if rt.mode() == "paper" else None
        win = favorite_window_of(s)
        return {
            "ok": True,
            "mode": rt.mode(),
            "strategy_rev": s.get("strategy_rev"),
            "ws_status": rt.ws_status,
            "live_trading": bool(s.get("live_trading")),
            "force_paper": bool(rt.env.force_paper),
            "keys_ready": live_keys_ready(rt.env),
            "engine_running": bool(s.get("engine_running")),
            "killed": bool(s.get("killed")),
            "maker_window_seconds": s.get("maker_window_seconds"),
            "max_book_age_ms": s.get("max_book_age_ms"),
            "taker_fok": bool(s.get("taker_fok", True)),
            "max_usd_per_trade": s.get("max_usd_per_trade"),
            "strategy_mode": strategy_mode_of(s),
            "favorite_min_price": s.get("favorite_min_price"),
            "favorite_max_price": s.get("favorite_max_price"),
            "favorite_maker": bool(s.get("favorite_maker")),
            "favorite_dir": s.get("favorite_dir") or "auto",
            "favorite_window_seconds": win,
            "favorite_window_label": favorite_window_label(win),
            "auto_redeem": bool(s.get("auto_redeem", True)),
            "circuit": rt.circuit_tripped(),
            "today_pnl": paper["today_pnl"] if paper is not None else rt.store.today_pnl(),
            "daily_loss_limit_usd": s.get("daily_loss_limit_usd"),
            "paper_equity": None if paper is None else paper["equity"],
        }

    @app.get("/", response_class=HTMLResponse)
    async def home(request: Request, t: str | None = Query(default=None)):
        if not _ok(request, t):
            return HTMLResponse(_gate(), status_code=401)
        html = PAGE.read_text(encoding="utf-8")
        resp = HTMLResponse(html)
        if t == token:
            resp.set_cookie("dash", token, httponly=True, samesite="lax")
        return resp

    @app.get("/api/state")
    async def state(request: Request, t: str | None = Query(default=None)):
        if not _ok(request, t):
            raise HTTPException(401, "unauthorized")
        return JSONResponse(rt.snapshot())

    @app.post("/api/action/{name}")
    async def action(
        name: str,
        request: Request,
        t: str | None = Query(default=None),
        amount: float | None = Query(default=None),
    ):
        if not _ok(request, t):
            raise HTTPException(401, "unauthorized")
        if name == "pause":
            rt.store.patch_settings(engine_running=False)
        elif name == "resume":
            rt.store.patch_settings(engine_running=True, killed=False)
        elif name == "kill":
            rt.store.patch_settings(killed=True, engine_running=False, live_trading=False)
            n = rt.store.cancel_all_resting("kill")
            live_n = 0
            try:
                live_n = await rt.broker().cancel_open_orders()
            except Exception as exc:
                rt.store.add_event("warn", f"dashboard kill cancel_live {type(exc).__name__}: {exc}"[:180])
            rt.store.add_event("warn", f"dashboard kill cancelled_resting={n} live={live_n}")
        elif name == "paper":
            rt.store.patch_settings(live_trading=False)
        elif name == "set_paper_cash":
            if amount is None:
                raise HTTPException(400, "amount required")
            cash = clamp_paper_cash(amount)
            rt.store.patch_settings(paper_starting_cash=cash)
            rt.store.add_event("info", f"paper bankroll set ${cash:.2f} (reset to apply)")
            return {"ok": True, "settings": rt.settings(), "paper": rt.store.paper_state()}
        elif name == "reset_paper":
            cash = clamp_paper_cash(amount if amount is not None else rt.paper_bankroll())
            rt.store.patch_settings(paper_starting_cash=cash)
            book = rt.store.reset_paper(cash)
            rt.store.add_event("warn", f"paper reset starting=${book['starting']:.2f}")
            return {"ok": True, "settings": rt.settings(), "paper": book}
        elif name == "clear_circuit":
            book = rt.store.reset_today_pnl()
            rt._circuit_latch = False
            rt.store.add_event("warn", f"dashboard cleared daily circuit equity=${book['equity']:.2f}")
            return {"ok": True, "settings": rt.settings(), "paper": book, "circuit": rt.circuit_tripped()}
        else:
            raise HTTPException(400, "unknown action")
        rt.store.add_event("info", f"dashboard {name}")
        return {"ok": True, "settings": rt.settings()}

    @app.get("/login")
    async def login(t: str = ""):
        if token and t == token:
            resp = RedirectResponse("/")
            resp.set_cookie("dash", token, httponly=True, samesite="lax")
            return resp
        return HTMLResponse(_gate(), status_code=401)

    return app


def _gate() -> str:
    return """<!doctype html><meta charset=utf-8><title>Surf</title>
    <body style="font-family:sans-serif;background:#f3ead7;padding:40px">
    <p>Dashboard 要 token。用 /?t=你的DASHBOARD_TOKEN 或者 Zeabur 變數。</p>
    </body>"""
