#!/usr/bin/env python3
"""Predict high-probability 5m favorite reverses *before* the 97-98c lift.

Prior tape study (reverse_30d): at fill time the Polymarket book/tape of
losers looks like winners. The actual 0/1 is Chainlink TWAP vs price-to-beat.
This script joins official Gamma PTB/finalPrice with Binance 1s (USDT) as a
path proxy, then tests skip rules a live taker could run if it also saw the
TWAP vs PTB.

Hold out the newest 10 days. Do not promote a rule that only wins on holdout.
"""

from __future__ import annotations

import io
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reverse_30d as r30

OUT = Path(__file__).with_name("reverse_predict.json")
CACHE = Path(os.environ.get("REVERSE_30D_CACHE", "/tmp/reverse_30d_cache"))
BN_ROOT = Path(os.environ.get("BINANCE_1S_CACHE", "/tmp/binance_1s"))
FILLS_PATH = CACHE / "_first_one.json"
META_PATH = CACHE / "_gamma_meta.json"
UA = {
    "User-Agent": "surf-arb-research/1.8 (read-only; reverse prediction; no trading)",
    "Accept": "application/json",
}
BN_UA = {"User-Agent": "Mozilla/5.0 surf-arb-research/1.8"}
VISION_API = "https://data-api.binance.vision/api/v3/klines"
VISION_ZIP = "https://data.binance.vision/data/spot/daily/klines"
SYMBOL = {"btc": "BTCUSDT", "eth": "ETHUSDT"}
HOLDOUT_DAYS = 10
NOTIONAL = r30.NOTIONAL


def phi(z: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1.0 + math.erf(float(z) / math.sqrt(2.0)))


def fair_p_stay(lead_bps: float, vol_bps_sqrt_s: float, left_s: float) -> float | None:
    """P(still on the favorite side at snapshot close) under a BM approximation."""
    if vol_bps_sqrt_s is None or vol_bps_sqrt_s <= 1e-9 or left_s <= 0:
        return None
    return round(phi(lead_bps / (vol_bps_sqrt_s * math.sqrt(left_s))), 6)


def to_sec(raw: int) -> int:
    t = int(raw)
    if t > 10**17:
        return t // 10**9
    if t > 10**14:
        return t // 10**6
    if t > 10**11:
        return t // 10**3
    return t


def http_json(url: str, timeout: float = 25.0, tries: int = 5, ua=None):
    last = None
    headers = ua or UA
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                return None
            if exc.code in {429, 451, 500, 502, 503, 403}:
                time.sleep(0.4 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.35 * (2**i))
    if last:
        raise last
    return None


def infer_lookback(source: str, cfg: dict | None) -> int:
    if isinstance(cfg, dict) and cfg.get("twapLookbackSeconds"):
        return int(cfg["twapLookbackSeconds"])
    src = (source or "").lower()
    if "twap-60" in src or "twap_60" in src:
        return 60
    if "twap-30" in src or "twap_30" in src:
        return 30
    return 1


def parse_meta(ev: dict) -> dict | None:
    market = (ev.get("markets") or [{}])[0]
    if not isinstance(market, dict):
        return None
    md = ev.get("eventMetadata") or {}
    if not isinstance(md, dict):
        md = {}
    cfg = market.get("cryptoMarketConfig") if isinstance(market.get("cryptoMarketConfig"), dict) else {}
    source = str(market.get("resolutionSource") or ev.get("resolutionSource") or "")
    ptb = md.get("priceToBeat")
    final = md.get("finalPrice")
    try:
        ptb_f = float(ptb) if ptb is not None else None
    except (TypeError, ValueError):
        ptb_f = None
    try:
        final_f = float(final) if final is not None else None
    except (TypeError, ValueError):
        final_f = None
    if ptb_f is None:
        return None
    return {
        "slug": str(ev.get("slug") or ""),
        "ptb": ptb_f,
        "final": final_f,
        "lookback": infer_lookback(source, cfg),
        "source": source,
        "twap_enabled": bool(cfg.get("twapEnabled")) if cfg else ("twap" in source.lower()),
        "cfg_id": str(cfg.get("id") or "") if cfg else "",
    }


