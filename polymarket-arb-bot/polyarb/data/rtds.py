"""Real-time Chainlink RTDS stream + rolling 5m / 15m / 1h short-crypto event discovery.

Connects to Polymarket's public live-data relay (``wss://ws-live-data.polymarket.com``)
to track the exact Chainlink Data Streams used on-chain by ``btc-5m-twap-60`` and
``btc-15m-twap-60`` markets:

1. ``crypto_prices_twap_sixty`` (``btc/usd``, ``eth/usd``, ``sol/usd``, ``xrp/usd``):
   * Records exact 1-second ``timestamp -> 60s_twap`` prints. As verified on-chain,
     ``eventMetadata.priceToBeat`` at ``T_start`` and ``eventMetadata.finalPrice`` at
     ``T_end`` equal the exact 10-decimal ``crypto_prices_twap_sixty`` print at those
     Unix timestamps.
2. ``crypto_prices_chainlink`` (1-second Chainlink spot) + ``crypto_prices`` (1-second
   Binance spot):
   * Used to integrate the **already-realized portion** ``[T_end - 60, t]`` of the
     settling 60-second TWAP window and cross-verify zero exchange lead-lag divergence.
3. ``ShortCryptoCatalog``:
   * Deterministic slug loader for rolling 1-market ``5m``, ``15m``, and ``1h`` Up/Down
     events (plus the companion ``1h Above ___`` 20-strike ladder), seeding exact
     ``priceToBeat`` on cold boot from the preceding ``5m`` window's ``finalPrice``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta
from typing import Callable, Iterable, Mapping

import httpx
import websockets

from polyarb.data.binance import ET_ZONE
from polyarb.data.gamma import GAMMA_URL, parse_event
from polyarb.models import Event, Outcome

RTDS_URL = "wss://ws-live-data.polymarket.com"
log = logging.getLogger(__name__)

SHORT_ASSETS: dict[str, dict[str, str]] = {
    "btc": {"cl": "btc/usd", "spot": "btcusdt", "full": "bitcoin", "binance": "BTCUSDT"},
    "eth": {"cl": "eth/usd", "spot": "ethusdt", "full": "ethereum", "binance": "ETHUSDT"},
    "sol": {"cl": "sol/usd", "spot": "solusdt", "full": "solana", "binance": "SOLUSDT"},
    "xrp": {"cl": "xrp/usd", "spot": "xrpusdt", "full": "xrp", "binance": "XRPUSDT"},
}
CL_TO_ASSET: dict[str, str] = {v["cl"]: k for k, v in SHORT_ASSETS.items()}
SPOT_TO_ASSET: dict[str, str] = {v["spot"]: k for k, v in SHORT_ASSETS.items()}

_MONTH_NAMES = (
    "",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)


def fmt_et_hour_slug(dt_et: datetime) -> tuple[str, str]:
    """Return ``(month_day_year, hour_str)`` in Polymarket's exact hourly slug format, e.g. ``('november-13-2026', '4am')``."""
    mon = _MONTH_NAMES[dt_et.month]
    h = dt_et.hour
    ampm = "am" if h < 12 else "pm"
    h12 = 12 if (h % 12 == 0) else (h % 12)
    return f"{mon}-{dt_et.day}-{dt_et.year}", f"{h12}{ampm}"


