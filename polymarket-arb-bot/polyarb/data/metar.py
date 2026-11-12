"""Real-time NOAA Aviation Weather Center (AWC) METAR feed for temperature markets.

Polymarket daily high/low temperature markets resolve directly from routine METAR
observations published at ``https://aviationweather.gov/api/data/metar``:

* Lines starting with ``SPECI`` (special observations) do not count.
* Temperature is read from the 8-digit ``T`` remark (tenths of °C) when present,
  otherwise from the body ``temp/dew`` group (whole °C).
* Values in °F use ``°C * 9 / 5 + 32``; both °C and °F round half-up
  (``floor(x + 0.5)``) to whole degrees.
* Each observation belongs to the calendar date in the station's local timezone.
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
from zoneinfo import ZoneInfo

import httpx

AWC_METAR_URL = "https://aviationweather.gov/api/data/metar"
STATION_CHUNK = 8  # 8 stations * ~36h < AWC's 400-row JSON cap
log = logging.getLogger(__name__)

# Every ICAO airport used by Polymarket weather markets plus major global hubs.
ICAO_TIMEZONES: dict[str, str] = {
    "CYYC": "America/Edmonton",
    "CYYZ": "America/Toronto",
    "CYVR": "America/Vancouver",
    "EDDB": "Europe/Berlin",
    "EDDF": "Europe/Berlin",
    "EDDM": "Europe/Berlin",
    "EFHK": "Europe/Helsinki",
    "EGLC": "Europe/London",
    "EGLL": "Europe/London",
    "EHAM": "Europe/Amsterdam",
    "EIDW": "Europe/Dublin",
    "EPWA": "Europe/Warsaw",
    "FACT": "Africa/Johannesburg",
    "FAOR": "Africa/Johannesburg",
    "HECA": "Africa/Cairo",
    "KATL": "America/New_York",
    "KAUS": "America/Chicago",
    "KBOS": "America/New_York",
    "KDAL": "America/Chicago",
    "KDCA": "America/New_York",
    "KDEN": "America/Denver",
    "KDFW": "America/Chicago",
    "KDTW": "America/Detroit",
    "KHOU": "America/Chicago",
    "KIAD": "America/New_York",
    "KIAH": "America/Chicago",
    "KJFK": "America/New_York",
    "KLAS": "America/Los_Angeles",
    "KLAX": "America/Los_Angeles",
    "KLGA": "America/New_York",
    "KMIA": "America/New_York",
    "KMSP": "America/Chicago",
    "KORD": "America/Chicago",
    "KPHL": "America/New_York",
    "KPHX": "America/Phoenix",
    "KSEA": "America/Los_Angeles",
    "KSFO": "America/Los_Angeles",
    "LEBL": "Europe/Madrid",
    "LEMD": "Europe/Madrid",
    "LFPB": "Europe/Paris",
    "LFPG": "Europe/Paris",
    "LGAV": "Europe/Athens",
    "LIMC": "Europe/Rome",
    "LIRF": "Europe/Rome",
    "LLBG": "Asia/Jerusalem",
    "LOWW": "Europe/Vienna",
    "LPPT": "Europe/Lisbon",
    "LSZH": "Europe/Zurich",
    "LTAC": "Europe/Istanbul",
    "LTFM": "Europe/Istanbul",
    "MMMX": "America/Mexico_City",
    "MPMG": "America/Panama",
    "NZAA": "Pacific/Auckland",
    "NZWN": "Pacific/Auckland",
    "OEJN": "Asia/Riyadh",
    "OERK": "Asia/Riyadh",
    "OMDB": "Asia/Dubai",
    "OPKC": "Asia/Karachi",
    "OPLA": "Asia/Karachi",
    "RCSS": "Asia/Taipei",
    "RCTP": "Asia/Taipei",
    "RJAA": "Asia/Tokyo",
    "RJBB": "Asia/Tokyo",
    "RJTT": "Asia/Tokyo",
    "RKPK": "Asia/Seoul",
    "RKSI": "Asia/Seoul",
    "RKSS": "Asia/Seoul",
    "RPLL": "Asia/Manila",
    "SAEZ": "America/Argentina/Buenos_Aires",
    "SBGL": "America/Sao_Paulo",
    "SBGR": "America/Sao_Paulo",
    "SCEL": "America/Santiago",
    "SEQM": "America/Guayaquil",
    "SKBO": "America/Bogota",
    "SPJC": "America/Lima",
    "UUEE": "Europe/Moscow",
    "UUWW": "Europe/Moscow",
    "VABB": "Asia/Kolkata",
    "VHHH": "Asia/Hong_Kong",
    "VIDP": "Asia/Kolkata",
    "VILK": "Asia/Kolkata",
    "VTBS": "Asia/Bangkok",
    "WIII": "Asia/Jakarta",
    "WMKK": "Asia/Kuala_Lumpur",
    "WSSS": "Asia/Singapore",
    "YMML": "Australia/Melbourne",
    "YSSY": "Australia/Sydney",
    "ZBAA": "Asia/Shanghai",
    "ZGGG": "Asia/Shanghai",
    "ZGSZ": "Asia/Shanghai",
    "ZHCC": "Asia/Shanghai",
    "ZHHH": "Asia/Shanghai",
    "ZSJN": "Asia/Shanghai",
    "ZSPD": "Asia/Shanghai",
    "ZSQD": "Asia/Shanghai",
    "ZUCK": "Asia/Shanghai",
    "ZUUU": "Asia/Shanghai",
}

_T_REMARK_RE = re.compile(r"\bT([01])(\d{3})\d{4}\b")
_Z_TIME_RE = re.compile(r"\b\d{6}Z\b")
_BODY_TEMP_RE = re.compile(r"\b(M?\d{1,2})/(?:M?\d{1,2}|//)\b")


def round_half_up(value: float) -> int:
    """Polymarket rounding rule: exact halves (.5) round toward +infinity (16.5 -> 17, -8.5 -> -8)."""
    return math.floor(value + 0.5 + 1e-9)


def to_whole_degrees(temp_c: float, unit: str) -> int:
    raw = temp_c * 9.0 / 5.0 + 32.0 if unit == "F" else temp_c
    return round_half_up(raw)


def parse_metar_temp_c(raw_ob: str) -> float | None:
    """Extract qualifying routine METAR temperature in °C, or None for SPECI / missing temperature."""
    line = (raw_ob or "").strip()
    if not line or line.upper().startswith("SPECI"):
        return None
    m = _T_REMARK_RE.search(line)
    if m:
        sign = -1.0 if m.group(1) == "1" else 1.0
        return sign * int(m.group(2)) / 10.0
    z_pos = _Z_TIME_RE.search(line)
    body = line[z_pos.end():] if z_pos else line
    rmk = body.find(" RMK ")
    if rmk != -1:
        body = body[:rmk]
    m2 = _BODY_TEMP_RE.search(body)
    if m2:
        tok = m2.group(1)
        return float(-int(tok[1:]) if tok.startswith("M") else int(tok))
    return None


def filter_glitches(obs: list[tuple[int, float]], max_jump_c: float = 12.0) -> list[tuple[int, float]]:
    """Drop an isolated single-observation sensor glitch unless corroborated by at least one other reading."""
    if len(obs) < 3 or max_jump_c <= 0:
        return obs
    ordered = sorted(obs, key=lambda item: item[0])
    temps = sorted(tc for _, tc in ordered)
    median_c = temps[len(temps) // 2]
    out: list[tuple[int, float]] = []
    for ts, tc in ordered:
        others = [other_tc for other_ts, other_tc in ordered if other_ts != ts]
        if abs(tc - median_c) > max_jump_c and all(abs(tc - other_tc) > max_jump_c for other_tc in others):
            log.warning("ignoring isolated METAR spike %.1f°C at ts=%d (median=%.1f°C)", tc, ts, median_c)
            continue
        out.append((ts, tc))
    return out


@dataclass(frozen=True, slots=True)
class StationDaySummary:
    icao: str
    obs_date: str
    count: int
    min_c: float
    max_c: float
    min_f: int
    max_f: int
    min_c_whole: int
    max_c_whole: int
    day_finished: bool
    last_obs_ts: int

    def stat(self, mode: str, unit: str) -> int:
        if mode == "high":
            return self.max_f if unit == "F" else self.max_c_whole
        return self.min_f if unit == "F" else self.min_c_whole


def is_metar_release_window(now: float | None = None) -> bool:
    """Routine METARs transmit around :00, :20, :30 and :50 of every hour."""
    minute = datetime.fromtimestamp(now or time.time(), tz=timezone.utc).minute
    return (minute % 30 <= 5) or (20 <= minute <= 25) or (50 <= minute <= 55)


class MetarFeed:
    """Maintains per-(station, local_date) running METAR highs/lows and notifies listeners on updates."""

    def __init__(
        self,
        *,
        fast_poll_s: float = 4.0,
        idle_poll_s: float = 15.0,
        glitch_delta_c: float = 12.0,
        on_station_update: Callable[[str], None] | None = None,
        http: httpx.AsyncClient | None = None,
        url: str = AWC_METAR_URL,
    ) -> None:
        self.fast_poll_s = fast_poll_s
        self.idle_poll_s = idle_poll_s
        self.glitch_delta_c = glitch_delta_c
        self.on_station_update = on_station_update
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self.url = url
        self.stations: tuple[str, ...] = ()
        self._obs: dict[tuple[str, str], dict[int, float]] = {}
        self._latest_date: dict[str, str] = {}
        self._summaries: dict[tuple[str, str], StationDaySummary] = {}
        self._task: asyncio.Task | None = None
        self.polls = 0
        self.last_poll_at = 0.0
        self.updates = 0

    def set_stations(self, stations: Iterable[str]) -> None:
        known = sorted({s.upper() for s in stations if s and s.upper() in ICAO_TIMEZONES})
        self.stations = tuple(known)

    def get(self, icao: str | None, obs_date: str | None) -> StationDaySummary | None:
        if not icao or not obs_date:
            return None
        return self._summaries.get((icao.upper(), obs_date))

    def ingest_rows(self, rows: Iterable[dict]) -> set[str]:
        """Ingest raw JSON rows from AWC and return the set of ICAO codes whose summary changed."""
        touched_keys: set[tuple[str, str]] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            icao = str(row.get("icaoId") or "").upper()
            obs_ts = row.get("obsTime")
            raw_ob = str(row.get("rawOb") or "")
            tz_name = ICAO_TIMEZONES.get(icao)
            if not tz_name or not obs_ts:
                continue
            tc = parse_metar_temp_c(raw_ob)
            if tc is None:
                continue
            ts = int(obs_ts)
            local_date = datetime.fromtimestamp(ts, ZoneInfo(tz_name)).strftime("%Y-%m-%d")
            bucket = self._obs.setdefault((icao, local_date), {})
            if bucket.get(ts) != tc:
                bucket[ts] = tc
                touched_keys.add((icao, local_date))
            prev_latest = self._latest_date.get(icao, "")
            if local_date > prev_latest:
                self._latest_date[icao] = local_date
                # A new local date finishes all earlier dates for this station.
                for k_icao, k_date in list(self._obs):
                    if k_icao == icao and k_date < local_date:
                        touched_keys.add((k_icao, k_date))

        changed_icaos: set[str] = set()
        for icao, d_str in touched_keys:
            raw_pts = list(self._obs.get((icao, d_str), {}).items())
            clean = filter_glitches(raw_pts, self.glitch_delta_c)
            if not clean:
                continue
            c_vals = [tc for _, tc in clean]
            f_vals = [to_whole_degrees(tc, "F") for tc in c_vals]
            cw_vals = [to_whole_degrees(tc, "C") for tc in c_vals]
            summary = StationDaySummary(
                icao=icao,
                obs_date=d_str,
                count=len(clean),
                min_c=min(c_vals),
                max_c=max(c_vals),
                min_f=min(f_vals),
                max_f=max(f_vals),
                min_c_whole=min(cw_vals),
                max_c_whole=max(cw_vals),
                day_finished=self._latest_date.get(icao, "") > d_str,
                last_obs_ts=max(ts for ts, _ in clean),
            )
            if self._summaries.get((icao, d_str)) != summary:
                self._summaries[(icao, d_str)] = summary
                changed_icaos.add(icao)
                self.updates += 1
        return changed_icaos

    async def poll_once(self, hours: int = 3) -> set[str]:
        if not self.stations:
            return set()
        chunks = [
            self.stations[i : i + STATION_CHUNK]
            for i in range(0, len(self.stations), STATION_CHUNK)
        ] if hours > 6 else [self.stations[i : i + 32] for i in range(0, len(self.stations), 32)]

        async def _fetch(chunk: tuple[str, ...]) -> list[dict]:
            try:
                resp = await self._http.get(
                    self.url,
                    params={"ids": ",".join(chunk), "format": "json", "hours": hours},
                )
                if resp.status_code != 200:
                    return []
                data = resp.json()
                return data if isinstance(data, list) else []
            except Exception as exc:
                log.debug("METAR poll failed for %s: %s", chunk[:3], exc)
                return []

        batches = await asyncio.gather(*(_fetch(c) for c in chunks))
        rows = [row for batch in batches for row in batch]
        self.polls += 1
        self.last_poll_at = time.time()
        changed = self.ingest_rows(rows)
        if changed and self.on_station_update:
            for icao in changed:
                self.on_station_update(icao)
        return changed

    async def start(self) -> None:
        await self.stop()
        if not self.stations:
            return
        await self.poll_once(hours=36)
        self._task = asyncio.create_task(self._loop(), name="metar-feed")

    async def _loop(self) -> None:
        full_sync_at = time.monotonic() + 900.0
        while True:
            wait = self.fast_poll_s if is_metar_release_window() else self.idle_poll_s
            await asyncio.sleep(wait)
            hours = 36 if time.monotonic() >= full_sync_at else 3
            if hours == 36:
                full_sync_at = time.monotonic() + 900.0
            try:
                await self.poll_once(hours=hours)
            except Exception as exc:
                log.warning("METAR loop error: %s", exc)

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
            "stations": len(self.stations),
            "days_tracked": len(self._summaries),
            "polls": self.polls,
            "updates": self.updates,
            "last_poll_at": round(self.last_poll_at, 1),
        }
