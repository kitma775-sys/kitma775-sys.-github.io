"""Pick short-dated liquid binaries — the only HTTP-reachable surf set.

Long-dated high-volume books rest at ask_sum ≥ 1.001. Sports tags are not
game-clock. The books that still *move* on a 2s poll are 15m/1h crypto
windows, ranked by time-to-expiry, skipping empty 1.00/1.00 books.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

DEFAULT_TAGS = ["15M", "1H"]
DEFAULT_ASSETS = ["btc", "eth", "sol", "xrp", "bnb", "hype", "doge"]
EMPTY_YES_ASK = 0.99
# 15m slugs are btc-updown-15m-… ; 1h slugs are bitcoin-up-or-down-…
ASSET_ALIASES = {
    "btc": ("btc", "bitcoin"),
    "eth": ("eth", "ethereum"),
    "sol": ("sol", "solana"),
    "xrp": ("xrp",),
    "bnb": ("bnb", "binance"),
    "hype": ("hype",),
    "doge": ("doge", "dogecoin"),
}


def parse_tokens(raw: Any) -> list[str]:
    if isinstance(raw, list):
        return [str(x) for x in raw if x]
    if not raw:
        return []
    import json

    try:
        data = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(x) for x in data if x]


def seconds_left(end: str | None, *, now: datetime | None = None) -> float | None:
    if not end:
        return None
    try:
        dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return (dt - stamp).total_seconds()


def iso_z(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def gamma_events_params(
    tag: str,
    *,
    limit: int,
    now: datetime,
    max_horizon: float,
) -> dict[str, str | int]:
    """Soonest live windows. Unfiltered /events still lists ended 15m books as open."""
    return {
        "active": "true",
        "closed": "false",
        "limit": int(limit),
        "tag_slug": tag,
        "end_date_min": iso_z(now),
        "end_date_max": iso_z(now + timedelta(seconds=max(60.0, float(max_horizon)))),
        "order": "endDate",
        "ascending": "true",
    }


def is_updown(slug: str) -> bool:
    text = (slug or "").lower()
    return "updown" in text or "up-or-down" in text


def asset_hit(slug: str, assets: list[str] | None) -> bool:
    if not assets:
        return True
    text = (slug or "").lower()
    for raw in assets:
        if not raw:
            continue
        key = str(raw).lower()
        needles = ASSET_ALIASES.get(key, (key,))
        if any(n in text for n in needles):
            return True
    return False


def looks_empty(best_ask: Any, seconds_left: float | None = None) -> bool:
    """Skip locked 1.00/1.00 books. Keep 0.99 tails in the last 3 minutes.

    Gamma bestAsk ≥ 0.99 mid-window is usually an empty alt. At expiry the
    winning YES can quote 0.99 while NO is still 0.01 — that is the surf.
    """
    try:
        ask = float(best_ask)
    except (TypeError, ValueError):
        return False
    if ask >= 1.0:
        return True
    if ask >= EMPTY_YES_ASK:
        if seconds_left is None:
            return True
        try:
            return float(seconds_left) > 180.0
        except (TypeError, ValueError):
            return True
    return False


def pick_markets(
    rows: list[dict],
    *,
    want: int = 16,
    max_horizon: float = 3600.0,
    min_left: float = 3.0,
) -> list[dict]:
    live: list[dict] = []
    for row in rows:
        left = row.get("seconds_left")
        try:
            left_f = float(left)
        except (TypeError, ValueError):
            continue
        if left_f < min_left or left_f > max_horizon:
            continue
        if looks_empty(row.get("best_ask"), row.get("seconds_left")):
            continue
        live.append(row)
    live.sort(key=lambda r: (float(r["seconds_left"]), -float(r.get("volume24hr") or 0)))
    out: list[dict] = []
    seen: set[str] = set()
    for row in live:
        cid = str(row.get("condition_id") or row.get("slug") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        out.append(row)
        if len(out) >= max(1, int(want)):
            break
    return out
