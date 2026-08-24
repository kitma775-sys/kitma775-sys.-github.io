from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.config import load_env
from app.dashboard import create_app
from app.runtime import Runtime, engine_loop
from app.store import Store
from app.telegram_ui import run_telegram


def run() -> None:
    env = load_env()
    Path(env.data_dir).mkdir(parents=True, exist_ok=True)
    if not env.dashboard_token:
        env.dashboard_token = os.getenv("DASHBOARD_TOKEN") or secrets.token_urlsafe(12)
        print(f"DASHBOARD_TOKEN={env.dashboard_token}", flush=True)
    if env.trading_mode == "paper":
        # keep live_trading false at boot even if sqlite leftover from a previous experiment
        pass
    store = Store(Path(env.data_dir) / "surf.sqlite")
    if env.trading_mode != "live" or env.force_paper or not env.private_key:
        store.patch_settings(live_trading=False)
    if not env.engine_autostart:
        store.patch_settings(engine_running=False)
    paper = store.ensure_paper(env.paper_starting_cash)
    rt = Runtime(store, env)
    store.add_event(
        "info",
        f"boot mode={rt.mode()} port={env.port} paper_start=${paper['starting']:.2f} cash=${paper['cash']:.2f}",
    )
    asyncio.run(_serve(rt))


async def _serve(rt: Runtime) -> None:
    import uvicorn

    app = create_app(rt)
    config = uvicorn.Config(app, host="0.0.0.0", port=rt.env.port, log_level="info")
    server = uvicorn.Server(config)
    await asyncio.gather(
        server.serve(),
        engine_loop(rt),
        run_telegram(rt),
    )


if __name__ == "__main__":
    try:
        run()
    except KeyboardInterrupt:
        sys.exit(0)