def list_gamma_meta(oldest_end: int, newest_end: int) -> dict:
    """Re-list 30d series pages (they already carry eventMetadata)."""
    if META_PATH.exists():
        try:
            cached = json.loads(META_PATH.read_text())
            if isinstance(cached, dict) and len(cached) > 10000:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    out: dict = {}
    for asset in r30.ASSETS:
        rows = []
        seen: set[str] = set()
        end_max = None
        stalled = 0
        pages = 0
        while pages < 400:
            pages += 1
            url = (
                f"{r30.GAMMA}/events?series_id={r30.SERIES[asset]}&closed=true"
                f"&order=endDate&ascending=false&limit=100"
            )
            if end_max:
                url += f"&end_date_max={end_max}"
            page = r30.get_json(url) or []
            if not page:
                break
            added = 0
            stop = False
            last_end_iso = None
            for ev in page:
                slug = str(ev.get("slug") or "")
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                market = (ev.get("markets") or [{}])[0]
                if not isinstance(market, dict):
                    continue
                end_iso = market.get("endDate") or ev.get("endDate")
                if not end_iso:
                    continue
                end = r30.end_ts(end_iso)
                last_end_iso = end_iso
                if end < oldest_end:
                    stop = True
                    break
                if end > newest_end:
                    continue
                meta = parse_meta(ev)
                if meta:
                    out[slug] = meta
                    added += 1
                    rows.append(slug)
            if stop:
                break
            if not last_end_iso:
                break
            if added == 0:
                stalled += 1
                end_max = r30.iso_utc(r30.end_ts(last_end_iso) - 300)
                if stalled >= 4:
                    break
                continue
            stalled = 0
            nxt = r30.iso_utc(r30.end_ts(last_end_iso) - 1)
            end_max = r30.iso_utc(r30.end_ts(last_end_iso) - 300) if nxt == end_max else nxt
            time.sleep(0.03)
        print(f"  gamma meta {asset} +{len(rows)} total={len(out)}", flush=True)
    META_PATH.parent.mkdir(parents=True, exist_ok=True)
    META_PATH.write_text(json.dumps(out))
    return out


def fetch_missing_meta(slugs: list[str], have: dict) -> dict:
    miss = [s for s in slugs if s not in have]
    if not miss:
        return have

    def one(slug: str):
        data = http_json(f"{r30.GAMMA}/events?slug={slug}")
        if not data:
            return slug, None
        ev = data[0] if isinstance(data, list) else data
        return slug, parse_meta(ev)

    print(f"  slug-meta missing {len(miss)}", flush=True)
    with ThreadPoolExecutor(max_workers=10) as pool:
        futs = [pool.submit(one, s) for s in miss]
        for i, fut in enumerate(as_completed(futs), 1):
            slug, meta = fut.result()
            if meta:
                have[slug] = meta
            if i % 200 == 0:
                print(f"    slug-meta {i}/{len(miss)}", flush=True)
            time.sleep(0.01)
    META_PATH.write_text(json.dumps(have))
    return have


