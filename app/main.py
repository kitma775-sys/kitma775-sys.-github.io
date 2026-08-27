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
    """Patch live sqlite up to rev 18. Does not reset the paper ledger."""
    rev = int(store.settings().get("strategy_rev") or 0)
    n = 0
    if rev < 6:
        n = store.cancel_all_resting("strategy_rev6")
        store.patch_settings(
            strategy_rev=6,
            maker_first=False,
            maker_min_edge=0.01,
            maker_window_seconds=0.0,
            maker_max_skew=0.10,
            quote_cooldown_seconds=5.0,
            tags=["15M", "1H"],
            assets=["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"],
            scan_limit=16,
            max_horizon_seconds=3600.0,
        )
        if n:
            store.add_event("info", f"rev6 cancelled {n} resting maker quotes")
    if rev < 7:
        store.patch_settings(
            strategy_rev=7,
            maker_first=False,
            maker_window_seconds=0.0,
            max_book_age_ms=60000.0,
        )
        store.add_event("info", "rev7 ws book hold 60s + near-expiry HTTP")
    if rev < 8:
        store.patch_settings(
            strategy_rev=8,
            maker_first=False,
            maker_window_seconds=0.0,
            max_book_age_ms=60000.0,
            tags=["5M", "15M", "1H"],
            scan_limit=24,
        )
        store.add_event("info", "rev8 5m windows + two-ask ranking; maker still off")
    if rev < 9:
        store.patch_settings(
            strategy_rev=9,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            fok_delay_ms=250.0,
        )
        store.add_event("info", "rev9 pair FOK: 250ms delay then both legs full size or kill")
    if rev < 10:
        store.patch_settings(
            strategy_rev=10,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            fok_delay_ms=250.0,
            quote_cooldown_seconds=5.0,
        )
        store.add_event(
            "info",
            "rev10 FAK leftover +EV size at snapshot limits, else requote delayed book; kill cooldown 0.4s",
        )
    if rev < 11:
        store.patch_settings(
            strategy_rev=11,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            strategy_mode="auto",
            favorite_min_price=0.95,
            favorite_max_price=0.99,
            favorite_window_seconds=30.0,
            favorite_maker=True,
        )
        store.add_event(
            "info",
            "rev11 auto: complement first, else last-30s 95-99¢ favorite; 95¢ bid optional; hold to settle",
        )
    if rev < 12:
        store.patch_settings(
            strategy_rev=12,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
        )
        store.add_event(
            "info",
            "rev12 favorite 97¢ rest no longer blocks lifting 97-99¢; WS checks trade-through; no last-2min HTTP spam",
        )
    if rev < 13:
        store.patch_settings(
            strategy_rev=13,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_dir="auto",
            favorite_min_price=0.95,
            favorite_max_price=0.99,
            favorite_window_seconds=0,
        )
        store.add_event(
            "info",
            "rev13 try full-session 95-99¢ favorite; dir auto/up/down; Telegram 尾窗 cycle includes 全段",
        )
    if rev < 14:
        store.patch_settings(
            strategy_rev=14,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
        )
        store.add_event(
            "info",
            "rev14 cap favorite stacking at max_usd_per_trade per market; circuit still scans tape",
        )
    if rev < 15:
        store.patch_settings(
            strategy_rev=15,
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.90,
            favorite_max_price=0.99,
        )
        store.add_event(
            "info",
            "rev15 open favorite band to 90-99¢; keep current tail window; no paper reset",
        )
    if rev < 16:
        store.patch_settings(
            strategy_rev=16,
            auto_redeem=True,
        )
        store.add_event(
            "info",
            "rev16 auto-redeem resolved inventory (paper credit / live redeemPositions); keep band and paper",
        )
    if rev < 17:
        store.patch_settings(
            strategy_rev=17,
            strategy_mode="favorite",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.90,
            favorite_max_price=0.98,
            max_usd_per_trade=5.0,
        )
        store.add_event(
            "info",
            "rev17 favorite-only 90-98¢ at $5; complement two-ask off; keep window and paper; no live",
        )
    if rev < 18:
        store.patch_settings(
            strategy_rev=18,
            strategy_mode="favorite",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.90,
            favorite_max_price=0.98,
            favorite_window_seconds=180.0,
            max_usd_per_trade=5.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev18 pre-live align: pin favorite window 180s; same 90-98¢ $5; keep paper; no live",
        )
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
