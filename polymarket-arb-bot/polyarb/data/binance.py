"""Real-time Binance Spot closed-kline feed for deterministic crypto settlement locks.

Polymarket's hourly, daily noon, weekly, and monthly crypto price events resolve
exclusively from **closed** Binance Spot USDT candles (``BTCUSDT``, ``ETHUSDT``,
``SOLUSDT``, ``XRPUSDT``, ``BNBUSDT``, ``DOGEUSDT``):

1. Hourly ``"<Coin> above ___ on <Date>, <Hour> ET?"``:
   * Resolves to ``YES`` iff the **1-hour candle** ending at ``<Hour>:00:00 ET``
     (``openTime = (<Hour>-1):00:00 ET``, ``closeTime = <Hour>:00:00 ET - 1ms``)
     has ``Close > K``.
2. Daily ``"<Coin> price on <Date>?"`` & ``"<Coin> above ___ on <Date>?"``:
   * Resolves on the **1-minute candle** at ``12:00 ET`` (noon). To eliminate any
     boundary convention ambiguity between the ``11:59->12:00`` and ``12:00->12:01``
     bars, ``noon_close_band`` records ``(min_close, max_close)`` once BOTH bars
     are closed (~61s after noon); any strike strictly outside that band is 100%
     unambiguously decided.
3. Barrier ``"What price will <Coin> hit on <Date> / <Range> / in <Month>?"``:
   * Resolves ``YES`` as soon as any **closed 1-minute candle** inside the ET
     date window has ``High >= K`` (``↑``) or ``Low <= K`` (``↓``), and ``NO``
     once the final ``23:59:59.999 ET`` candle of the window has closed without
     breaching ``K``.

Uses ``https://data-api.binance.vision/api/v3/klines`` (Binance's global public
market-data CDN, accessible from JP/US/EU without geo-restriction). Unclosed
in-progress candles (``closeTime >= now_ms``) are strictly ignored.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import httpx

BINANCE_DATA_URLS: tuple[str, ...] = (
    "https://data-api.binance.vision/api/v3/klines",
    "https://api1.binance.com/api/v3/klines",
    "https://api4.binance.com/api/v3/klines",
)
ET_ZONE = ZoneInfo("America/New_York")
DEFAULT_SYMBOLS: tuple[str, ...] = ("BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT")
log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class CryptoDaySummary:
    symbol: str
    et_date: str  # "YYYY-MM-DD" in America/New_York
    high: float
    low: float
    day_finished: bool
    last_close_ms: int


def is_binance_burst_window(now: float | None = None) -> bool:
    """Poll sub-second right around every hour boundary (:59:56..:00:12) and noon+1m (:00:56..:01:12)."""
    t = now or time.time()
    dt = datetime.fromtimestamp(t, tz=timezone.utc)
    sec_in_hr = dt.minute * 60 + dt.second
    if sec_in_hr >= 3596 or sec_in_hr <= 12:
        return True
    if 56 <= sec_in_hr <= 72:
        return True
    return False


class BinanceFeed:
    """Tracks immutable closed Binance Spot 1h and 1m candles in ET calendar coordinates."""

    def __init__(
        self,
        *,
        burst_poll_s: float = 0.5,
        idle_poll_s: float = 6.0,
        symbols: Iterable[str] = DEFAULT_SYMBOLS,
        on_symbol_update: Callable[[str], None] | None = None,
        http: httpx.AsyncClient | None = None,
        urls: tuple[str, ...] = BINANCE_DATA_URLS,
    ) -> None:
        self.burst_poll_s = burst_poll_s
        self.idle_poll_s = idle_poll_s
        self.symbols: tuple[str, ...] = tuple(sorted({s.upper().replace("_", "") for s in symbols if s}))
        self.on_symbol_update = on_symbol_update
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=10.0)
        self.urls = urls
        self._url_idx = 0

        # (symbol, end_et_key "YYYY-MM-DD HH:00") -> closed 1h Close price
        self._hourly_closes: dict[tuple[str, str], float] = {}
        # (symbol, et_date "YYYY-MM-DD") -> (min(c_1159, c_1200), max(c_1159, c_1200))
        self._noon_bands: dict[tuple[str, str], tuple[float, float]] = {}
        self._noon_parts: dict[tuple[str, str], dict[int, float]] = {}
        # (symbol, et_date "YYYY-MM-DD") -> [min_low, max_high, last_close_ms]
        self._day_extrema: dict[tuple[str, str], list[float]] = {}
        self._latest_et_date: dict[str, str] = {}

        self._task: asyncio.Task | None = None
        self.polls = 0
        self.last_poll_at = 0.0
        self.updates = 0

    def set_symbols(self, symbols: Iterable[str]) -> None:
        merged = sorted({*(self.symbols), *(s.upper().replace("_", "") for s in symbols if s)})
        self.symbols = tuple(merged)

    def hourly_close(self, symbol: str, end_et_key: str) -> float | None:
        """Return closed 1h candle Close ending at ``end_et_key`` ('YYYY-MM-DD HH:00' ET), or None if not yet closed."""
        return self._hourly_closes.get((symbol.upper().replace("_", ""), end_et_key))

    def noon_close_band(self, symbol: str, et_date: str) -> tuple[float, float] | None:
        """Return (lo_close, hi_close) across closed 11:59 & 12:00 ET 1m bars, or None until both close."""
        return self._noon_bands.get((symbol.upper().replace("_", ""), et_date))

    def period_summary(self, symbol: str, start_date: date, end_date: date) -> tuple[float, float, bool] | None:
        """Return (min_low, max_high, period_finished) across closed bars in [start_date..end_date] ET."""
        sym = symbol.upper().replace("_", "")
        cur_str = start_date.strftime("%Y-%m-%d")
        end_str = end_date.strftime("%Y-%m-%d")
        lo = float("inf")
        hi = float("-inf")
        any_seen = False
        for (s, d_str), vals in self._day_extrema.items():
            if s == sym and cur_str <= d_str <= end_str:
                any_seen = True
                if vals[0] < lo:
                    lo = vals[0]
                if vals[1] > hi:
                    hi = vals[1]
        if not any_seen:
            return None
        latest = self._latest_et_date.get(sym, "")
        # Complete period-end NO lock requires us to have observed from start_date AND moved past end_date.
        has_start = (sym, cur_str) in self._day_extrema
        finished = has_start and latest > end_str
        return lo, hi, finished

    def ingest_klines(self, symbol: str, interval: str, rows: Iterable[list], now_ms: int | None = None) -> bool:
        """Ingest raw Binance kline rows, strictly ignoring any bar with ``closeTime >= now_ms``."""
        sym = symbol.upper().replace("_", "")
        cutoff_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
        changed = False

        for row in rows:
            if not isinstance(row, (list, tuple)) or len(row) < 7:
                continue
            open_ms = int(row[0])
            close_ms = int(row[6])
            if close_ms >= cutoff_ms:
                continue  # NEVER trust an unclosed in-progress candle!
            high_p = float(row[2])
            low_p = float(row[3])
            close_p = float(row[4])

            open_et = datetime.fromtimestamp(open_ms / 1000.0, ET_ZONE)
            et_date = open_et.strftime("%Y-%m-%d")

            # Update daily running high/low (1m & 1h closed candles share exact trade-tape extrema)
            prev = self._day_extrema.get((sym, et_date))
            if prev is None:
                self._day_extrema[(sym, et_date)] = [low_p, high_p, float(close_ms)]
                changed = True
            else:
                if low_p < prev[0]:
                    prev[0] = low_p
                    changed = True
                if high_p > prev[1]:
                    prev[1] = high_p
                    changed = True
                if close_ms > prev[2]:
                    prev[2] = float(close_ms)

            if et_date > self._latest_et_date.get(sym, ""):
                old_latest = self._latest_et_date.get(sym, "")
                self._latest_et_date[sym] = et_date
                if old_latest:
                    changed = True

            if interval == "1h":
                end_et = datetime.fromtimestamp((close_ms + 1) / 1000.0, ET_ZONE)
                end_key = end_et.strftime("%Y-%m-%d %H:00")
                if self._hourly_closes.get((sym, end_key)) != close_p:
                    self._hourly_closes[(sym, end_key)] = close_p
                    changed = True
            elif interval == "1m":
                if open_et.hour == 11 and open_et.minute == 59:
                    parts = self._noon_parts.setdefault((sym, et_date), {})
                    parts[1159] = close_p
                elif open_et.hour == 12 and open_et.minute == 0:
                    parts = self._noon_parts.setdefault((sym, et_date), {})
                    parts[1200] = close_p
                parts = self._noon_parts.get((sym, et_date))
                if parts and 1159 in parts and 1200 in parts:
                    band = (min(parts[1159], parts[1200]), max(parts[1159], parts[1200]))
                    if self._noon_bands.get((sym, et_date)) != band:
                        self._noon_bands[(sym, et_date)] = band
                        changed = True

        if changed:
            self.updates += 1
        return changed

    async def _fetch_klines(self, symbol: str, interval: str, limit: int, start_ms: int | None = None) -> list:
        params: dict[str, str | int] = {"symbol": symbol, "interval": interval, "limit": limit}
        if start_ms is not None:
            params["startTime"] = start_ms
        for offset in range(len(self.urls)):
            idx = (self._url_idx + offset) % len(self.urls)
            try:
                resp = await self._http.get(self.urls[idx], params=params)
                if resp.status_code == 200:
                    self._url_idx = idx
                    data = resp.json()
                    return data if isinstance(data, list) else []
            except Exception as exc:
                log.debug("binance fetch %s %s failed via %s: %s", symbol, interval, self.urls[idx], exc)
        return []

    async def poll_once(self, bootstrap: bool = False) -> set[str]:
        if not self.symbols:
            return set()
        changed_syms: set[str] = set()
        now_ms = int(time.time() * 1000)
        now_et = datetime.now(ET_ZONE)

        async def _poll_sym(sym: str) -> None:
            s_changed = False
            if bootstrap:
                # 750 hourly bars (~31 days) seed full month/week highs & lows + recent hourly closes in 1 request!
                h_rows = await self._fetch_klines(sym, "1h", 750)
                if self.ingest_klines(sym, "1h", h_rows, now_ms=now_ms):
                    s_changed = True
                m_rows = await self._fetch_klines(sym, "1m", 120)
                if self.ingest_klines(sym, "1m", m_rows, now_ms=now_ms):
                    s_changed = True
                # Also fetch today's 11:59-12:01 ET noon bars if past 12:00 ET
                noon_open = now_et.replace(hour=11, minute=59, second=0, microsecond=0)
                if now_et >= noon_open:
                    n_rows = await self._fetch_klines(sym, "1m", 4, start_ms=int(noon_open.timestamp() * 1000))
                    if self.ingest_klines(sym, "1m", n_rows, now_ms=now_ms):
                        s_changed = True
            else:
                h_rows, m_rows = await asyncio.gather(
                    self._fetch_klines(sym, "1h", 3),
                    self._fetch_klines(sym, "1m", 5),
                )
                if self.ingest_klines(sym, "1h", h_rows, now_ms=now_ms):
                    s_changed = True
                if self.ingest_klines(sym, "1m", m_rows, now_ms=now_ms):
                    s_changed = True
            if s_changed:
                changed_syms.add(sym)

        await asyncio.gather(*(_poll_sym(s) for s in self.symbols))
        self.polls += 1
        self.last_poll_at = time.time()
        if changed_syms and self.on_symbol_update:
            for sym in changed_syms:
                self.on_symbol_update(sym)
        return changed_syms

    async def start(self) -> None:
        await self.stop()
        if not self.symbols:
            return
        await self.poll_once(bootstrap=True)
        self._task = asyncio.create_task(self._loop(), name="binance-feed")

    async def _loop(self) -> None:
        while True:
            wait = self.burst_poll_s if is_binance_burst_window() else self.idle_poll_s
            await asyncio.sleep(wait)
            try:
                await self.poll_once(bootstrap=False)
            except Exception as exc:
                log.warning("binance feed loop error: %s", exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def close(self) -> None:
        await self.stop()
        if self._own_http:
            await self._http.aclose()

    def stats(self) -> dict:
        return {
            "symbols": len(self.symbols),
            "hourly_closes": len(self._hourly_closes),
            "days_tracked": len(self._day_extrema),
            "polls": self.polls,
            "updates": self.updates,
            "last_poll_at": round(self.last_poll_at, 1),
        }