class ChainlinkRtdsFeed:
    """Maintains exact per-second Chainlink 60s-TWAP & 1s-Spot ring buffers for 5m/15m locks."""

    def __init__(
        self,
        *,
        assets: Iterable[str] = ("btc", "eth", "sol", "xrp"),
        on_asset_tick: Callable[[str], None] | None = None,
        url: str = RTDS_URL,
        twap_keep_s: int = 2400,
        spot_keep_s: int = 240,
    ) -> None:
        self.assets = tuple(sorted({a.lower() for a in assets if a.lower() in SHORT_ASSETS}))
        self.on_asset_tick = on_asset_tick
        self.url = url
        self.twap_keep_s = twap_keep_s
        self.spot_keep_s = spot_keep_s

        # asset ("btc") -> {unix_sec: value}
        self._twap_sec: dict[str, dict[int, float]] = {a: {} for a in SHORT_ASSETS}
        self._cl_spot_sec: dict[str, dict[int, float]] = {a: {} for a in SHORT_ASSETS}
        self._latest_twap: dict[str, tuple[int, float]] = {}
        self._latest_cl_spot: dict[str, tuple[int, float]] = {}
        self._latest_bin_spot: dict[str, tuple[int, float]] = {}
        self._last_wake_at: dict[str, float] = {}

        self._task: asyncio.Task | None = None
        self.messages = 0
        self.reconnects = 0
        self.last_msg_at = 0.0

    def seed_benchmark(self, asset: str, ts_sec: int, value: float) -> None:
        """Seed exact Chainlink TWAP at ``ts_sec`` (e.g. from Gamma ``priceToBeat`` or prior 5m ``finalPrice``)."""
        a = asset.lower()
        if a in self._twap_sec and value > 0 and ts_sec > 0:
            self._twap_sec[a].setdefault(int(ts_sec), float(value))

    def twap_at(self, asset: str, ts_sec: int, tolerance_s: int = 0) -> float | None:
        """Return Chainlink 60s TWAP at ``ts_sec`` (or within ``tolerance_s`` when > 0)."""
        buf = self._twap_sec.get(asset.lower())
        if not buf:
            return None
        if ts_sec in buf:
            return buf[ts_sec]
        if tolerance_s > 0:
            for d in range(1, tolerance_s + 1):
                if (ts_sec + d) in buf:
                    return buf[ts_sec + d]
                if (ts_sec - d) in buf:
                    return buf[ts_sec - d]
        return None

    def latest_twap(self, asset: str) -> tuple[int, float] | None:
        return self._latest_twap.get(asset.lower())

    def latest_cl_spot(self, asset: str) -> tuple[int, float] | None:
        return self._latest_cl_spot.get(asset.lower())

    def latest_bin_spot(self, asset: str) -> tuple[int, float] | None:
        return self._latest_bin_spot.get(asset.lower())

    def integral_locked_outcome(
        self,
        asset: str,
        open_twap: float,
        end_ts: int,
        now_s: float,
        shock_bps: float = 90.0,
        min_coverage: float = 0.70,
    ) -> tuple[Outcome, str] | None:
        """Exact realized-integral 60s-TWAP lock during ``(0, 25]`` seconds before ``end_ts``.

        Uses the already-elapsed Chainlink 1s spot prints inside ``[end_ts - 60, floor(now_s)]``
        plus a worst-case instantaneous ``shock_bps`` jump held for ALL remaining ``r`` seconds,
        AND cross-checks both 1s Chainlink spot and Binance spot for agreement.
        """
        a = asset.lower()
        rem_s = end_ts - now_s
        if open_twap <= 0 or not (0.0 < rem_s < 55.0):
            return None
        cl_latest = self._latest_cl_spot.get(a)
        tw_latest = self._latest_twap.get(a)
        if cl_latest is None or tw_latest is None:
            return None
        # Freshness guard: stream must have updated within the last 4 seconds
        if now_s - cl_latest[0] > 4.5 or now_s - tw_latest[0] > 4.5:
            return None
        cl_now = cl_latest[1]
        tw_now = tw_latest[1]

        win_start = end_ts - 60
        cur_sec = min(end_ts - 1, int(now_s))
        elapsed_n = max(0, cur_sec - win_start + 1)
        if elapsed_n < 10:
            return None

        spot_buf = self._cl_spot_sec.get(a) or {}
        observed = [spot_buf[s] for s in range(win_start, cur_sec + 1) if s in spot_buf]
        if len(observed) < max(8, int(elapsed_n * min_coverage)):
            return None

        locked_avg = sum(observed) / len(observed)
        locked_s = 60.0 - rem_s
        shock = max(0.0, shock_bps) / 10_000.0

        # Worst-case ending 60s TWAP under an immediate +/- shock held for all remaining rem_s seconds
        worst_up_end = (locked_s * locked_avg + rem_s * cl_now * (1.0 - shock)) / 60.0
        worst_dn_end = (locked_s * locked_avg + rem_s * cl_now * (1.0 + shock)) / 60.0

        # Also check current 60s TWAP & Binance spot (if fresh) are not diverging the wrong way
        bin_latest = self._latest_bin_spot.get(a)
        bin_fresh = bin_latest is not None and (now_s - bin_latest[0] <= 5.0)

        if worst_up_end > open_twap and cl_now * (1.0 - shock * (rem_s / 60.0)) > open_twap and tw_now > open_twap:
            if bin_fresh:
                # Guard against a sudden >-25bp spot crash on Binance that hasn't hit Chainlink yet
                if (bin_latest[1] - cl_now) / cl_now < -0.0025:  # type: ignore[index]
                    return None
            margin_bp = (worst_up_end - open_twap) / open_twap * 10_000.0
            return Outcome.YES, f"twap_min={worst_up_end:.2f}>open={open_twap:.2f} (+{margin_bp:.1f}bp@{shock_bps:.0f}bp, r={rem_s:.1f}s)"

        if worst_dn_end < open_twap and cl_now * (1.0 + shock * (rem_s / 60.0)) < open_twap and tw_now < open_twap:
            if bin_fresh:
                if (bin_latest[1] - cl_now) / cl_now > 0.0025:  # type: ignore[index]
                    return None
            margin_bp = (open_twap - worst_dn_end) / open_twap * 10_000.0
            return Outcome.NO, f"twap_max={worst_dn_end:.2f}<open={open_twap:.2f} (+{margin_bp:.1f}bp@{shock_bps:.0f}bp, r={rem_s:.1f}s)"

        return None

    def tail_reversal_lock(
        self,
        asset: str,
        open_twap: float,
        end_ts: int,
        now_s: float,
        min_reversal_usd: float,
        min_coverage: float = 0.70,
    ) -> tuple[Outcome, float, str] | None:
        """Last-seconds lock: how far the remaining prints must run to cross the strike.

        ``req = elapsed / remaining * |realized_avg - open|``. Returns
        ``(winning outcome, req_usd, proof)`` only when ``req`` clears
        ``min_reversal_usd`` and spot, the rolling TWAP, and a fresh Binance
        print all still sit on that side of the strike.
        """
        a = asset.lower()
        rem_s = end_ts - now_s
        if open_twap <= 0 or min_reversal_usd <= 0 or not (0.0 < rem_s < 55.0):
            return None
        cl_latest = self._latest_cl_spot.get(a)
        tw_latest = self._latest_twap.get(a)
        if cl_latest is None or tw_latest is None:
            return None
        if now_s - cl_latest[0] > 4.5 or now_s - tw_latest[0] > 4.5:
            return None
        cl_now, tw_now = cl_latest[1], tw_latest[1]
        win_start = end_ts - 60
        cur_sec = min(end_ts - 1, int(now_s))
        elapsed_n = max(0, cur_sec - win_start + 1)
        if elapsed_n < 10:
            return None
        spot_buf = self._cl_spot_sec.get(a) or {}
        observed = [spot_buf[s] for s in range(win_start, cur_sec + 1) if s in spot_buf]
        if len(observed) < max(8, int(elapsed_n * min_coverage)):
            return None
        locked_avg = sum(observed) / len(observed)
        lead = locked_avg - open_twap
        if abs(lead) < 1e-6:
            return None
        req = ((60.0 - rem_s) / max(rem_s, 0.5)) * abs(lead)
        if req + 1e-9 < min_reversal_usd:
            return None
        bin_latest = self._latest_bin_spot.get(a)
        bin_fresh = bin_latest is not None and (now_s - bin_latest[0] <= 5.0)
        if lead > 0:
            if not (cl_now > open_twap and tw_now > open_twap):
                return None
            if bin_fresh and (bin_latest[1] - cl_now) / cl_now < -0.0025:  # type: ignore[index]
                return None
            return Outcome.YES, req, f"tail lead=+{lead:.1f} req=${req:.0f} r={rem_s:.1f}s"
        if not (cl_now < open_twap and tw_now < open_twap):
            return None
        if bin_fresh and (bin_latest[1] - cl_now) / cl_now > 0.0025:  # type: ignore[index]
            return None
        return Outcome.NO, req, f"tail lead={lead:.1f} req=${req:.0f} r={rem_s:.1f}s"

    def ingest_message(self, message: Mapping) -> set[str]:
        topic = message.get("topic")
        if topic not in ("crypto_prices_twap_sixty", "crypto_prices_chainlink", "crypto_prices"):
            return set()
        payload = message.get("payload")
        if not isinstance(payload, Mapping):
            return set()
        base_sym = str(payload.get("symbol") or "").lower()
        points = payload.get("data")
        rows = points if isinstance(points, list) else [payload]

        touched: set[str] = set()
        for pt in rows:
            if not isinstance(pt, Mapping):
                continue
            sym = str(pt.get("symbol") or base_sym).lower()
            val_raw = pt.get("value")
            ts_raw = pt.get("timestamp") or message.get("timestamp")
            if val_raw is None or ts_raw is None:
                continue
            try:
                val = float(val_raw)
                ts_num = int(float(ts_raw))
            except (TypeError, ValueError):
                continue
            if val <= 0:
                continue
            ts_sec = ts_num // 1000 if ts_num > 10_000_000_000 else ts_num

            if topic == "crypto_prices_twap_sixty":
                asset = CL_TO_ASSET.get(sym)
                if asset in self.assets:
                    buf = self._twap_sec[asset]
                    buf[ts_sec] = val
                    prev = self._latest_twap.get(asset)
                    if prev is None or ts_sec >= prev[0]:
                        self._latest_twap[asset] = (ts_sec, val)
                    touched.add(asset)
            elif topic == "crypto_prices_chainlink":
                asset = CL_TO_ASSET.get(sym)
                if asset in self.assets:
                    buf = self._cl_spot_sec[asset]
                    buf[ts_sec] = val
                    prev = self._latest_cl_spot.get(asset)
                    if prev is None or ts_sec >= prev[0]:
                        self._latest_cl_spot[asset] = (ts_sec, val)
                    touched.add(asset)
            elif topic == "crypto_prices":
                asset = SPOT_TO_ASSET.get(sym)
                if asset in self.assets:
                    prev = self._latest_bin_spot.get(asset)
                    if prev is None or ts_sec >= prev[0]:
                        self._latest_bin_spot[asset] = (ts_sec, val)

        self.messages += 1
        self.last_msg_at = time.time()
        if self.messages % 120 == 0:
            self._prune(int(self.last_msg_at))

        if touched and self.on_asset_tick is not None:
            now_m = time.monotonic()
            for a in touched:
                sec_mod = int(self.last_msg_at) % 300
                # Wake aggressively in [-25s .. +75s] around every 5m boundary, else 1Hz throttle
                in_hot_zone = sec_mod >= 275 or sec_mod <= 75
                min_gap = 0.20 if in_hot_zone else 1.2
                if now_m - self._last_wake_at.get(a, 0.0) >= min_gap:
                    self._last_wake_at[a] = now_m
                    self.on_asset_tick(a)
        return touched

    def _prune(self, now_sec: int) -> None:
        tw_cut = now_sec - self.twap_keep_s
        sp_cut = now_sec - self.spot_keep_s
        for a in self.assets:
            tw = self._twap_sec[a]
            if len(tw) > self.twap_keep_s + 120:
                for k in [k for k in tw if k < tw_cut]:
                    del tw[k]
            sp = self._cl_spot_sec[a]
            if len(sp) > self.spot_keep_s + 60:
                for k in [k for k in sp if k < sp_cut]:
                    del sp[k]

    async def start(self) -> None:
        await self.stop()
        if not self.assets:
            return
        self._task = asyncio.create_task(self._run(), name="rtds-chainlink")

    async def _run(self) -> None:
        subs = [
            {"topic": "crypto_prices_twap_sixty", "type": "*", "filters": ""},
            {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
            *[
                {"topic": "crypto_prices", "type": "*", "filters": json.dumps({"symbol": SHORT_ASSETS[a]["spot"]})}
                for a in self.assets
            ],
        ]
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(self.url, ping_interval=10, open_timeout=15, max_size=2**23) as ws:
                    await ws.send(json.dumps({"action": "subscribe", "subscriptions": subs}))
                    backoff = 1.0
                    async for raw in ws:
                        if not raw or not str(raw).lstrip().startswith(("{", "[")):
                            continue
                        try:
                            parsed = json.loads(raw)
                        except ValueError:
                            continue
                        if isinstance(parsed, list):
                            for it in parsed:
                                if isinstance(it, Mapping):
                                    self.ingest_message(it)
                        elif isinstance(parsed, Mapping):
                            self.ingest_message(parsed)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("RTDS stream reconnecting (%s)", exc)
                self.reconnects += 1
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def close(self) -> None:
        await self.stop()

    def stats(self) -> dict:
        latest = {a.upper(): round(v[1], 4 if v[1] < 10 else 2) for a, v in self._latest_twap.items()}
        return {
            "assets": len(self.assets),
            "messages": self.messages,
            "reconnects": self.reconnects,
            "last_msg_at": round(self.last_msg_at, 1),
            "twap": latest,
        }


def build_short_crypto_slugs(now: float | None = None, assets: Iterable[str] = ("btc", "eth", "sol", "xrp")) -> list[str]:
    """Deterministic Gamma event slugs for active & just-finished 5m, 15m, and 1h crypto markets."""
    ts = int(time.time() if now is None else now)
    cur_5m = ts - (ts % 300)
    cur_15m = ts - (ts % 900)
    now_et = datetime.fromtimestamp(ts, ET_ZONE)
    cur_hr = now_et.replace(minute=0, second=0, microsecond=0)
    prev_hr = cur_hr - timedelta(hours=1)
    next_hr = cur_hr + timedelta(hours=1)

    d_prev, h_prev = fmt_et_hour_slug(prev_hr)
    d_cur, h_cur = fmt_et_hour_slug(cur_hr)
    d_next, h_next = fmt_et_hour_slug(next_hr)

    slugs: list[str] = []
    for a in assets:
        spec = SHORT_ASSETS.get(a.lower())
        if not spec:
            continue
        full = spec["full"]
        slugs.extend(
            [
                f"{a}-updown-5m-{cur_15m - 300}",
                f"{a}-updown-5m-{cur_5m - 300}",
                f"{a}-updown-5m-{cur_5m}",
                f"{a}-updown-5m-{cur_5m + 300}",
                f"{a}-updown-15m-{cur_15m - 900}",
                f"{a}-updown-15m-{cur_15m}",
                f"{full}-up-or-down-{d_prev}-{h_prev}-et",
                f"{full}-up-or-down-{d_cur}-{h_cur}-et",
                f"{full}-above-on-{d_next}-{h_next}-et",
            ]
        )
    return list(dict.fromkeys(slugs))


async def fetch_short_crypto_events(
    http: httpx.AsyncClient,
    rtds_feed: ChainlinkRtdsFeed | None = None,
    now: float | None = None,
    base_url: str = GAMMA_URL,
) -> list[Event]:
    """Fetch active & just-ended 5m/15m/1h events by slug and seed exact ``priceToBeat`` benchmarks."""
    slugs = build_short_crypto_slugs(now=now)
    sem = asyncio.Semaphore(10)
    base = base_url.rstrip("/")

    async def _one(sl: str) -> Event | None:
        async with sem:
            try:
                resp = await http.get(f"{base}/events", params={"slug": sl})
                if resp.status_code != 200:
                    return None
                arr = resp.json()
                if not isinstance(arr, list) or not arr:
                    return None
                return parse_event(arr[0])
            except Exception:
                return None

    fetched = [ev for ev in await asyncio.gather(*(_one(s) for s in slugs)) if ev is not None]

    # Seed exact Chainlink TWAP benchmarks from Gamma eventMetadata (priceToBeat & prior-window finalPrice)!
    if rtds_feed is not None:
        for ev in fetched:
            if not (ev.oracle_sig or "").startswith("chainlink_twap60:"):
                continue
            asset = ev.oracle_sig.split(":", 1)[1]
            if ev.start_time and ev.price_to_beat:
                rtds_feed.seed_benchmark(asset, int(ev.start_time.timestamp()), ev.price_to_beat)
            if ev.end_date and ev.final_price:
                rtds_feed.seed_benchmark(asset, int(ev.end_date.timestamp()), ev.final_price)

    # Keep tradeable events (including 1-market Up/Down events and just-ended windows within 90s that still accept orders)
    out = [ev for ev in fetched if ev.tradeable_markets]
    return out
