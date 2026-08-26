from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_SETTINGS = {
    "engine_running": True,
    "auto_execute": True,
    "live_trading": False,
    "killed": False,
    "min_edge": 0.02,
    "max_usd_per_trade": 25.0,
    "min_shares": 5.0,
    "daily_loss_limit_usd": 50.0,
    "max_open_markets": 8,
    "max_imbalance_shares": 40.0,
    "poll_seconds": 2.0,
    "max_book_age_ms": 2000.0,
    "prefer_tail": True,
    "tail_confirm": 0.90,
    "stale_leg": 0.02,
    "maker_first": False,
    "auto_merge": True,
    "fee_rate": 0.07,
    "tag": "15M",
    "tags": ["15M", "1H"],
    "assets": ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"],
    "scan_limit": 16,
    "max_horizon_seconds": 3600.0,
    "notify_signals": True,
    "notify_rejects": False,
    "quote_cooldown_seconds": 5.0,
    "paper_slip_ticks": 0,
    "paper_starting_cash": 500.0,
    "strategy_rev": 6,
    "maker_min_leg": 0.22,
    "maker_max_skew": 0.10,
    "maker_window_seconds": 0.0,
    # Rev 6: no resting maker. 6h tape had 0 two-sided fills and −EV one-sided
    # hedges. Taker min_edge stays 0.02; hunt only when both books are fresh.
    "maker_min_edge": 0.01,
}


SETTING_STEPS = {
    "min_edge": (0.005, 0.005, 0.08),
    "maker_min_edge": (0.005, 0.005, 0.08),
    "max_usd_per_trade": (5.0, 5.0, 500.0),
    "min_shares": (1.0, 5.0, 50.0),
    "daily_loss_limit_usd": (10.0, 10.0, 1000.0),
    "max_open_markets": (1.0, 1.0, 30.0),
    "max_imbalance_shares": (5.0, 5.0, 500.0),
    "poll_seconds": (0.5, 1.0, 15.0),
    "maker_window_seconds": (5.0, 0.0, 120.0),
    "tail_confirm": (0.01, 0.80, 0.98),
    "stale_leg": (0.005, 0.005, 0.10),
    "fee_rate": (0.01, 0.0, 0.12),
    "paper_slip_ticks": (1.0, 0.0, 3.0),
    "paper_starting_cash": (100.0, 50.0, 100000.0),
    "scan_limit": (1.0, 8.0, 32.0),
}


def clamp_paper_cash(amount: float) -> float:
    return float(min(100000.0, max(50.0, round(float(amount), 2))))


def setting_num(s: dict, key: str, default: float) -> float:
    v = s.get(key)
    if v is None or v == "":
        return float(default)
    return float(v)


@dataclass
class Env:
    telegram_token: str = ""
    telegram_owner_id: int | None = None
    dashboard_token: str = ""
    port: int = 8080
    data_dir: str = "./data"
    trading_mode: str = "paper"
    engine_autostart: bool = True
    private_key: str = ""
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_passphrase: str = ""
    force_paper: bool = False
    paper_starting_cash: float = 500.0


def load_env() -> Env:
    owner = (
        os.getenv("TELEGRAM_OWNER_ID", "").strip()
        or os.getenv("TELEGRAM_CHAT_ID", "").strip()
    )
    return Env(
        telegram_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_owner_id=int(owner) if owner.isdigit() else None,
        dashboard_token=os.getenv("DASHBOARD_TOKEN", "").strip(),
        port=int(os.getenv("PORT", "8080")),
        data_dir=os.getenv("DATA_DIR", "./data"),
        trading_mode=os.getenv("TRADING_MODE", "paper").strip().lower(),
        engine_autostart=os.getenv("ENGINE_AUTOSTART", "true").lower() in {"1", "true", "yes"},
        private_key=os.getenv("POLYMARKET_PRIVATE_KEY", "").strip(),
        clob_api_key=os.getenv("CLOB_API_KEY", "").strip(),
        clob_secret=os.getenv("CLOB_SECRET", "").strip(),
        clob_passphrase=os.getenv("CLOB_PASSPHRASE", "").strip(),
        force_paper=os.getenv("FORCE_PAPER", "false").lower() in {"1", "true", "yes"},
        paper_starting_cash=float(os.getenv("PAPER_STARTING_CASH", "500") or 500),
    )


def live_keys_ready(env: Env) -> bool:
    return bool(env.private_key)
