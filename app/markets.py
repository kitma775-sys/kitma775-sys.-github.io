from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import httpx

from app.geo import interpret
from app.hunter import Level
from app.rescue import parse_outcome_prices
from app.twap import parse_window
from app.universe import (
    DEFAULT_ASSETS,
    DEFAULT_TAGS,
    asset_hit,
    gamma_events_params,
    is_updown,
    parse_tokens,
    pick_markets,
    seconds_left,
    tag_horizon,
)

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GEO = "https://polymarket.com/api/geoblock"


def _parse_levels(raw: list | None, reverse: bool) -> list[Level]:
    if not isinstance(raw, list):
        return []
    out: list[Level] = []
    for row in raw or []:
        if not isinstance(row, dict):
            continue
        try:
            out.append(Level(price=float(row["price"]), size=float(row["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda x: x.price, reverse=reverse)


class MarketData:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def geoblock(self) -> dict[str, Any]:
        try:
            r = await self.client.get(GEO, timeout=10)
            r.raise_for_status()
            return interpret(r.json())
        except Exception as exc:
            return interpret({"blocked": None, "error": str(exc)})

    async def live_events(
        self,
        tags: str | list[str] | None,
        assets: list[str] | None,
        want: int = 16,
        max_horizon: float = 3600.0,
    ) -> list[dict]:
        tag_list = [str(t).strip() for t in (tags if isinstance(tags, list) else [tags or "5M"]) if str(t).strip()]
        if not tag_list:
            tag_list = list(DEFAULT_TAGS)
        asset_list = [str(a).strip() for a in (assets if assets is not None else DEFAULT_ASSETS) if str(a).strip()]
        now = datetime.now(timezone.utc)
        fetched = await asyncio.gather(
            *(
                self._events_tag(
                    tag,
                    limit=max(want * 4, 40),
                    now=now,
                    max_horizon=tag_horizon(tag, max_horizon),
                )
                for tag in tag_list
            ),
            return_exceptions=True,
        )
        rows: list[dict] = []
        for tag, payload in zip(tag_list, fetched):
            if isinstance(payload, Exception):
                continue
            rows.extend(self._normalize_events(payload, tag, asset_list, now))
        return pick_markets(rows, want=want, max_horizon=max_horizon)

    async def _events_tag(self, tag: str, limit: int = 40, now: datetime | None = None, max_horizon: float = 3600.0) -> list:
        stamp = now or datetime.now(timezone.utc)
        r = await self.client.get(
            f"{GAMMA}/events",
            params=gamma_events_params(tag, limit=limit, now=stamp, max_horizon=max_horizon),
            timeout=15,
        )
        r.raise_for_status()
        events = r.json() or []
        return events if isinstance(events, list) else []

    def _normalize_events(self, events: list, tag: str, assets: list[str], now: datetime) -> list[dict]:
        picked: list[dict] = []
        for ev in events:
            markets = ev.get("markets") or []
            if not markets:
                continue
            m = markets[0]
            if m.get("closed") or ev.get("closed") or m.get("acceptingOrders") is False:
                continue
            end = m.get("endDate") or ev.get("endDate")
            slug = str(ev.get("slug") or m.get("slug") or "")
            if not is_updown(slug) or not asset_hit(slug, assets):
                continue
            tokens = parse_tokens(m.get("clobTokenIds"))
            if len(tokens) < 2:
                continue
            fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
            parsed = parse_window(slug)
            outcomes = parse_outcome_prices(m.get("outcomePrices"))
            picked.append(
                {
                    "slug": slug,
                    "title": ev.get("title") or slug,
                    "condition_id": m.get("conditionId") or "",
                    "up_token": tokens[0],
                    "down_token": tokens[1],
                    "end": end,
                    "tick": m.get("orderPriceMinTickSize") or 0.01,
                    "min_size": m.get("orderMinSize") or 5,
                    "fee_rate": fs.get("rate"),
                    "neg_risk": bool(m.get("negRisk")),
                    "tag": tag,
                    "best_ask": m.get("bestAsk"),
                    "outcome_prices": None if outcomes is None else [outcomes[0], outcomes[1]],
                    "volume24hr": float(m.get("volume24hr") or ev.get("volume24hr") or 0),
                    "seconds_left": seconds_left(end, now=now),
                    "twap_ok": parsed is not None,
                    "twap_horizon": None if parsed is None else parsed.horizon,
                }
            )
        return picked

    async def book(self, token_id: str) -> dict[str, list[Level]]:
        empty: dict[str, list[Level]] = {"asks": [], "bids": []}
        if not token_id:
            return empty
        last_exc: Exception | None = None
        for _ in range(2):
            try:
                r = await self.client.get(f"{CLOB}/book", params={"token_id": token_id}, timeout=8)
                if r.status_code >= 400:
                    return empty
                data = r.json() or {}
                if not isinstance(data, dict):
                    return empty
                return {
                    "asks": _parse_levels(data.get("asks"), reverse=False),
                    "bids": _parse_levels(data.get("bids"), reverse=True),
                }
            except Exception as exc:
                last_exc = exc
                await asyncio.sleep(0.15)
        if last_exc is not None:
            raise last_exc
        return empty

    async def event_by_slug(self, slug: str) -> dict[str, Any] | None:
        if not slug:
            return None
        try:
            r = await self.client.get(f"{GAMMA}/events", params={"slug": slug}, timeout=10)
            r.raise_for_status()
            rows = r.json() or []
        except Exception:
            return None
        if not rows:
            return None
        return rows[0]

    async def books_pair(self, up_token: str, down_token: str) -> tuple[dict, dict]:
        up, down = await self.book(up_token), await self.book(down_token)
        # fetch sequentially is safer on rate limits; caller can gather if needed
        return up, down
