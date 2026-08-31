"""Chainlink 60s TWAP vs window-open PTB — mid-band directional engine.

Live ticks must be the same Chainlink USD stream the market settles on.
Never subtract a Binance/USDT print from Gamma priceToBeat (≈9 bps basis).
PTB is the first same-source tick at/after the window open.

Settlement (5m up/down): last `lookback` seconds of Chainlink TWAP >= PTB → Up.
15m uses the same oracle but collides with 5m for 14 CLOB slots and has no tape grid.
Hourly `*-up-or-down-*` and `*-above-*` settle on Binance candles — never this engine.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass

from app.fees import taker_fee

TWAP_LOOKBACK = 60
MID_LO = 0.45
MID_HI = 0.55
BTC_5M_RE = re.compile(r"^btc-updown-5m-(\d+)$")
UPDOWN_RE = re.compile(r"^([a-z0-9]+)-updown-(5m|15m)-(\d+)$")
HORIZON_SECONDS = {"5m": 300, "15m": 900}
TAG_TO_HORIZON = {"5M": "5m", "15M": "15m"}
CHAINLINK_SYMBOL = {
    "btc": "btc/usd",
    "eth": "eth/usd",
    "sol": "sol/usd",
    "xrp": "xrp/usd",
    "doge": "doge/usd",
    "bnb": "bnb/usd",
    "hype": "hype/usd",
    "zec": "zec/usd",
}
CHAINLINK_ASSETS = tuple(CHAINLINK_SYMBOL)
WINDOW_SECONDS = 300
DEFAULT_TWAP_ASSETS = ("btc", "eth")
DEFAULT_TWAP_HORIZONS = ("5m",)


def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0)))


def fair_p_up(lead_bps: float, vol_bps_sqrt_s: float | None, left_s: float, *, lookback: int = TWAP_LOOKBACK) -> float | None:
    """P(settlement TWAP stays on the current side of PTB) under a BM approx.

    After the settlement window starts (left <= lookback) the remaining average
    is shorter. Before that, uncertainty is the walk until the TWAP window plus
    the window itself — cap tau so a 2 bps lead at t+20s is not treated as 87%.
    """
    if vol_bps_sqrt_s is None or vol_bps_sqrt_s <= 1e-9 or left_s <= 0:
        return None
    lb = max(int(lookback), 1)
    if left_s <= lb:
        tau = max(float(left_s), 1.0)
    else:
        tau = float(left_s - lb) + 0.5 * lb
        tau = min(max(tau, 8.0), 180.0)
    return round(phi(float(lead_bps) / (float(vol_bps_sqrt_s) * math.sqrt(tau))), 6)


def lead_bps(twap: float, ptb: float) -> float | None:
    if ptb is None or twap is None or float(ptb) <= 0 or float(twap) <= 0:
        return None
    return (float(twap) - float(ptb)) / float(ptb) * 10000.0


def fee_per_share(px: float, fee_rate: float) -> float:
    p = min(max(float(px), 0.0), 1.0)
    return float(fee_rate) * p * (1.0 - p)


@dataclass(frozen=True)
class TwapWindow:
    asset: str
    horizon: str
    start: int

    @property
    def window_seconds(self) -> int:
        return int(HORIZON_SECONDS[self.horizon])

    @property
    def symbol(self) -> str | None:
        return CHAINLINK_SYMBOL.get(self.asset)

    @property
    def slug(self) -> str:
        return f"{self.asset}-updown-{self.horizon}-{self.start}"


def parse_window(slug: str) -> TwapWindow | None:
    """Only 5m/15m `{asset}-updown-{horizon}-{start}` with a live Chainlink feed.

    Hourly `bitcoin-up-or-down-…` / `*-above-*` return None (Binance candle).
    """
    m = UPDOWN_RE.match(str(slug or "").strip().lower())
    if not m:
        return None
    asset, horizon, start_s = m.group(1), m.group(2), m.group(3)
    if asset not in CHAINLINK_SYMBOL or horizon not in HORIZON_SECONDS:
        return None
    return TwapWindow(asset=asset, horizon=horizon, start=int(start_s))


def parse_5m(slug: str) -> tuple[str, int] | None:
    parsed = parse_window(slug)
    if not parsed or parsed.horizon != "5m":
        return None
    return parsed.asset, parsed.start


def is_btc_5m(slug: str) -> bool:
    return bool(BTC_5M_RE.match(str(slug or "")))


def is_hourly_updown(slug: str) -> bool:
    text = str(slug or "").lower()
    return "up-or-down" in text and "-updown-" not in text


def asset_from_symbol(symbol: str) -> str | None:
    key = str(symbol or "").strip().lower()
    for asset, sym in CHAINLINK_SYMBOL.items():
        if key == sym:
            return asset
    return None


def _token_tuple(raw, fallback: tuple[str, ...]) -> tuple[str, ...]:
    if raw is None or raw == "":
        return fallback
    if isinstance(raw, str):
        items = [a.strip().lower() for a in raw.split(",") if a.strip()]
    else:
        items = [str(a).lower().strip() for a in raw if str(a).strip()]
    return tuple(items) or fallback


def hunt_assets(s: dict | None = None) -> tuple[str, ...]:
    """Telegram scan coins ∩ Chainlink TWAP-60 allowlist.

    `twap_assets` is the capability pin (what the engine may trade).
    `assets` is what Telegram opened. Opening SOL only hunts SOL if both match.
    """
    d = s or {}
    pinned = set(_token_tuple(d.get("twap_assets"), DEFAULT_TWAP_ASSETS))
    scan_raw = d.get("assets")
    scan = _token_tuple(scan_raw, tuple(pinned)) if scan_raw not in (None, "") else tuple(pinned)
    return tuple(a for a in scan if a in CHAINLINK_SYMBOL and a in pinned)


def hunt_horizons(s: dict | None = None) -> tuple[str, ...]:
    """5M tag ∩ pinned TWAP horizons. Rev 34+ pins 5m-only. 1H never hunts."""
    d = s or {}
    pinned = set(_token_tuple(d.get("twap_horizons"), DEFAULT_TWAP_HORIZONS))
    tags = d.get("tags")
    if tags is None or tags == "":
        tag_list = [d.get("tag") or "5M"]
    elif isinstance(tags, str):
        tag_list = [t.strip() for t in tags.split(",") if t.strip()]
    else:
        tag_list = [str(t).strip() for t in tags if str(t).strip()]
    out: list[str] = []
    for tag in tag_list:
        horizon = TAG_TO_HORIZON.get(str(tag).upper())
        if horizon and horizon in pinned and horizon in HORIZON_SECONDS and horizon not in out:
            out.append(horizon)
    return tuple(out)


def chainlink_symbols_for(s: dict | None = None, extra_assets: tuple[str, ...] = ()) -> tuple[str, ...]:
    assets = list(hunt_assets(s))
    for raw in extra_assets:
        a = str(raw or "").lower()
        if a in CHAINLINK_SYMBOL and a not in assets:
            assets.append(a)
    return tuple(CHAINLINK_SYMBOL[a] for a in assets if a in CHAINLINK_SYMBOL) or ("btc/usd",)


def future_listing(left: float | None, window_seconds: int) -> bool:
    """Skip books listed for the next window, not the live clock."""
    if left is None:
        return False
    return float(left) > float(window_seconds) + 5.0


def in_mid_band(px: float, lo: float = MID_LO, hi: float = MID_HI) -> bool:
    return float(lo) - 1e-12 <= float(px) <= float(hi) + 1e-12


@dataclass(frozen=True)
class TwapSnap:
    symbol: str
    slug: str
    asset: str
    start: int
    ptb: float
    twap: float
    spot: float
    lead_bps: float
    vol_bps_sqrt_s: float | None
    fair_p_up: float | None
    lookback: int
    age_ms: float
    tick_n: int
    connected: bool

    @property
    def side(self) -> str:
        return "up" if self.lead_bps >= 0 else "down"

    @property
    def fair_p_side(self) -> float | None:
        if self.fair_p_up is None:
            return None
        return self.fair_p_up if self.side == "up" else round(1.0 - self.fair_p_up, 6)


@dataclass(frozen=True)
class TwapParams:
    min_price: float = MID_LO
    max_price: float = MID_HI
    min_lead_bps: float = 6.0
    min_edge: float = 0.04
    min_left: float = 12.0
    max_left: float = 280.0
    max_spread: float = 0.04
    max_age_ms: float = 3000.0
    min_ticks: int = 20
    lookback: int = TWAP_LOOKBACK
    scratch_p: float = 0.48
    scratch_min_bid: float = 0.38
    scratch_left_min: float = 8.0
    assets: tuple[str, ...] = DEFAULT_TWAP_ASSETS
    horizons: tuple[str, ...] = DEFAULT_TWAP_HORIZONS


def default_params(s: dict | None = None) -> TwapParams:
    d = s or {}

    def num(key: str, fallback: float) -> float:
        v = d.get(key)
        if v is None or v == "":
            return float(fallback)
        return float(v)

    assets = hunt_assets(d) or DEFAULT_TWAP_ASSETS
    horizons = hunt_horizons(d)
    if not horizons and d.get("twap_horizons") is None and d.get("tags") is None:
        horizons = DEFAULT_TWAP_HORIZONS
    return TwapParams(
        min_price=num("twap_min_price", MID_LO),
        max_price=num("twap_max_price", MID_HI),
        min_lead_bps=num("twap_min_lead_bps", 6.0),
        min_edge=num("twap_min_edge", 0.04),
        min_left=num("twap_min_left", 12.0),
        max_left=num("twap_max_left", 280.0),
        max_spread=num("twap_max_spread", 0.04),
        max_age_ms=num("twap_max_age_ms", 3000.0),
        min_ticks=int(num("twap_min_ticks", 20)),
        lookback=int(num("twap_lookback", TWAP_LOOKBACK)),
        scratch_p=num("twap_scratch_p", 0.48),
        scratch_min_bid=num("twap_scratch_min_bid", 0.38),
        scratch_left_min=num("twap_scratch_left_min", 8.0),
        assets=assets,
        horizons=horizons,
    )


def slug_allowed(slug: str, params: TwapParams) -> bool:
    parsed = parse_window(slug)
    if not parsed:
        return False
    if parsed.asset not in set(params.assets):
        return False
    return parsed.horizon in set(params.horizons)


def entry_edge(fair_p: float, px: float, fee_rate: float) -> float:
    return float(fair_p) - float(px) - fee_per_share(px, fee_rate)


def twap_entry_reason(
    *,
    slug: str,
    snap: TwapSnap | None,
    ask: float | None,
    bid: float | None,
    left: float | None,
    fee_rate: float,
    params: TwapParams,
) -> str | None:
    """None means enter. Otherwise a stable skip reason."""
    if snap is None or not snap.connected:
        return "twap_no_feed"
    parsed = parse_window(slug)
    if parsed is None or not slug_allowed(slug, params):
        if parsed is None:
            return "twap_oracle"
        if parsed.horizon not in set(params.horizons):
            return "twap_horizon"
        return "twap_asset"
    if snap.age_ms > params.max_age_ms:
        return "twap_stale"
    if snap.tick_n < params.min_ticks:
        return "twap_thin"
    if snap.ptb <= 0 or snap.twap <= 0:
        return "twap_no_ptb"
    if left is None or left < params.min_left or left > params.max_left:
        return "twap_window"
    if ask is None or not in_mid_band(ask, params.min_price, params.max_price):
        return "twap_band"
    if bid is None:
        return "twap_no_bid"
    if ask + 1e-12 < bid:
        return "twap_crossed"
    if (ask - bid) > params.max_spread + 1e-12:
        return "twap_wide"
    if abs(snap.lead_bps) < params.min_lead_bps - 1e-12:
        return "twap_lead"
    fair = snap.fair_p_side
    if fair is None:
        return "twap_no_fair"
    if entry_edge(fair, ask, fee_rate) + 1e-12 < params.min_edge:
        return "twap_edge"
    return None


def hold_value(shares: float, fair_p: float) -> float:
    return float(shares) * float(fair_p)


def scratch_proceeds(shares: float, bid: float, fee_rate: float) -> float:
    return max(0.0, float(shares) * float(bid) - taker_fee(shares, bid, fee_rate))


def should_scratch(
    *,
    fair_p: float | None,
    lead_bps_signed: float | None,
    bid: float | None,
    shares: float,
    fee_rate: float,
    left: float | None,
    params: TwapParams,
) -> tuple[bool, str]:
    if left is None or left < params.scratch_left_min:
        return False, "twap_scratch_late"
    if bid is None or float(bid) + 1e-12 < params.scratch_min_bid:
        return False, "twap_scratch_no_bid"
    if fair_p is None:
        return True, "twap_scratch_no_fair"
    proceeds = scratch_proceeds(shares, bid, fee_rate)
    held = hold_value(shares, fair_p)
    if proceeds + 1e-9 >= held:
        return True, "twap_scratch_better"
    if float(fair_p) < params.scratch_p:
        return True, "twap_scratch_weak"
    if lead_bps_signed is not None and float(lead_bps_signed) < 0:
        return True, "twap_scratch_flip"
    return False, "twap_hold"


def time_weighted_twap(ticks: list[tuple[float, float]], end_ts: float, lookback: float) -> float | None:
    """ticks are (unix_seconds, price) sorted ascending, last tick may be after end_ts."""
    if not ticks or lookback <= 0:
        return None
    start = float(end_ts) - float(lookback)
    # Build a step path: price held until the next tick.
    xs = [(float(t), float(p)) for t, p in ticks if p > 0]
    if not xs:
        return None
    # Seed with last price at or before start.
    seed = None
    for t, p in xs:
        if t <= start:
            seed = p
        else:
            break
    if seed is None:
        # first tick inside window — weight from first tick only
        inside = [(t, p) for t, p in xs if start < t <= end_ts + 1e-9]
        if not inside:
            return None
        area = 0.0
        t_prev, p_prev = inside[0]
        for t, p in inside[1:]:
            area += p_prev * (t - t_prev)
            t_prev, p_prev = t, p
        area += p_prev * max(0.0, float(end_ts) - t_prev)
        dur = max(1e-9, float(end_ts) - inside[0][0])
        return area / dur
    points = [(start, seed)]
    for t, p in xs:
        if start < t <= end_ts + 1e-9:
            points.append((t, p))
    if points[-1][0] < end_ts:
        points.append((end_ts, points[-1][1]))
    area = 0.0
    for (t0, p0), (t1, _p1) in zip(points, points[1:]):
        area += p0 * max(0.0, t1 - t0)
    dur = max(1e-9, float(end_ts) - start)
    return area / dur
