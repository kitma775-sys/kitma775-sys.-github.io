"""Paper-only runner for the 5-minute crypto lock.

Connects to the public Chainlink TWAP stream Polymarket names in the market
rules, and to the public spot stream. One $20 buy per window, BTC and ETH,
and only inside the last 25 seconds when the ask is still <= 92c. Results go
to ``<data>/crypto-lock.jsonl``. This never sends an order.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import websockets

from polyarb.crypto_lock import (
    DAILY_STOP_USD,
    MAX_LEFT_S,
    MIN_LEFT_S,
    WINDOW_S,
    LockOrder,
    order_if_cheap,
)
from polyarb.fees import fee_per_share
from polyarb.models import FeeSchedule

RTDS_URL = "wss://ws-live-data.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
ASSETS = {
    "btc": {"twap": "btc/usd", "spot": "btcusdt"},
    "eth": {"twap": "eth/usd", "spot": "ethusdt"},
}
log = logging.getLogger(__name__)


def _window_start(now: float | None = None) -> int:
    now = time.time() if now is None else now
    return int(now) // WINDOW_S * WINDOW_S


def _day_pnl(path: Path, now: float | None = None) -> float:
    if not path.exists():
        return 0.0
    day = datetime.fromtimestamp(now or time.time(), timezone.utc).strftime("%Y-%m-%d")
    total = 0.0
    for line in path.read_text().splitlines():
        if not line:
            continue
        row = json.loads(line)
        if str(row.get("settled_at", "")).startswith(day):
            total += float(row.get("pnl") or 0)
    return total


class _Feed:
    def __init__(self) -> None:
        self.twap: dict[str, float] = {}
        self.spot: dict[str, float] = {}

    def apply(self, message: dict) -> None:
        topic = message.get("topic")
        payload = message.get("payload") or {}
        symbol = payload.get("symbol")
        points = payload.get("data")
        if isinstance(points, list):
            # Snapshots put the symbol on the payload, not on each point. Keep the latest print.
            for point in points:
                self._put(topic, point.get("symbol") or symbol, point.get("value"))
            return
        self._put(topic, symbol, payload.get("value"))

    def _put(self, topic: str | None, symbol: str | None, value) -> None:
        if symbol is None or value is None:
            return
        try:
            px = float(value)
        except (TypeError, ValueError):
            return
        if topic == "crypto_prices_twap_sixty":
            self.twap[symbol] = px
        elif topic == "crypto_prices":
            self.spot[symbol] = px


async def _stream(feed: _Feed, stop: asyncio.Event) -> None:
    subs = []
    for spec in ASSETS.values():
        subs.append({"topic": "crypto_prices_twap_sixty", "type": "update", "filters": json.dumps({"symbol": spec["twap"]})})
        subs.append({"topic": "crypto_prices", "type": "*", "filters": json.dumps({"symbol": spec["spot"]})})
    while not stop.is_set():
        try:
            async with websockets.connect(RTDS_URL, ping_interval=10, open_timeout=20, max_size=2**22) as ws:
                await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    except asyncio.TimeoutError:
                        await ws.send("ping")
                        continue
                    if not raw or not str(raw).lstrip().startswith(("{", "[")):
                        continue
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if isinstance(parsed, list):
                        for item in parsed:
                            if isinstance(item, dict):
                                feed.apply(item)
                    elif isinstance(parsed, dict):
                        feed.apply(parsed)
        except (OSError, websockets.WebSocketException) as exc:
            log.warning("price stream dropped: %s", exc)
            await asyncio.sleep(1)


async def _market(client: httpx.AsyncClient, asset: str, start: int) -> dict | None:
    resp = await client.get(f"{GAMMA}/markets", params={"slug": f"{asset}-updown-5m-{start}"})
    resp.raise_for_status()
    rows = resp.json()
    return rows[0] if isinstance(rows, list) and rows else None


async def _ask(client: httpx.AsyncClient, token: str) -> tuple[float, float] | None:
    resp = await client.get(f"{CLOB}/book", params={"token_id": token})
    if resp.status_code != 200:
        return None
    asks = resp.json().get("asks") or []
    if not asks:
        return None
    best = min(asks, key=lambda level: float(level["price"]))
    return float(best["price"]), float(best["size"])


def _fee(market: dict) -> FeeSchedule:
    sched = market.get("feeSchedule") or {}
    try:
        rate = float(sched.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    return FeeSchedule(rate=rate, exponent=float(sched.get("exponent") or 1) or 1.0, taker_only=True)


def _tokens(market: dict) -> list[str]:
    raw = market.get("clobTokenIds")
    return json.loads(raw) if isinstance(raw, str) else list(raw or [])


async def _settle(client: httpx.AsyncClient, slug: str) -> bool | None:
    deadline = time.time() + 90
    while time.time() < deadline:
        resp = await client.get(f"{GAMMA}/markets", params={"slug": slug})
        rows = resp.json() if resp.status_code == 200 else []
        market = rows[0] if isinstance(rows, list) and rows else None
        if market:
            prices = market.get("outcomePrices")
            if isinstance(prices, str):
                prices = json.loads(prices)
            if prices and float(prices[0]) >= 0.99:
                return True
            if prices and float(prices[1]) >= 0.99:
                return False
        await asyncio.sleep(3)
    return None


async def run_paper(path: Path, duration_s: float | None = None) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    feed = _Feed()
    stop = asyncio.Event()
    stream = asyncio.create_task(_stream(feed, stop))
    started = time.time()
    opens: dict[tuple[str, int], tuple[float, float]] = {}
    traded: set[tuple[str, int]] = set()
    quoted: set[tuple[str, int]] = set()
    skipped: set[int] = set()
    pending: list[asyncio.Task] = []
    async with httpx.AsyncClient(timeout=15) as client:
        try:
            while duration_s is None or time.time() - started < duration_s:
                if _day_pnl(path) <= DAILY_STOP_USD:
                    log.error("crypto-lock daily stop hit; pausing new trades")
                    await asyncio.sleep(5)
                    continue
                start = _window_start()
                left = start + WINDOW_S - time.time()
                for asset, spec in ASSETS.items():
                    twap = feed.twap.get(spec["twap"])
                    spot = feed.spot.get(spec["spot"])
                    if twap is None or spot is None:
                        continue
                    key = (asset, start)
                    if key not in opens and left <= WINDOW_S - 2:
                        # Joined after the window opened; the start print is already gone.
                        if start not in skipped:
                            skipped.add(start)
                            log.info("crypto-lock skipping this window; open print was missed")
                        continue
                    opens.setdefault(key, (twap, spot))
                    if key in traded or not MIN_LEFT_OK(left):
                        continue
                    twap_open, spot_open = opens[key]
                    market = await _market(client, asset, start)
                    if market is None:
                        continue
                    tokens = _tokens(market)
                    if len(tokens) < 2:
                        continue
                    up = await _ask(client, tokens[0])
                    down = await _ask(client, tokens[1])
                    order = order_if_cheap(
                        twap_open, twap, spot_open, spot, left,
                        up[0] if up else None, down[0] if down else None,
                    )
                    if key not in quoted:
                        quoted.add(key)
                        log.info(
                            "crypto-lock %s left=%.0fs twap %+.4f%% spot %+.4f%% ask_up=%s ask_down=%s %s",
                            asset, left,
                            (twap / twap_open - 1) * 100, (spot / spot_open - 1) * 100,
                            None if up is None else round(up[0], 3),
                            None if down is None else round(down[0], 3),
                            "BUY " + order.side if order else "skip",
                        )
                    if order is None:
                        continue
                    size = up[1] if order.side == "up" else down[1]
                    if size < order.shares:
                        continue
                    traded.add(key)
                    pending.append(asyncio.create_task(
                        _record(client, path, market, asset, start, order, twap_open, twap, left)
                    ))
                await asyncio.sleep(1)
        finally:
            stop.set()
            stream.cancel()
            await asyncio.gather(stream, *pending, return_exceptions=True)
    return 0


def MIN_LEFT_OK(left: float) -> bool:
    return MIN_LEFT_S <= left <= MAX_LEFT_S


async def _record(client, path: Path, market: dict, asset: str, start: int, order: LockOrder, twap_open: float, twap: float, left: float) -> None:
    fee = _fee(market)
    won = await _settle(client, f"{asset}-updown-5m-{start}")
    side_won = (order.side == "up") if won is not None else None
    fee_usd = order.shares * fee_per_share(fee, order.ask)
    if side_won is None:
        pnl = None
    elif side_won:
        pnl = order.shares * (1.0 - order.ask) - fee_usd
    else:
        pnl = -order.shares * order.ask - fee_usd
    row = {
        "asset": asset,
        "window_start": start,
        "side": order.side,
        "ask": round(order.ask, 4),
        "shares": round(order.shares, 2),
        "twap_open": twap_open,
        "twap_now": twap,
        "seconds_left": round(left, 1),
        "won": side_won,
        "pnl": None if pnl is None else round(pnl, 4),
        "settled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    with path.open("a") as fh:
        fh.write(json.dumps(row) + "\n")
    log.info("crypto-lock paper %s", row)
