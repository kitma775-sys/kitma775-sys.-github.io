from __future__ import annotations

import os
from dataclasses import dataclass


DEFAULT_SETTINGS = {
    "engine_running": True,
    "auto_execute": True,
    "live_trading": False,
    "killed": False,
    "min_edge": 0.02,
    "max_usd_per_trade": 5.0,
    "min_shares": 5.0,
    "daily_loss_limit_usd": 50.0,
    "max_open_markets": 8,
    "max_imbalance_shares": 40.0,
    "poll_seconds": 2.0,
    "max_book_age_ms": 60000.0,
    "prefer_tail": True,
    "tail_confirm": 0.90,
    "stale_leg": 0.02,
    "maker_first": False,
    "auto_merge": True,
    "auto_redeem": True,
    "fee_rate": 0.07,
    "tag": "5M",
    "tags": ["5M"],
    "assets": ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"],
    "scan_limit": 40,
    "max_horizon_seconds": 3600.0,
    "notify_signals": True,
    "notify_rejects": False,
    "quote_cooldown_seconds": 5.0,
    "paper_slip_ticks": 0,
    "paper_starting_cash": 500.0,
    "strategy_rev": 44,
    "maker_min_leg": 0.22,
    "maker_max_skew": 0.10,
    "maker_window_seconds": 0.0,
    # Rev 26: TWAP-only. Complement / favorite stay in library tests, not the live engine.
    "maker_min_edge": 0.01,
    "taker_fok": True,
    "fok_delay_ms": 250.0,
    "strategy_mode": "twap",
    "favorite_min_price": 0.97,
    "favorite_max_price": 0.98,
    "favorite_window_seconds": 60.0,
    "favorite_maker": False,
    "favorite_dir": "auto",
    "twap_min_price": 0.45,
    "twap_max_price": 0.55,
    "twap_min_lead_bps": 6.0,
    "twap_min_edge": 0.04,
    "twap_min_left": 12.0,
    "twap_max_left": 280.0,
    "twap_max_spread": 0.04,
    "twap_scratch_p": 0.48,
    "twap_scratch_min_bid": 0.38,
    "twap_assets": ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge", "zec"],
    "twap_horizons": ["5m"],
    "twap_lookback": 60.0,
    "twap_rescore_seconds": 15.0,
    # Extra wait after FOK confirm, then re-walk the book (order RTT).
    "clob_rtt_ms": 150.0,
}


# 5m crypto up/down CLOB min is 5 shares. In the 45–55¢ TWAP band that is
# ~$2.34–$2.84 after taker fee, so $2 cannot fill. Telegram steps start at $3.
TRADE_USD_STEPS = (3.0, 5.0, 10.0, 15.0, 20.0, 25.0, 50.0, 100.0, 200.0, 500.0)


def nudge_trade_usd(cur, *, up: bool) -> float:
    """Move along TRADE_USD_STEPS. Values below $3 snap to $3."""
    try:
        x = float(cur)
    except (TypeError, ValueError):
        x = float(DEFAULT_SETTINGS["max_usd_per_trade"])
    steps = TRADE_USD_STEPS
    if up:
        for n in steps:
            if n > x + 1e-9:
                return float(n)
        return float(steps[-1])
    for n in reversed(steps):
        if n < x - 1e-9:
            return float(n)
    return float(steps[0])


SETTING_STEPS = {
    "max_usd_per_trade": (5.0, 3.0, 500.0),
    "min_shares": (1.0, 5.0, 50.0),
    "daily_loss_limit_usd": (10.0, 10.0, 1000.0),
    "max_open_markets": (1.0, 1.0, 30.0),
    "poll_seconds": (0.5, 1.0, 15.0),
    "paper_slip_ticks": (1.0, 0.0, 3.0),
    "paper_starting_cash": (100.0, 50.0, 100000.0),
    "scan_limit": (1.0, 8.0, 48.0),
    "twap_max_left": (10.0, 60.0, 280.0),
    "twap_min_lead_bps": (1.0, 2.0, 20.0),
    "clob_rtt_ms": (50.0, 0.0, 500.0),
}


def clamp_paper_cash(amount: float) -> float:
    return float(min(100000.0, max(50.0, round(float(amount), 2))))


def setting_num(s: dict, key: str, default: float) -> float:
    v = s.get(key)
    if v is None or v == "":
        return float(default)
    return float(v)


STRATEGY_MODES = ("twap",)


def strategy_mode_of(s: dict | None) -> str:
    """Live engine is TWAP-only. Leftover sqlite modes coerce to twap."""
    return "twap"


