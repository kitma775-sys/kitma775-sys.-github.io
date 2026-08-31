from __future__ import annotations

import asyncio
import os
import secrets
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from app.config import clamp_paper_cash, load_env, live_keys_ready
from app.dashboard import create_app
from app.runtime import Runtime, engine_loop
from app.store import Store
from app.telegram_ui import run_telegram


def apply_strategy_rev(store: Store) -> int:
    """Patch live sqlite up to rev 29. Does not reset the paper ledger."""
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
    if rev < 19:
        store.patch_settings(
            strategy_rev=19,
            strategy_mode="favorite",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.97,
            favorite_max_price=0.98,
            favorite_window_seconds=180.0,
            favorite_maker=False,
            max_usd_per_trade=5.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev19 wait for official 0/1 before redeem; favorite 97-98¢ $5 taker-only; keep window and paper; no live",
        )
    if rev < 20:
        store.patch_settings(
            strategy_rev=20,
            strategy_mode="favorite",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.97,
            favorite_max_price=0.98,
            favorite_window_seconds=60.0,
            favorite_maker=False,
            max_usd_per_trade=5.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev20 last 60s locked 97-98¢; skip if other ask >=10¢ or same 5m already filled; keep paper; no live",
        )
    if rev < 21:
        store.patch_settings(
            strategy_rev=21,
            strategy_mode="favorite",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_min_price=0.97,
            favorite_max_price=0.98,
            favorite_window_seconds=60.0,
            favorite_maker=False,
            max_usd_per_trade=5.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev21 true-top 97-98 lock; skip leftover 97 after 99¢ bid; WS-only hunt; pin $5; keep paper; no live",
        )
    if rev < 22:
        store.patch_settings(
            strategy_rev=22,
            strategy_mode="complement",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            min_edge=0.02,
            favorite_min_price=0.97,
            favorite_max_price=0.98,
            favorite_window_seconds=60.0,
            favorite_maker=False,
            max_usd_per_trade=5.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev22 stop favorite 97-98 hunt; complement-only min_edge 0.02 FOK maker-off; keep paper; no live",
        )
    if rev < 23:
        store.patch_settings(
            strategy_rev=23,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=120.0,
            twap_scratch_p=0.48,
            twap_assets=["btc"],
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev23 Chainlink 60s TWAP mid-band BTC 5m + scratch; complement still first; keep paper; no live",
        )
    if rev < 24:
        store.patch_settings(
            strategy_rev=24,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=180.0,
            twap_scratch_p=0.48,
            twap_assets=["btc"],
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev24 TWAP window 180s left (copy top directional timing); keep 6bps+scratch; no pair-lock taker; keep paper; no live",
        )
    if rev < 25:
        store.patch_settings(
            strategy_rev=25,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=180.0,
            twap_scratch_p=0.48,
            twap_assets=["btc"],
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev25 paper fill = live CLOB FAK (BUY USDC amount, SELL scratch, RTT re-walk); keep paper; no live",
        )
    if rev < 26:
        store.patch_settings(
            strategy_rev=26,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=180.0,
            twap_scratch_p=0.48,
            twap_assets=["btc"],
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev26 TWAP-only: no complement, no favorite; keep paper; no live",
        )
    if rev < 27:
        store.patch_settings(
            strategy_rev=27,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_assets=["btc", "eth"],
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev27 TWAP earlier window 280s + ETH 5m; keep 45-55 6bps scratch; no pair-lock; keep paper; no live",
        )
    if rev < 28:
        from app.twap import CHAINLINK_ASSETS, DEFAULT_TWAP_HORIZONS

        store.patch_settings(
            strategy_rev=28,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_assets=list(CHAINLINK_ASSETS),
            twap_horizons=list(DEFAULT_TWAP_HORIZONS),
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev28 settlement allowlist: 5m+15m Chainlink TWAP-60 any feed coin; 1H Binance never; scan tags/assets unchanged; keep paper; no live",
        )
    if rev < 29:
        cur = store.settings()
        try:
            scan_n = int(float(cur.get("scan_limit") or 24))
        except (TypeError, ValueError):
            scan_n = 24
        store.patch_settings(
            strategy_rev=29,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            scan_limit=max(scan_n, 40),
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev29 per-symbol Chainlink sockets + scan 40 + TWAP-window rank; no all-coin same-clock lock; keep 6bps scratch; keep paper; no live",
        )
    if rev < 30:
        store.patch_settings(
            strategy_rev=30,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev30 CLOB WS hunt-window+inventory only; honest twap_gate; persist PTB; keep 6bps scratch; keep paper; no live",
        )
    if rev < 31:
        store.patch_settings(
            strategy_rev=31,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev31 CLOB WS cap 16 + PTB required; skip HTTP next-window; keep 6bps scratch; keep paper; no live",
        )
    if rev < 32:
        store.patch_settings(
            strategy_rev=32,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev32 two CLOB sockets x8, no initial_dump, cap 14; keep 6bps scratch; keep paper; no live",
        )
    if rev < 33:
        store.patch_settings(
            strategy_rev=33,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev33 CLOB slots prefer 45-55 outcomePrices over locked 5m pennies; keep 6bps scratch; keep paper; no live",
        )
    if rev < 34:
        store.patch_settings(
            strategy_rev=34,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            tag="5M",
            tags=["5M"],
            twap_horizons=["5m"],
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev34 5m-only multi-coin Chainlink TWAP; drop 15m/1H scan; keep 6bps scratch; keep paper; no live",
        )
    if rev < 35:
        store.patch_settings(
            strategy_rev=35,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            tag="5M",
            tags=["5M"],
            twap_horizons=["5m"],
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev35 CLOB pre-warms next 5m in last 45s; drop locked-penny slots and leftover 15m PTB; keep 6bps scratch; keep paper; no live",
        )
    if rev < 36:
        store.patch_settings(
            strategy_rev=36,
            strategy_mode="twap",
            maker_first=False,
            maker_window_seconds=0.0,
            taker_fok=True,
            favorite_maker=False,
            min_edge=0.02,
            max_usd_per_trade=5.0,
            twap_min_price=0.45,
            twap_max_price=0.55,
            twap_min_lead_bps=6.0,
            twap_min_edge=0.04,
            twap_min_left=12.0,
            twap_max_left=280.0,
            twap_scratch_p=0.48,
            twap_lookback=60.0,
            twap_rescore_seconds=15.0,
            clob_rtt_ms=150.0,
            tag="5M",
            tags=["5M"],
            twap_horizons=["5m"],
            live_trading=False,
        )
        store.add_event(
            "info",
            "rev36 keep locked 5m on CLOB WS until next-window prewarm needs the cap; stop mid-window reconnect storm; keep 6bps scratch; keep paper; no live",
        )
    if rev < 37:
        store.patch_settings(strategy_rev=37)
        store.add_event(
            "info",
            "rev37 live-ready: persist TG live across restart when keys ready; isolate paper vs live inventory; FAK actual fill size; live circuit uses live dump/settle",
        )
    if rev < 38:
        store.patch_settings(strategy_rev=38)
        store.add_event(
            "info",
            "rev38 TWAP scratch uses the event map again (do not overwrite it with the live bool); paper leftover stays isolated; keep stake and 6bps",
        )
    if rev < 39:
        store.patch_settings(strategy_rev=39)
        store.add_event(
            "info",
            "rev39 live preflight: Gnosis Safe CLOB FAK does not need Builder API key; skip perps/auto-redeem extras; keep stake and 6bps",
        )
    if rev < 40:
        store.patch_settings(strategy_rev=40)
        store.add_event(
            "info",
            "rev40 CLOB 503/trading-is-disabled: halt live posts ~90s, notify once, keep scanning; keep stake and 6bps",
        )
    if rev < 41:
        store.patch_settings(strategy_rev=41)
        store.add_event(
            "info",
            "rev41 live: leftover paper settles silently into the paper book; paper leftover does not block live TWAP; keep stake and 6bps",
        )
    return n


def clamp_live_at_boot(store: Store, env) -> None:
    """Never auto-enable live. Only force paper when the operator locked it or keys are missing.

    TRADING_MODE=paper is the default *until* Telegram two-step sets sqlite live_trading.
    A restart must not wipe that confirm, or the user cannot just flip TG.
    """
    if env.force_paper or not live_keys_ready(env):
        store.patch_settings(live_trading=False)


def run() -> None:
    env = load_env()
    Path(env.data_dir).mkdir(parents=True, exist_ok=True)
    if not env.dashboard_token:
        env.dashboard_token = os.getenv("DASHBOARD_TOKEN") or secrets.token_urlsafe(12)
        print(f"DASHBOARD_TOKEN={env.dashboard_token}", flush=True)
    store = Store(Path(env.data_dir) / "surf.sqlite")
    clamp_live_at_boot(store, env)
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
