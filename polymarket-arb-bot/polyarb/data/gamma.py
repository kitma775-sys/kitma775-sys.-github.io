from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, AsyncIterator, Mapping

import httpx

from polyarb.fastjson import loads
from polyarb.models import FEE_FREE, Event, FeeSchedule, Market

GAMMA_URL = "https://gamma-api.polymarket.com"
log = logging.getLogger(__name__)


def _json_list(value: Any) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except ValueError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    for candidate in (text, text[:10]):
        try:
            dt = datetime.fromisoformat(candidate)
        except ValueError:
            continue
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    return None


def parse_fee(raw: Mapping[str, Any]) -> FeeSchedule:
    sched = raw.get("feeSchedule")
    if raw.get("feesEnabled") is False or not isinstance(sched, Mapping):
        return FEE_FREE
    rate = _float(sched.get("rate"))
    if rate <= 0:
        return FEE_FREE
    return FeeSchedule(
        rate=rate,
        exponent=_float(sched.get("exponent"), 1.0) or 1.0,
        taker_only=bool(sched.get("takerOnly", True)),
        rebate_rate=_float(sched.get("rebateRate")),
    )


def parse_market(raw: Mapping[str, Any], event_end: datetime | None = None) -> Market | None:
    tokens = [str(t) for t in _json_list(raw.get("clobTokenIds"))]
    outcomes = [str(o) for o in _json_list(raw.get("outcomes"))]
    if len(tokens) != 2 or len(outcomes) != 2 or not all(tokens):
        return None
    accepting = (
        bool(raw.get("active"))
        and not raw.get("closed")
        and not raw.get("archived")
        and bool(raw.get("acceptingOrders"))
        and raw.get("enableOrderBook") is not False
    )
    closed = bool(raw.get("closed"))
    resolved = None
    if closed:
        prices = [_float(p, -1.0) for p in _json_list(raw.get("outcomePrices"))]
        if len(prices) == 2 and prices[0] >= 0.99:
            resolved = "YES"
        elif len(prices) == 2 and prices[1] >= 0.99:
            resolved = "NO"
    return Market(
        id=str(raw.get("id")),
        condition_id=str(raw.get("conditionId") or ""),
        question=str(raw.get("question") or ""),
        slug=str(raw.get("slug") or ""),
        yes_token=tokens[0],
        no_token=tokens[1],
        outcomes=(outcomes[0], outcomes[1]),
        group_title=str(raw.get("groupItemTitle") or "").strip(),
        fee=parse_fee(raw),
        tick_size=_float(raw.get("orderPriceMinTickSize"), 0.01) or 0.01,
        min_size=_float(raw.get("orderMinSize"), 5.0) or 5.0,
        end_date=parse_dt(raw.get("endDate")) or event_end,
        neg_risk=bool(raw.get("negRisk")),
        neg_risk_market_id=raw.get("negRiskMarketID") or None,
        question_id=raw.get("questionID") or None,
        neg_risk_other=bool(raw.get("negRiskOther")),
        accepting_orders=accepting,
        version=str(raw.get("version") or "v1"),
        closed=closed,
        resolved_outcome=resolved,
        seconds_delay=int(_float(raw.get("secondsDelay"))),
        volume_24h=_float(raw.get("volume24hr")),
        liquidity=_float(raw.get("liquidityNum") or raw.get("liquidity")),
        fee_type=str(raw.get("feeType") or ""),
    )


def parse_event(raw: Mapping[str, Any]) -> Event:
    end = parse_dt(raw.get("endDate"))
    markets = [m for m in (parse_market(rm, end) for rm in raw.get("markets") or []) if m is not None]
    tags = tuple(
        str(t.get("slug") if isinstance(t, Mapping) else t)
        for t in raw.get("tags") or []
        if t
    )
    return Event(
        id=str(raw.get("id")),
        slug=str(raw.get("slug") or ""),
        title=str(raw.get("title") or ""),
        markets=markets,
        neg_risk=bool(raw.get("negRisk") or raw.get("enableNegRisk")),
        neg_risk_augmented=bool(raw.get("negRiskAugmented")),
        neg_risk_market_id=raw.get("negRiskMarketID") or None,
        end_date=end,
        version=str(raw.get("version") or "v1"),
        tags=tags,
        cumulative=raw.get("cumulativeMarkets"),
        sort_by=raw.get("sortBy"),
    )


class GammaClient:
    """Read-only client for market discovery (keyset pagination)."""

    def __init__(
        self,
        http: httpx.AsyncClient | None = None,
        base_url: str = GAMMA_URL,
        timeout: float = 30.0,
        max_retries: int = 5,
    ) -> None:
        self._own = http is None
        self._http = http or httpx.AsyncClient(timeout=timeout, http2=False)
        self._base = base_url.rstrip("/")
        self._max_retries = max_retries

    async def __aenter__(self) -> "GammaClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._own:
            await self._http.aclose()

    async def _get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        delay = 1.0
        for attempt in range(self._max_retries):
            try:
                resp = await self._http.get(f"{self._base}{path}", params=params)
                if resp.status_code == 429 or resp.status_code >= 500:
                    raise httpx.HTTPStatusError("retryable", request=resp.request, response=resp)
                resp.raise_for_status()
                return loads(resp.content)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status is not None and status < 500 and status != 429:
                    raise
                if attempt == self._max_retries - 1:
                    raise
                log.warning("gamma %s failed (%s); retry in %.1fs", path, exc, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 16.0)
        raise RuntimeError("unreachable")

    async def iter_raw_events(
        self, *, closed: bool = False, page_size: int = 100, max_pages: int | None = None, **filters: Any
    ) -> AsyncIterator[dict]:
        cursor: str | None = None
        pages = 0
        while True:
            params: dict[str, Any] = {"closed": str(closed).lower(), "limit": page_size, **filters}
            if cursor:
                params["after_cursor"] = cursor
            data = await self._get("/events/keyset", params)
            events = data.get("events") if isinstance(data, Mapping) else data
            for raw in events or []:
                yield raw
            pages += 1
            cursor = data.get("next_cursor") if isinstance(data, Mapping) else None
            if not cursor or not events or (max_pages is not None and pages >= max_pages):
                return

    async def fetch_events(self, *, min_markets: int = 2, **filters: Any) -> list[Event]:
        """Open events with at least ``min_markets`` tradeable markets (single binaries carry no structure)."""
        out: list[Event] = []
        async for raw in self.iter_raw_events(**filters):
            if len(raw.get("markets") or ()) < min_markets:
                continue
            ev = parse_event(raw)
            if len(ev.tradeable_markets) >= min_markets:
                out.append(ev)
        return out

    async def get_event(self, event_id: str) -> Event | None:
        try:
            raw = await self._get(f"/events/{event_id}")
        except httpx.HTTPStatusError:
            return None
        return parse_event(raw) if isinstance(raw, Mapping) else None
