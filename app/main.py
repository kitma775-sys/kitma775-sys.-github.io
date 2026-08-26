from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.config import clamp_paper_cash, load_env
from app.dashboard import create_app
from app.runtime import Runtime, engine_loop
from app.store import Store
from app.telegram_ui import run_telegram


def apply_strategy_rev(store: Store) -> int:
    """Patch live sqlite up to rev 6. Does not reset the paper ledger."""
    if int(store.settings().get("strategy_rev") or 0) >= 6:
        return 0
    n = store.cancel_all_resting("strategy_rev6")
    store.patch_settings(
        strategy_rev=6,
        maker_first=False,
        maker_min_edge=0.01,
        maker_window_seconds=0.0,
        maker_max_skew=0.10,
        quote_cooldown_seconds=5.0,
        max_book_age_ms=2000.0,
        tags=["15M", "1H"],
        assets=["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"],
        scan_limit=16,
        max_horizon_seconds=3600.0,
    )
    if n:
        store.add_event("info", f"rev6 cancelled {n} resting maker quotes")
    return n


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
    seed = clamp_paper_cash(env.paper_starting_cash)
    if not store.paper_exists():
        store.patch_settings(paper_starting_cash=seed)
    planned = float(store.settings().get("paper_starting_cash") or seed)
    paper = store.ensure_paper(planned)
    apply_strategy_rev(store)
    pruned = store.prune_empty_inventory()
    if pruned:
        store.add_event("info", f"pruned {pruned} empty inventory rows")
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