class SecPx:
    """Second-resolution close prices with small-gap backfill."""

    def __init__(self, t0: int, t1: int):
        self.t0 = int(t0)
        n = int(t1) - self.t0 + 1
        self.px = [0.0] * n
        self.ok = bytearray(n)

    def set(self, ts: int, price: float) -> None:
        i = int(ts) - self.t0
        if 0 <= i < len(self.px) and price > 0:
            self.px[i] = float(price)
            self.ok[i] = 1

    def at(self, ts: int, slack: int = 3) -> float | None:
        for k in range(slack + 1):
            i = int(ts) - self.t0 - k
            if 0 <= i < len(self.ok) and self.ok[i]:
                return self.px[i]
        return None

    def twap(self, ts: int, lookback: int) -> float | None:
        lb = max(int(lookback), 1)
        xs = []
        for k in range(lb):
            p = self.at(ts - k, slack=0)
            if p is None:
                p = self.at(ts - k, slack=2)
            if p is not None:
                xs.append(p)
        if len(xs) < max(1, lb // 2):
            return None
        return sum(xs) / len(xs)

    def realized_vol_bps_sqrt_s(self, ts: int, window: int = 120) -> float | None:
        rets = []
        prev = None
        for k in range(window, -1, -1):
            p = self.at(ts - k, slack=1)
            if p is None:
                continue
            if prev is not None and prev > 0 and p > 0:
                rets.append(math.log(p / prev))
            prev = p
        if len(rets) < 30:
            return None
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / max(len(rets) - 1, 1)
        std = math.sqrt(max(var, 0.0))
        return std * 10000.0  # bps per sqrt(second) ≈ 1s return std in bps

    def max_lead_bps(self, start: int, ts: int, ptb: float, lookback: int) -> float | None:
        if ptb <= 0:
            return None
        mx = None
        step = 5
        t = start
        while t <= ts:
            tw = self.twap(t, lookback)
            if tw is not None:
                lead = (tw - ptb) / ptb * 10000.0
                mx = lead if mx is None else max(mx, lead)
            t += step
        return mx


def download_zip_day(sym: str, day: str) -> int:
    """Fetch one Binance vision daily 1s zip if missing. Returns bytes or 0."""
    path = BN_ROOT / sym / f"{sym}-1s-{day}.zip"
    if path.exists() and path.stat().st_size > 1000:
        return path.stat().st_size
    path.parent.mkdir(parents=True, exist_ok=True)
    url = f"{VISION_ZIP}/{sym}/1s/{sym}-1s-{day}.zip"
    req = urllib.request.Request(url, headers=BN_UA)
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            path.write_bytes(resp.read())
        print(f"  zip {path.name} {path.stat().st_size}", flush=True)
        return path.stat().st_size
    except urllib.error.HTTPError as exc:
        if path.exists():
            path.unlink(missing_ok=True)
        if exc.code != 404:
            print(f"  zip fail {sym} {day} HTTP {exc.code}", flush=True)
        return 0
    except Exception as exc:
        if path.exists():
            path.unlink(missing_ok=True)
        print(f"  zip fail {sym} {day} {type(exc).__name__}", flush=True)
        return 0


def load_zip_day(sym: str, day: str, series: SecPx) -> int:
    path = BN_ROOT / sym / f"{sym}-1s-{day}.zip"
    if not path.exists():
        return 0
    n = 0
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                cols = line.strip().split(",")
                if len(cols) < 5:
                    continue
                try:
                    ts = to_sec(int(float(cols[0])))
                    px = float(cols[4])
                except (ValueError, IndexError):
                    continue
                series.set(ts, px)
                n += 1
    return n


def fetch_vision_1s_range(sym: str, t0: int, t1: int, series: SecPx) -> int:
    """Fill a gap (e.g. today) from data-api.binance.vision 1s klines."""
    n = 0
    ms = t0 * 1000
    end_ms = t1 * 1000
    while ms <= end_ms:
        url = f"{VISION_API}?symbol={sym}&interval=1s&startTime={ms}&limit=1000"
        chunk = http_json(url, ua=BN_UA) or []
        if not chunk:
            break
        last = ms
        for row in chunk:
            ts = to_sec(int(row[0]))
            series.set(ts, float(row[4]))
            n += 1
            last = int(row[0])
        nxt = last + 1000
        if nxt <= ms:
            break
        ms = nxt
        if len(chunk) < 1000:
            break
        time.sleep(0.03)
    return n


def load_series(asset: str, t0: int, t1: int) -> SecPx:
    sym = SYMBOL[asset]
    series = SecPx(t0 - 5, t1 + 5)
    d0 = datetime.fromtimestamp(t0, timezone.utc).date()
    d1 = datetime.fromtimestamp(t1, timezone.utc).date()
    day = d0
    while day <= d1:
        iso = day.isoformat()
        n = load_zip_day(sym, iso, series)
        if n == 0:
            download_zip_day(sym, iso)
            n = load_zip_day(sym, iso, series)
        if n == 0:
            a = int(datetime(day.year, day.month, day.day, tzinfo=timezone.utc).timestamp())
            b = a + 86400 - 1
            print(f"  vision-api 1s {sym} {iso}", flush=True)
            fetch_vision_1s_range(sym, max(a, t0 - 5), min(b, t1 + 5), series)
        day += timedelta(days=1)
    return series


def load_fills() -> list[dict]:
    if FILLS_PATH.exists():
        rows = json.loads(FILLS_PATH.read_text())
        if len(rows) > 1000:
            print(f"fills cache {len(rows)}", flush=True)
            return rows
    events = json.loads((CACHE / "_events.json").read_text())
    print(f"rebuild fills from {len(events)} events", flush=True)
    first, _second, _dwell, meta = r30.apply_events(events)
    one = r30.one_per_window(first)
    keep = (
        "slug",
        "asset",
        "start",
        "end",
        "winner",
        "bought",
        "px",
        "tick",
        "left",
        "ts",
        "hour",
        "dow",
        "volume",
        "won",
        "pnl",
        "printed_99",
        "opp20",
        "spike15",
        "fakeout",
        "looked_90",
    )
    slim = [{k: r[k] for k in keep if k in r} for r in one]
    FILLS_PATH.write_text(json.dumps(slim))
    print(f"wrote fills {len(slim)} err={meta['errors']}", flush=True)
    return slim


def sign_lead(bought: str, lead_bps: float | None) -> float | None:
    if lead_bps is None:
        return None
    return lead_bps if bought == "Up" else -lead_bps


def attach_spot(rows: list[dict], meta: dict, series_map: dict[str, SecPx]) -> list[dict]:
    out = []
    miss_px = miss_meta = 0
    for r in rows:
        m = meta.get(r["slug"])
        if not m:
            miss_meta += 1
            continue
        pxsrc = series_map[r["asset"]]
        lb = int(m["lookback"] or 1)
        ptb = float(m["ptb"])
        ts = int(r["ts"])
        start = int(r["start"])
        end = int(r["end"])
        tw_now = pxsrc.twap(ts, lb)
        tw_open = pxsrc.twap(start, lb)
        tw_end = pxsrc.twap(end, lb)
        tw_ago15 = pxsrc.twap(ts - 15, lb)
        spot_now = pxsrc.at(ts)
        if tw_now is None or spot_now is None:
            miss_px += 1
            continue
        lead_ptb = (tw_now - ptb) / ptb * 10000.0
        lead_open = None if tw_open is None else (tw_now - tw_open) / tw_open * 10000.0
        mom15 = None
        if tw_ago15 is not None and tw_ago15 > 0:
            mom15 = (tw_now - tw_ago15) / tw_ago15 * 10000.0
        basis_open = None if tw_open is None else (tw_open - ptb) / ptb * 10000.0
        signed_ptb = sign_lead(r["bought"], lead_ptb)
        signed_open = sign_lead(r["bought"], lead_open)
        signed_mom = sign_lead(r["bought"], mom15)
        vol = pxsrc.realized_vol_bps_sqrt_s(ts, 120)
        left = float(r["left"])
        fair_ptb = fair_p_stay(signed_ptb or 0.0, vol, left) if signed_ptb is not None and vol else None
        fair_open = fair_p_stay(signed_open or 0.0, vol, left) if signed_open is not None and vol else None
        # Partial settlement TWAP already observed when left < lookback.
        known = None
        known_frac = None
        if lb > 1 and left < lb:
            known_frac = (lb - left) / lb
            # prices in (end-lb, ts] are already inside the close TWAP window
            known = pxsrc.twap(ts, int(lb - left)) if (lb - left) >= 1 else tw_now
        mx = pxsrc.max_lead_bps(start, ts, ptb, lb)
        signed_max = sign_lead(r["bought"], mx)
        fade = None if signed_max is None or signed_ptb is None else signed_max - signed_ptb
        final = m.get("final")
        official_lead = None if not final else (final - ptb) / ptb * 10000.0
        row = dict(r)
        row.update(
            {
                "ptb": ptb,
                "final": final,
                "lookback": lb,
                "regime": "twap60" if lb >= 60 else ("twap30" if lb >= 30 else "snapshot"),
                "tw_now": round(tw_now, 6),
                "tw_open": None if tw_open is None else round(tw_open, 6),
                "tw_end": None if tw_end is None else round(tw_end, 6),
                "spot_now": round(spot_now, 6),
                "lead_ptb_bps": round(lead_ptb, 4),
                "lead_open_bps": None if lead_open is None else round(lead_open, 4),
                "signed_ptb_bps": None if signed_ptb is None else round(signed_ptb, 4),
                "signed_open_bps": None if signed_open is None else round(signed_open, 4),
                "signed_mom15_bps": None if signed_mom is None else round(signed_mom, 4),
                "basis_open_bps": None if basis_open is None else round(basis_open, 4),
                "vol_bps_sqrt_s": None if vol is None else round(vol, 5),
                "fair_p": fair_ptb,
                "fair_p_open": fair_open,
                "wrong_side_ptb": bool(signed_ptb is not None and signed_ptb < 0),
                "wrong_side_open": bool(signed_open is not None and signed_open < 0),
                "thin_lead_2bps": bool(signed_open is not None and signed_open < 2),
                "thin_lead_5bps": bool(signed_open is not None and signed_open < 5),
                "deep_wrong_2bps": bool(signed_open is not None and signed_open < -2),
                "deep_wrong_5bps": bool(signed_open is not None and signed_open < -5),
                "fade_bps": None if fade is None else round(fade, 4),
                "known_twap": None if known is None else round(known, 6),
                "known_frac": None if known_frac is None else round(known_frac, 4),
                "official_lead_bps": None if official_lead is None else round(official_lead, 4),
            }
        )
        out.append(row)
    print(f"joined {len(out)} miss_meta={miss_meta} miss_px={miss_px}", flush=True)
    return out


def summarize(rows: list[dict]) -> dict:
    return r30.summarize(rows)


def eval_skip(rows: list[dict], pred) -> dict:
    skip = [r for r in rows if pred(r)]
    keep = [r for r in rows if not pred(r)]
    n = len(rows)
    n_lose = sum(1 for r in rows if not r["won"])
    caught = sum(1 for r in skip if not r["won"])
    false_al = sum(1 for r in skip if r["won"])
    prec = None if not skip else caught / len(skip)
    rec = None if not n_lose else caught / n_lose
    lift = None if not (prec and n) or n_lose == 0 else prec / (n_lose / n)
    sk = summarize(skip)
    kp = summarize(keep)
    # EV of skipping one flagged trade at $5 97c mix uses actual pnl of skipped set
    saved = -sk["pnl_usd"]  # skipping them avoids that pnl (negative pnl → positive save)
    return {
        "n_skip": len(skip),
        "n_keep": len(keep),
        "skip_frac": None if not n else round(len(skip) / n, 4),
        "skip_reverse": sk["reverse"],
        "skip_pnl": sk["pnl_usd"],
        "precision": None if prec is None else round(prec, 6),
        "recall": None if rec is None else round(rec, 6),
        "lift": None if lift is None else round(lift, 3),
        "caught_reverses": caught,
        "false_alarms": false_al,
        "keep": kp,
        "saved_usd_if_skipped": round(saved, 2),
        "ev_ok_keep": kp.get("ev_ok"),
        "keep_vs_be": kp.get("vs_be"),
    }


SKIP_RULES = [
    ("wrong_side_open", lambda r: r.get("wrong_side_open") is True),
    ("deep_wrong_open_2bps", lambda r: r.get("deep_wrong_2bps") is True),
    ("deep_wrong_open_5bps", lambda r: r.get("deep_wrong_5bps") is True),
    ("wrong_open_and_left_le_20", lambda r: r.get("wrong_side_open") and r["left"] <= 20),
    ("wrong_open_and_left_le_30", lambda r: r.get("wrong_side_open") and r["left"] <= 30),
    ("wrong_open_and_mom_down", lambda r: r.get("wrong_side_open") and (r.get("signed_mom15_bps") or 0) < 0),
    ("mom15_lt_-3bps", lambda r: r.get("signed_mom15_bps") is not None and r["signed_mom15_bps"] < -3),
    ("mom15_lt_-5bps", lambda r: r.get("signed_mom15_bps") is not None and r["signed_mom15_bps"] < -5),
    ("fair_open_lt_50", lambda r: r.get("fair_p_open") is not None and r["fair_p_open"] < 0.50),
    ("fair_open_lt_80", lambda r: r.get("fair_p_open") is not None and r["fair_p_open"] < 0.80),
    ("fair_open_lt_90", lambda r: r.get("fair_p_open") is not None and r["fair_p_open"] < 0.90),
    ("thin_open_2bps", lambda r: r.get("thin_lead_2bps") is True),
    ("thin_open_5bps", lambda r: r.get("thin_lead_5bps") is True),
    ("wrong_side_ptb_basis_contaminated", lambda r: r.get("wrong_side_ptb") is True),
    ("tape_printed_99", lambda r: bool(r.get("printed_99"))),
    ("tape_spike15", lambda r: bool(r.get("spike15"))),
]


def lead_buckets(rows: list[dict], key: str = "signed_ptb_bps") -> dict:
    edges = [(-1e9, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 10), (10, 1e9)]
    out = {}
    for lo, hi in edges:
        part = [r for r in rows if r.get(key) is not None and lo < r[key] <= hi]
        lab = f"{key}_{lo:g}_to_{hi:g}"
        out[lab] = summarize(part)
    return out


def proxy_quality(rows: list[dict]) -> dict:
    """Does Binance 1s TWAP-at-close agree with official 0/1 / Gamma final?"""
    n = 0
    agree_final_winner = 0
    agree_bn_ptb_winner = 0
    agree_bn_open_winner = 0
    basis = []
    for r in rows:
        if r.get("ptb") is None:
            continue
        n += 1
        up_official = r["winner"] == "Up"
        if r.get("final") is not None:
            up_final = r["final"] >= r["ptb"]
            agree_final_winner += int(up_final == up_official)
        if r.get("tw_end") is not None:
            up_bn_ptb = r["tw_end"] >= r["ptb"]
            agree_bn_ptb_winner += int(up_bn_ptb == up_official)
        if r.get("tw_end") is not None and r.get("tw_open") is not None:
            up_bn_open = r["tw_end"] >= r["tw_open"]
            agree_bn_open_winner += int(up_bn_open == up_official)
        if r.get("basis_open_bps") is not None:
            basis.append(abs(r["basis_open_bps"]))
    basis.sort()
    def pct(xs, p):
        if not xs:
            return None
        return round(xs[min(len(xs) - 1, int(round((len(xs) - 1) * p)))], 3)
    return {
        "n": n,
        "gamma_final_vs_winner": None if not n else round(agree_final_winner / n, 4),
        "binance_twap_end_vs_gamma_ptb_vs_winner": None if not n else round(agree_bn_ptb_winner / n, 4),
        "binance_twap_end_vs_open_vs_winner": None if not n else round(agree_bn_open_winner / n, 4),
        "abs_basis_open_bps_p50": pct(basis, 0.5),
        "abs_basis_open_bps_p90": pct(basis, 0.9),
    }


def split_holdout(rows: list[dict], newest_end: int) -> tuple[list[dict], list[dict]]:
    cut = newest_end - HOLDOUT_DAYS * 86400
    train = [r for r in rows if r["end"] < cut]
    test = [r for r in rows if r["end"] >= cut]
    return train, test


def rank_rules(rows: list[dict]) -> list[dict]:
    ranked = []
    for name, fn in SKIP_RULES:
        ev = eval_skip(rows, fn)
        ev["rule"] = name
        ranked.append(ev)
    ranked.sort(key=lambda x: (x["precision"] or 0, x["recall"] or 0), reverse=True)
    return ranked


def means_win_lose(rows: list[dict], keys: list[str]) -> dict:
    wins = [r for r in rows if r["won"]]
    loses = [r for r in rows if not r["won"]]

    def mean(xs, k):
        vals = [float(x[k]) for x in xs if x.get(k) is not None]
        return None if not vals else round(sum(vals) / len(vals), 4)

    def share(xs, k):
        if not xs:
            return None
        return round(sum(1 for x in xs if x.get(k)) / len(xs), 4)

    out = {"n_win": len(wins), "n_lose": len(loses), "means": {}, "flags": {}}
    for k in keys:
        out["means"][k] = {"win": mean(wins, k), "lose": mean(loses, k)}
    for k in ("wrong_side_ptb", "wrong_side_open", "thin_lead_2bps", "thin_lead_5bps", "deep_wrong_2bps", "deep_wrong_5bps"):
        out["flags"][k] = {"win": share(wins, k), "lose": share(loses, k)}
    return out


def build_findings(report: dict) -> dict:
    rules_all = {r["rule"]: r for r in report["rules_all"]}
    rules_ho = {r["rule"]: r for r in report["rules_holdout"]}
    rules_tr = {r["rule"]: r for r in report["rules_train"]}
    wr = rules_all.get("wrong_side_open") or {}
    wr_h = rules_ho.get("wrong_side_open") or {}
    wr_t = rules_tr.get("wrong_side_open") or {}
    deep = rules_all.get("deep_wrong_open_5bps") or {}
    mom = rules_all.get("mom15_lt_-5bps") or {}
    fair = rules_all.get("fair_open_lt_80") or {}
    anat = report["anatomy"]
    pq = report["proxy_quality"]
    lose_lead = (anat.get("means") or {}).get("signed_open_bps", {}).get("lose")
    win_lead = (anat.get("means") or {}).get("signed_open_bps", {}).get("win")
    lose_fair = (anat.get("means") or {}).get("fair_p_open", {}).get("lose")
    win_fair = (anat.get("means") or {}).get("fair_p_open", {}).get("win")
    wr_prec = None if not wr.get("precision") else round(100 * wr["precision"], 1)
    rare = rules_all.get("wrong_open_and_mom_down") or {}
    return {
        "headline_cantonese": (
            "用官方 PTB + 1 秒現貨路徑之後，仍然冇高精度「大機會反轉」事前閘。"
            f"輸盤入場時同源 lead 約 {lose_lead} bps，贏盤 {win_lead} bps；"
            f"Brownian fair P 輸盤 {lose_fair}、贏盤 {win_fair}（都約 87%），"
            "但你付出 97¢。3% 反轉係剩餘波動嘅尾，入場當刻同贏盤分唔開。"
            f"現貨同大熱反向 precision 只有 {wr_prec}%。"
        ),
        "why_tape_failed": (
            "97–98¢ 鎖盤只係市場暫時相信大熱；真正 0/1 係窗尾 Chainlink TWAP ≥ PTB。"
            "入場後 18–29 秒先砸穿 CLOB，係現貨路徑最後一分鐘先決定；入場當刻 lead 同贏盤無差別。"
        ),
        "why_spot_also_fails_as_a_hard_skip": (
            f"Binance 收市 TWAP vs 開盤 TWAP 同官方贏家一致率 {pq.get('binance_twap_end_vs_open_vs_winner')}，路徑代理可用。"
            f"Binance vs Gamma PTB 只有 {pq.get('binance_twap_end_vs_gamma_ptb_vs_winner')} "
            f"（開盤 basis p50 {pq.get('abs_basis_open_bps_p50')} bps）——唔可以用 USDT 現貨去減 Chainlink PTB。"
            "同源 lead 喺贏／輸盤均值幾乎重疊，skip-if-wrong-side 幾乎等如隨機少做單。"
        ),
        "top_method_attempted": {
            "name": "skip_if_same_source_twap_disagrees_with_favorite",
            "live_if_it_had_worked": (
                "窗開錄 Chainlink TWAP 做 PTB；hunt 當刻讀同一條 TWAP。買 Up 要 TWAP≥PTB。"
                "研究證明 precision 唔夠，唔好當 Rev 22。"
            ),
            "full_sample": wr,
            "train": wr_t,
            "holdout": wr_h,
            "deep_wrong_5bps": deep,
            "mom15_lt_minus_5bps": mom,
            "fair_open_lt_80": fair,
            "wrong_open_and_mom_down_rare": rare,
            "robust_enough_to_consider": False,
        },
        "rare_high_lift_warning": (
            f"wrong_open_and_mom_down 全樣本 precision {rare.get('precision')} n={rare.get('n_skip')}，"
            "但 holdout 只有 1 筆。唔夠穩，唔好上線。"
        ),
        "what_would_actually_predict": [
            "要預測呢 3% 就要預測未來 15–40 秒嘅 Chainlink TWAP 方向，而且要比 CLOB 更快。公開 1s 現貨喺抬 97 嘅當刻已經唔再分開輸贏。",
            "理論上下一步先係 colocation + 官方 TWAP 微結構（settlement 窗未完成嘅 running TWAP）。呢個月 Binance 1s 代理睇唔到可用 lift。",
            "實務上仍然最有效：唔加注、Rev 21 鎖盤、或者唔做呢個帶。入場時 fair≈87% 卻付 97¢，本身就係略負 EV。",
        ],
        "do_not_use": [
            "Binance／USDT 去減 Chainlink PTB（~9 bps 基差）",
            "完場 Gamma volume（前視）",
            "只靠 Polymarket tape（printed_99 / spike）",
            "Holdout 先靚嘅鐘點／只做 BTC／跳過第一 tick",
        ],
        "anatomy_signed_lead": anat.get("flags"),
        "anatomy_means": anat.get("means"),
        "implement": False,
        "note": "冇 Rev 22。公開現貨路徑喺入場當刻預測唔到之後嗰下 steamroller。",
    }


def main() -> None:
    fills = load_fills()
    newest_end = max(r["end"] for r in fills)
    oldest_end = min(r["end"] for r in fills) - 1
    print(f"fills {len(fills)} {r30.iso_utc(oldest_end)} -> {r30.iso_utc(newest_end)}", flush=True)

    print("gamma PTB/final", flush=True)
    meta = list_gamma_meta(oldest_end - 300, newest_end + 300)
    meta = fetch_missing_meta([r["slug"] for r in fills], meta)
    print(f"meta {len(meta)}", flush=True)

    t0 = min(r["start"] for r in fills) - 180
    t1 = max(r["end"] for r in fills) + 5
    series_map = {}
    for asset in r30.ASSETS:
        print(f"load 1s {asset}", flush=True)
        series_map[asset] = load_series(asset, t0, t1)

    joined = attach_spot(fills, meta, series_map)
    train, test = split_holdout(joined, newest_end)
    print(f"joined {len(joined)} train {len(train)} holdout {len(test)}", flush=True)

    rules_all = rank_rules(joined)
    rules_train = rank_rules(train)
    rules_hold = rank_rules(test)

    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_fills": len(fills),
        "n_joined": len(joined),
        "train_n": len(train),
        "holdout_n": len(test),
        "range": {"oldest_end": r30.iso_utc(oldest_end), "newest_end": r30.iso_utc(newest_end)},
        "notional_usd": NOTIONAL,
        "baseline": summarize(joined),
        "baseline_train": summarize(train),
        "baseline_holdout": summarize(test),
        "proxy_quality": proxy_quality(joined),
        "anatomy": means_win_lose(
            joined,
            [
                "signed_ptb_bps",
                "signed_open_bps",
                "signed_mom15_bps",
                "fair_p_open",
                "left",
                "basis_open_bps",
                "fade_bps",
                "vol_bps_sqrt_s",
            ],
        ),
        "lead_buckets_ptb": lead_buckets(joined, "signed_ptb_bps"),
        "lead_buckets_open": lead_buckets(joined, "signed_open_bps"),
        "lead_buckets_open_holdout": lead_buckets(test, "signed_open_bps"),
        "mom_buckets": lead_buckets(joined, "signed_mom15_bps"),
        "regime": {
            name: summarize([r for r in joined if r["regime"] == name])
            for name in ("snapshot", "twap30", "twap60")
        },
        "rules_all": rules_all,
        "rules_train": rules_train,
        "rules_holdout": rules_hold,
        "wrong_side_open_by_regime": {
            name: eval_skip([r for r in joined if r["regime"] == name], lambda r: r.get("wrong_side_open"))
            for name in ("snapshot", "twap30", "twap60")
        },
        "examples_wrong_side_losses": [
            {
                k: r[k]
                for k in (
                    "slug",
                    "asset",
                    "bought",
                    "winner",
                    "px",
                    "left",
                    "signed_ptb_bps",
                    "fair_p",
                    "lookback",
                    "regime",
                )
                if k in r
            }
            for r in sorted(
                [x for x in joined if (not x["won"]) and x.get("wrong_side_ptb")],
                key=lambda z: z["ts"],
                reverse=True,
            )[:12]
        ],
        "note": (
            "Binance USDT 1s is a proxy for Chainlink BTC/USD TWAP. Live skip must use the "
            "same Chainlink TWAP stream the market settles on, plus the window-open PTB."
        ),
    }
    report["findings"] = build_findings(report)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {OUT}", flush=True)
    print("baseline", report["baseline"], flush=True)
    print("proxy", report["proxy_quality"], flush=True)
    print("top rules", rules_all[:5], flush=True)
    print(report["findings"]["headline_cantonese"], flush=True)


if __name__ == "__main__":
    main()
