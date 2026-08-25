from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

import httpx

from app.geo import interpret
from app.hunter import Level

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

    async def live_events(self, tag: str, assets: list[str], want: int = 12) -> list[dict]:
        r = await self.client.get(
            f"{GAMMA}/events",
            params={"active": "true", "closed": "false", "limit": max(want * 4, 24), "tag_slug": tag},
            timeout=15,
        )
        r.raise_for_status()
        events = r.json() or []
        if not isinstance(events, list):
            return []
        now = datetime.now(timezone.utc)
        picked: list[dict] = []
        assets_l = [a.lower() for a in assets if a]
        for ev in events:
            markets = ev.get("markets") or []
            if not markets:
                continue
            m = markets[0]
            if m.get("closed") or ev.get("closed") or m.get("acceptingOrders") is False:
                continue
            end = m.get("endDate") or ev.get("endDate")
            if end:
                try:
                    end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
                    if end_dt < now:
                        continue
                except ValueError:
                    pass
            slug = str(ev.get("slug") or "")
            if assets_l and not any(a in slug.lower() for a in assets_l):
                continue
            try:
                tokens = json.loads(m["clobTokenIds"])
            except (KeyError, TypeError, json.JSONDecodeError):
                continue
            if len(tokens) < 2:
                continue
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
                    "fee_rate": ((m.get("feeSchedule") or {}).get("rate") if isinstance(m.get("feeSchedule"), dict) else None),
                    "neg_risk": bool(m.get("negRisk")),
                }
            )
            if len(picked) >= want:
                break
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