def favorite_window_of(s: dict | None) -> float:
    """Seconds left to allow a favorite fill. Missing → default 60. 0 = whole book."""
    if not s:
        return float(DEFAULT_SETTINGS["favorite_window_seconds"])
    return setting_num(s, "favorite_window_seconds", float(DEFAULT_SETTINGS["favorite_window_seconds"]))


def favorite_window_label(seconds) -> str:
    try:
        win = float(seconds)
    except (TypeError, ValueError):
        win = float(DEFAULT_SETTINGS["favorite_window_seconds"])
    if win <= 0:
        return "全段（完場前3秒）"
    return f"尾 {win:.0f}s"


def format_leg_prices(up, down, *, leg: str | None = None) -> str:
    """One-leg favorite is `Up 0.90`, not `0.90+0.0`."""
    try:
        up_px = float(up or 0)
    except (TypeError, ValueError):
        up_px = 0.0
    try:
        dn_px = float(down or 0)
    except (TypeError, ValueError):
        dn_px = 0.0
    side = str(leg or "").strip().lower()
    if side == "up" or (dn_px < 0.01 and up_px >= 0.01):
        return f"Up {up_px}"
    if side == "down" or (up_px < 0.01 and dn_px >= 0.01):
        return f"Down {dn_px}"
    return f"{up_px}+{dn_px}"


def format_shares(shares) -> str:
    try:
        n = float(shares or 0)
    except (TypeError, ValueError):
        n = 0.0
    return f"{n:.2f}"


def format_share_qty(shares) -> str:
    """18.9576 → '18.96股'. Never round to a bare 19 that looks like $19."""
    return f"{format_shares(shares)}股"


def format_fill_headline(*, up, down, shares, cost=None, leg: str | None = None) -> str:
    line = f"{format_leg_prices(up, down, leg=leg)} × {format_share_qty(shares)}"
    try:
        if cost is not None and float(cost) > 0:
            line += f" · 成本 ${float(cost):.2f}"
    except (TypeError, ValueError):
        pass
    return line


def is_favorite_inventory(kind) -> bool:
    return str(kind or "").startswith("favorite")


def is_directional_inventory(kind) -> bool:
    k = str(kind or "")
    return k.startswith("favorite") or k.startswith("twap")


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
    wallet: str = ""
    clob_api_key: str = ""
    clob_secret: str = ""
    clob_passphrase: str = ""
    force_paper: bool = False
    paper_starting_cash: float = 500.0
    dashboard_public_url: str = ""


def normalize_private_key(raw: str) -> str:
    pk = (raw or "").strip()
    if pk and not pk.startswith("0x"):
        pk = "0x" + pk
    return pk


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
        private_key=normalize_private_key(os.getenv("POLYMARKET_PRIVATE_KEY", "")),
        wallet=os.getenv("POLYMARKET_WALLET", "").strip(),
        clob_api_key=os.getenv("CLOB_API_KEY", "").strip(),
        clob_secret=os.getenv("CLOB_SECRET", "").strip(),
        clob_passphrase=os.getenv("CLOB_PASSPHRASE", "").strip(),
        force_paper=os.getenv("FORCE_PAPER", "false").lower() in {"1", "true", "yes"},
        paper_starting_cash=float(os.getenv("PAPER_STARTING_CASH", "500") or 500),
        dashboard_public_url=(
            os.getenv("DASHBOARD_PUBLIC_URL", "").strip().rstrip("/")
            or "https://surf-arb.zeabur.app"
        ),
    )


def live_keys_ready(env: Env) -> bool:
    return bool(env.private_key)


def is_live_inventory_kind(kind) -> bool:
    """True for on-chain inventory (`twap_live`, `favorite_live`)."""
    return str(kind or "").endswith("_live")


def inventory_matches_mode(kind, *, live: bool) -> bool:
    return is_live_inventory_kind(kind) is bool(live)


def live_switch_blockers(env: Env, geo: dict | None = None) -> list[str]:
    """Why Telegram cannot arm live. Does not enable trading by itself."""
    out: list[str] = []
    if env.force_paper:
        out.append("FORCE_PAPER")
    if not live_keys_ready(env):
        out.append("no_key")
    status = str((geo or {}).get("api_status") or "")
    if status == "full_block":
        out.append("geo_full_block")
    elif status == "close_only":
        out.append("geo_close_only")
    return out


LIVE_BLOCKER_ZH = {
    "FORCE_PAPER": "FORCE_PAPER 開緊",
    "no_key": "未設定 POLYMARKET_PRIVATE_KEY",
    "geo_full_block": "IP 所在地官方 API 全封鎖，唔會開實盤",
    "geo_close_only": "IP 所在地官方 API close-only，新倉會被拒",
}
