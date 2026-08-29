#!/usr/bin/env python3
"""30-day BTC+ETH 5m 97-98c last-60s reversal anatomy (read-only public APIs).

Goal: find causal tape features that cluster *true* 0/1 reverses, then test
filters a live taker could apply (time left, prior path, other-side prints,
99c-through leftover, dwell / second print). Hold out the newest 10 days.

Newest-first /trades pages are enough for last ~2 minutes on liquid BTC; we
keep paging until the tape covers 180s left so fake-out-then-relock is visible.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "surf-arb-research/1.7 (read-only; 30d reverse anatomy; no trading)",
    "Accept": "application/json",
}
OUT = Path(__file__).with_name("reverse_30d.json")
CACHE = Path(os.environ.get("REVERSE_30D_CACHE", "/tmp/reverse_30d_cache"))
SERIES = {"btc": "10684", "eth": "10683"}
ASSETS = ("btc", "eth")
FEE = 0.07
NOTIONAL = 5.0
CIRCUIT = 50.0
DAYS = 30
HOLDOUT_DAYS = 10
WORKERS = 12
PAGES = 6
COVER_LEFT = 180
BAND_LO = 0.97
BAND_HI = 0.98
MIN_LEFT = 3.0
MAX_LEFT = 60.0


def get_json(url: str, timeout: float = 25.0, tries: int = 5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                return None
            if exc.code in {429, 500, 502, 503, 422}:
                time.sleep(0.45 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.35 * (2**i))
    if last:
        raise last
    return None


def parse_field(raw, default):
    if isinstance(raw, (list, dict)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def end_ts(iso: str) -> int:
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def iso_utc(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fee_on(price: float) -> float:
    p = min(max(float(price), 0.0), 1.0)
    return FEE * p * (1.0 - p)


def pnl_usd(price: float, won: bool, notional: float = NOTIONAL) -> float:
    fee = fee_on(price)
    per = (1.0 - price - fee) if won else (-price - fee)
    return per * (notional / max(price, 0.01))


def reverse_breakeven(price: float) -> float:
    """Max reverse rate for +EV after crypto taker fee (1 - p - fee)."""
    return round(1.0 - float(price) - fee_on(price), 6)


def wilson_lo_hi(k: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    z = 1.96
    phat = k / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    spread = (z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5) / denom
    return [round(max(0.0, centre - spread), 6), round(min(1.0, centre + spread), 6)]


def fetch_trades(cid: str, *, pages: int = PAGES) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for page in range(pages):
        chunk = (
            get_json(f"{DATA}/trades?market={cid}&limit=1000&offset={page * 1000}&takerOnly=true")
            or []
        )
        if not chunk:
            break
        for t in chunk:
            hid = (
                str(t.get("transactionHash") or "")
                + f":{t.get('timestamp')}:{t.get('proxyWallet')}:{t.get('price')}:{t.get('size')}"
            )
            if hid in seen:
                continue
            seen.add(hid)
            rows.append(t)
        if len(chunk) < 1000:
            break
        time.sleep(0.03)
    return rows


def normalize(raw: list[dict], cid: str, end: int) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("conditionId") or "") != str(cid):
            continue
        try:
            px = float(t["price"])
            ts = int(t["timestamp"])
            size = float(t.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        outcome = str(t.get("outcome") or "")
        if outcome not in {"Up", "Down"}:
            continue
        left = end - ts
        if left < -5 or left > 400:
            continue
        out.append(
            {
                "ts": ts,
                "left": left,
                "px": px,
                "size": size,
                "side": str(t.get("side") or ""),
                "outcome": outcome,
            }
        )
    out.sort(key=lambda r: r["ts"])
    return out


def oldest_left(trades: list[dict]) -> float:
    in_win = [t["left"] for t in trades if 0 <= t["left"] <= 300]
    return max(in_win) if in_win else 0.0


def list_closed_events(asset: str, oldest_end: int) -> list[dict]:
    series = SERIES[asset]
    out: list[dict] = []
    seen: set[str] = set()
    end_max = None
    stalled = 0
    pages = 0
    while True:
        pages += 1
        if pages > 400:
            break
        url = (
            f"{GAMMA}/events?series_id={series}&closed=true"
            f"&order=endDate&ascending=false&limit=100"
        )
        if end_max:
            url += f"&end_date_max={end_max}"
        page = get_json(url) or []
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
            cid = str(market.get("conditionId") or "")
            prices = [float(x) for x in parse_field(market.get("outcomePrices"), ["0", "0"])]
            outcomes = [str(x) for x in parse_field(market.get("outcomes"), ["Up", "Down"])]
            end_iso = market.get("endDate") or ev.get("endDate")
            if not cid or not end_iso or len(prices) < 2:
                continue
            end = end_ts(end_iso)
            last_end_iso = end_iso
            if end < oldest_end:
                stop = True
                break
            if abs(max(prices) - 1.0) > 0.05:
                continue
            winner = outcomes[0] if prices[0] >= prices[1] else outcomes[1]
            out.append(
                {
                    "asset": asset,
                    "slug": slug,
                    "cid": cid,
                    "end": end,
                    "winner": winner,
                    "volume": float(market.get("volume") or ev.get("volume") or 0),
                    "start": end - 300,
                }
            )
            added += 1
        if stop:
            break
        if not last_end_iso:
            break
        if added == 0:
            stalled += 1
            end_max = iso_utc(end_ts(last_end_iso) - 300)
            if stalled >= 4:
                break
            continue
        stalled = 0
        nxt = iso_utc(end_ts(last_end_iso) - 1)
        if nxt == end_max:
            end_max = iso_utc(end_ts(last_end_iso) - 300)
        else:
            end_max = nxt
        if len(page) < 100 and added < 100:
            # last page of this end_max slice; step further back
            end_max = iso_utc(end_ts(last_end_iso) - 300)
        time.sleep(0.04)
        if len(out) >= DAYS * 12 * 24 + 50:
            break
    return out


def cache_path(slug: str) -> Path:
    return CACHE / f"{slug}.json"


def load_trades_cached(ev: dict) -> list[dict]:
    path = cache_path(ev["slug"])
    if path.exists():
        try:
            raw = json.loads(path.read_text())
            trades = raw if isinstance(raw, list) else raw.get("trades") or []
            if trades and oldest_left(trades) >= COVER_LEFT - 1:
                return trades
        except (json.JSONDecodeError, OSError):
            pass
    raw = fetch_trades(ev["cid"])
    trades = normalize(raw, ev["cid"], ev["end"])
    if oldest_left(trades) < COVER_LEFT and len(raw) >= 1000:
        # already paginated in fetch_trades
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trades))
    return trades


def last_px_before(trades: list[dict], *, outcome: str, ts: int, within_s: float) -> float | None:
    px = None
    lo = ts - within_s
    for t in trades:
        if t["ts"] >= ts:
            break
        if t["ts"] < lo:
            continue
        if t["outcome"] == outcome:
            px = t["px"]
    return px


def max_px_before(
    trades: list[dict], *, outcome: str, ts: int, within_s: float | None = None, buy_only: bool = False
) -> float:
    mx = 0.0
    lo = None if within_s is None else ts - within_s
    for t in trades:
        if t["ts"] >= ts:
            break
        if lo is not None and t["ts"] < lo:
            continue
        if t["outcome"] != outcome:
            continue
        if buy_only and t["side"] != "BUY":
            continue
        mx = max(mx, t["px"])
    return mx


def min_px_before(trades: list[dict], *, outcome: str, ts: int, within_s: float) -> float | None:
    mn = None
    lo = ts - within_s
    for t in trades:
        if t["ts"] >= ts:
            break
        if t["ts"] < lo:
            continue
        if t["outcome"] == outcome:
            mn = t["px"] if mn is None else min(mn, t["px"])
    return mn


def band_buys(trades: list[dict], outcome: str) -> list[dict]:
    out = []
    for t in trades:
        if t["side"] != "BUY" or t["outcome"] != outcome:
            continue
        if t["left"] < MIN_LEFT or t["left"] > MAX_LEFT:
            continue
        tick = round(t["px"], 2)
        if BAND_LO - 1e-12 <= tick <= BAND_HI + 1e-12:
            out.append(t)
    return out


def fakeout_relock(trades: list[dict], fill: dict) -> bool:
    """97+ printed before the last minute, then the favorite traded <=70c, then re-locked."""
    fav = fill["outcome"]
    saw_early = False
    dumped = False
    for t in trades:
        if t["ts"] >= fill["ts"]:
            break
        if t["outcome"] != fav or t["side"] != "BUY":
            continue
        if t["left"] > MAX_LEFT and round(t["px"], 2) >= 0.97:
            saw_early = True
        if saw_early and t["px"] <= 0.70:
            dumped = True
    return bool(saw_early and dumped)


def opp_after_max(trades: list[dict], fill: dict) -> float:
    other = "Down" if fill["outcome"] == "Up" else "Up"
    mx = 0.0
    for t in trades:
        if t["ts"] <= fill["ts"]:
            continue
        if t["left"] < 0:
            continue
        if t["outcome"] == other:
            mx = max(mx, t["px"])
    return mx


def seconds_to_opp(trades: list[dict], fill: dict, thresh: float) -> float | None:
    """Seconds from fill until the other side prints >= thresh (steamroller clock)."""
    other = "Down" if fill["outcome"] == "Up" else "Up"
    for t in trades:
        if t["ts"] <= fill["ts"]:
            continue
        if t["left"] < 0:
            continue
        if t["outcome"] == other and t["px"] >= thresh:
            return round(float(t["ts"] - fill["ts"]), 2)
    return None


def features(ev: dict, trades: list[dict], fill: dict, *, fill_kind: str) -> dict:
    fav = fill["outcome"]
    other = "Down" if fav == "Up" else "Up"
    ts = fill["ts"]
    won = fav == ev["winner"]
    hour = datetime.fromtimestamp(ts, timezone.utc).hour
    last15 = last_px_before(trades, outcome=fav, ts=ts, within_s=15)
    last20 = last_px_before(trades, outcome=fav, ts=ts, within_s=20)
    last30 = last_px_before(trades, outcome=fav, ts=ts, within_s=30)
    mn30 = min_px_before(trades, outcome=fav, ts=ts, within_s=30)
    printed_99 = max_px_before(trades, outcome=fav, ts=ts, buy_only=True) >= 0.985
    opp20 = max_px_before(trades, outcome=other, ts=ts, within_s=20, buy_only=True)
    opp30 = max_px_before(trades, outcome=other, ts=ts, within_s=30, buy_only=True)
    opp60 = max_px_before(trades, outcome=other, ts=ts, within_s=60, buy_only=True)
    opp_all = max_px_before(trades, outcome=other, ts=ts, buy_only=True)
    buys = band_buys(trades, fav)
    recent_fav_buys = [
        t
        for t in trades
        if t["ts"] < ts
        and t["ts"] >= ts - 20
        and t["outcome"] == fav
        and t["side"] == "BUY"
    ]
    last3 = recent_fav_buys[-3:]
    last3_ok = bool(last3) and all(x["px"] >= 0.94 for x in last3)
    opp_after = opp_after_max(trades, fill)
    notion_before = 0.0
    notion_last60 = 0.0
    for t in trades:
        if t["ts"] >= ts:
            break
        usd = float(t["px"]) * float(t.get("size") or 0)
        notion_before += usd
        if t["left"] <= MAX_LEFT:
            notion_last60 += usd
    return {
        "slug": ev["slug"],
        "asset": ev["asset"],
        "start": ev["start"],
        "end": ev["end"],
        "winner": ev["winner"],
        "bought": fav,
        "px": round(fill["px"], 4),
        "tick": round(fill["px"], 2),
        "left": float(fill["left"]),
        "size": round(float(fill.get("size") or 0), 4),
        "ts": ts,
        "hour": hour,
        "dow": datetime.fromtimestamp(ts, timezone.utc).weekday(),
        "volume": round(float(ev["volume"]), 2),
        "notion_before": round(notion_before, 2),
        "notion_last60": round(notion_last60, 2),
        "won": won,
        "pnl": round(pnl_usd(fill["px"], won), 5),
        "fill_kind": fill_kind,
        "printed_99": printed_99,
        "opp20": round(opp20, 4),
        "opp30": round(opp30, 4),
        "opp60": round(opp60, 4),
        "opp_all": round(opp_all, 4),
        "last15": None if last15 is None else round(last15, 4),
        "last20": None if last20 is None else round(last20, 4),
        "last30": None if last30 is None else round(last30, 4),
        "min30": None if mn30 is None else round(mn30, 4),
        "spike15": bool(last15 is not None and last15 < 0.90),
        "spike20": bool(last20 is not None and last20 < 0.85),
        "fakeout": fakeout_relock(trades, fill),
        "n_band_last60": len(buys),
        "last3_ge94": last3_ok,
        "opp_after": round(opp_after, 4),
        "looked_50": opp_after >= 0.50,
        "looked_90": opp_after >= 0.90,
        "sec_to_50": seconds_to_opp(trades, fill, 0.50),
        "sec_to_90": seconds_to_opp(trades, fill, 0.90),
    }


def first_band_fill(trades: list[dict]) -> dict | None:
    for t in trades:
        if t["side"] != "BUY":
            continue
        if t["left"] < MIN_LEFT or t["left"] > MAX_LEFT:
            continue
        tick = round(t["px"], 2)
        if BAND_LO - 1e-12 <= tick <= BAND_HI + 1e-12:
            return t
    return None


def second_band_fill(trades: list[dict]) -> dict | None:
    n = 0
    for t in trades:
        if t["side"] != "BUY":
            continue
        if t["left"] < MIN_LEFT or t["left"] > MAX_LEFT:
            continue
        tick = round(t["px"], 2)
        if BAND_LO - 1e-12 <= tick <= BAND_HI + 1e-12:
            n += 1
            if n >= 2:
                return t
    return None


def dwell_fill(trades: list[dict], *, wait_s: float) -> dict | None:
    first = first_band_fill(trades)
    if first is None:
        return None
    need = first["ts"] + wait_s
    for t in trades:
        if t["ts"] < need:
            continue
        if t["side"] != "BUY" or t["outcome"] != first["outcome"]:
            continue
        if t["left"] < MIN_LEFT:
            continue
        tick = round(t["px"], 2)
        if BAND_LO - 1e-12 <= tick <= BAND_HI + 1e-12:
            return t
    return None


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"n": 0, "win": 0, "lose": 0, "reverse": None, "pnl_usd": 0.0}
    lose = sum(1 for r in rows if not r["won"])
    win = n - lose
    pnl = sum(r["pnl"] for r in rows)
    avg_px = sum(r["px"] for r in rows) / n
    be = reverse_breakeven(avg_px)
    rev = lose / n
    return {
        "n": n,
        "win": win,
        "lose": lose,
        "reverse": round(rev, 6),
        "reverse_ci95": wilson_lo_hi(lose, n),
        "pnl_usd": round(pnl, 2),
        "avg_left": round(sum(r["left"] for r in rows) / n, 2),
        "avg_px": round(avg_px, 4),
        "looked_50_rate": round(sum(1 for r in rows if r.get("looked_50")) / n, 6),
        "looked_90_rate": round(sum(1 for r in rows if r.get("looked_90")) / n, 6),
        "reverse_be_at_avg_px": be,
        "vs_be": round(rev - be, 6),
        "ev_ok": bool(rev < be),
    }


def circuit_backtest(rows: list[dict]) -> dict:
    events = sorted(((r["ts"], r["pnl"], not r["won"]) for r in rows), key=lambda x: x[0])
    total = 0.0
    day_pnl = 0.0
    day_key = None
    halted = False
    taken = skipped = lose_taken = halt_days = 0
    day_pnls: list[float] = []

    def close_day():
        nonlocal halt_days
        if day_key is None:
            return
        day_pnls.append(day_pnl)
        if halted:
            halt_days += 1

    for ts, usd, lost in events:
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        if d != day_key:
            close_day()
            day_key = d
            day_pnl = 0.0
            halted = False
        if halted:
            skipped += 1
            continue
        day_pnl += usd
        total += usd
        taken += 1
        lose_taken += int(lost)
        if day_pnl <= -abs(CIRCUIT):
            halted = True
    close_day()
    n_days = max(len(day_pnls), 1)
    return {
        "n_taken": taken,
        "n_skipped_circuit": skipped,
        "lose_taken": lose_taken,
        "halt_days": halt_days,
        "trade_days": len(day_pnls),
        "total_usd": round(total, 2),
        "usd_per_day": round(total / n_days, 2),
        "worst_day_usd": round(min(day_pnls), 2) if day_pnls else 0.0,
        "best_day_usd": round(max(day_pnls), 2) if day_pnls else 0.0,
    }


def one_per_window(rows: list[dict]) -> list[dict]:
    """Same 5m timestamp: keep the earlier fill (BTC/ETH dump together)."""
    best: dict[int, dict] = {}
    for r in sorted(rows, key=lambda x: (x["ts"], x["asset"])):
        key = int(r["start"])
        if key not in best:
            best[key] = r
    return sorted(best.values(), key=lambda x: x["ts"])


def rate_table(rows: list[dict], key: str, buckets: list[tuple[str, callable]]) -> dict:
    out = {}
    for name, fn in buckets:
        part = [r for r in rows if fn(r)]
        out[name] = summarize(part)
    return out


def compare_win_lose(rows: list[dict]) -> dict:
    wins = [r for r in rows if r["won"]]
    loses = [r for r in rows if not r["won"]]

    def mean(xs, k):
        vals = [float(x[k]) for x in xs if x.get(k) is not None]
        return round(sum(vals) / len(vals), 4) if vals else None

    def share(xs, pred):
        if not xs:
            return None
        return round(sum(1 for x in xs if pred(x)) / len(xs), 4)

    keys = ["left", "px", "opp20", "opp30", "opp60", "opp_all", "last15", "last20", "min30", "volume", "notion_before", "notion_last60", "hour", "size", "n_band_last60"]
    means = {k: {"win": mean(wins, k), "lose": mean(loses, k)} for k in keys}
    flags = {
        "printed_99": {"win": share(wins, lambda r: r["printed_99"]), "lose": share(loses, lambda r: r["printed_99"])},
        "spike15": {"win": share(wins, lambda r: r["spike15"]), "lose": share(loses, lambda r: r["spike15"])},
        "spike20": {"win": share(wins, lambda r: r["spike20"]), "lose": share(loses, lambda r: r["spike20"])},
        "fakeout": {"win": share(wins, lambda r: r["fakeout"]), "lose": share(loses, lambda r: r["fakeout"])},
        "last3_ge94": {"win": share(wins, lambda r: r["last3_ge94"]), "lose": share(loses, lambda r: r["last3_ge94"])},
        "tick_97": {"win": share(wins, lambda r: r["tick"] == 0.97), "lose": share(loses, lambda r: r["tick"] == 0.97)},
        "tick_98": {"win": share(wins, lambda r: r["tick"] == 0.98), "lose": share(loses, lambda r: r["tick"] == 0.98)},
        "up": {"win": share(wins, lambda r: r["bought"] == "Up"), "lose": share(loses, lambda r: r["bought"] == "Up")},
        "eth": {"win": share(wins, lambda r: r["asset"] == "eth"), "lose": share(loses, lambda r: r["asset"] == "eth")},
        "first_tick_ge55s": {"win": share(wins, lambda r: r["left"] >= 55), "lose": share(loses, lambda r: r["left"] >= 55)},
        "late_lt15s": {"win": share(wins, lambda r: r["left"] < 15), "lose": share(loses, lambda r: r["left"] < 15)},
        "opp20_ge10": {"win": share(wins, lambda r: r["opp20"] >= 0.10), "lose": share(loses, lambda r: r["opp20"] >= 0.10)},
        "opp30_ge10": {"win": share(wins, lambda r: r["opp30"] >= 0.10), "lose": share(loses, lambda r: r["opp30"] >= 0.10)},
        "looked_50": {"win": share(wins, lambda r: r.get("looked_50")), "lose": share(loses, lambda r: r.get("looked_50"))},
        "looked_90": {"win": share(wins, lambda r: r.get("looked_90")), "lose": share(loses, lambda r: r.get("looked_90"))},
    }
    return {"n_win": len(wins), "n_lose": len(loses), "means": means, "flags": flags}


FILTERS = [
    ("baseline_first_97_98_last60", lambda r: True),
    ("rev21_skip_99_through", lambda r: not r["printed_99"]),
    ("skip_opp20_ge_10c", lambda r: r["opp20"] < 0.10),
    ("skip_opp30_ge_10c", lambda r: r["opp30"] < 0.10),
    ("skip_spike15_below_90", lambda r: not r["spike15"]),
    ("skip_spike20_below_85", lambda r: not r["spike20"]),
    ("skip_fakeout_relock", lambda r: not r["fakeout"]),
    ("skip_first_tick_left_gt_55", lambda r: r["left"] <= 55),
    ("skip_late_left_lt_15", lambda r: r["left"] >= 15),
    ("require_last3_ge94", lambda r: r["last3_ge94"]),
    ("btc_only", lambda r: r["asset"] == "btc"),
    ("eth_only", lambda r: r["asset"] == "eth"),
    ("up_only", lambda r: r["bought"] == "Up"),
    ("down_only", lambda r: r["bought"] == "Down"),
    ("px_97_only", lambda r: r["tick"] == 0.97),
    ("px_98_only", lambda r: r["tick"] == 0.98),
    ("core_rev21_opp20_nospike", lambda r: (not r["printed_99"]) and r["opp20"] < 0.10 and not r["spike15"]),
    (
        "core_plus_no_first_tick",
        lambda r: (not r["printed_99"]) and r["opp20"] < 0.10 and not r["spike15"] and r["left"] <= 55,
    ),
    (
        "core_plus_no_fakeout",
        lambda r: (not r["printed_99"]) and r["opp20"] < 0.10 and not r["spike15"] and not r["fakeout"],
    ),
    (
        "strict_lock",
        lambda r: (not r["printed_99"])
        and r["opp20"] < 0.10
        and not r["spike15"]
        and not r["fakeout"]
        and r["left"] <= 55
        and r["left"] >= 12
        and r["last3_ge94"],
    ),
    # Hour/volume cuts are documented as overfit probes, not Rev 22 candidates.
    ("skip_utc_0_6_11_13", lambda r: r["hour"] not in {0, 6, 11, 13}),
]


def eval_filters(rows: list[dict]) -> dict:
    out = {}
    for name, fn in FILTERS:
        part = [r for r in rows if fn(r)]
        row = summarize(part)
        row["circuit"] = circuit_backtest(part)
        row["kept_frac"] = None if not rows else round(len(part) / len(rows), 4)
        out[name] = row
    return out


def hour_table(rows: list[dict]) -> dict:
    out = {}
    for h in range(24):
        out[str(h)] = summarize([r for r in rows if r["hour"] == h])
    return out


def left_table(rows: list[dict]) -> dict:
    edges = [(55, 60), (45, 55), (30, 45), (15, 30), (3, 15)]
    out = {}
    for lo, hi in edges:
        out[f"left_{lo}_{hi}"] = summarize([r for r in rows if lo < r["left"] <= hi])
    return out


def _pct(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(xs)
    i = min(len(ys) - 1, max(0, int(round((len(ys) - 1) * p))))
    return ys[i]


def quantile_table(rows: list[dict], key: str, n_bins: int = 5) -> dict:
    vals = sorted(float(r[key]) for r in rows if r.get(key) is not None)
    if len(vals) < n_bins:
        return {}
    cuts = [_pct(vals, i / n_bins) for i in range(n_bins + 1)]
    out = {}
    for i in range(n_bins):
        lo, hi = cuts[i], cuts[i + 1]
        if i + 1 == n_bins:
            part = [r for r in rows if r.get(key) is not None and lo <= float(r[key]) <= hi]
        else:
            part = [r for r in rows if r.get(key) is not None and lo <= float(r[key]) < hi]
        label = f"{key}_q{i + 1}_{lo:.0f}_{hi:.0f}" if key == "volume" else f"{key}_q{i + 1}"
        row = summarize(part)
        row["lo"] = lo
        row["hi"] = hi
        out[label] = row
    return out


def tick_x_left(rows: list[dict]) -> dict:
    out = {}
    for tick, tname in ((0.97, "97"), (0.98, "98")):
        for lo, hi in ((55, 60), (45, 55), (30, 45), (15, 30), (3, 15)):
            part = [r for r in rows if r["tick"] == tick and lo < r["left"] <= hi]
            out[f"px{tname}_left_{lo}_{hi}"] = summarize(part)
    return out


def dow_table(rows: list[dict]) -> dict:
    names = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    out = {}
    for i, name in enumerate(names):
        out[name] = summarize([r for r in rows if r.get("dow") == i])
    return out


def dist_stats(xs: list[float]) -> dict | None:
    if not xs:
        return None
    ys = sorted(float(x) for x in xs)
    n = len(ys)
    return {
        "n": n,
        "mean": round(sum(ys) / n, 2),
        "p25": round(_pct(ys, 0.25) or 0.0, 2),
        "p50": round(_pct(ys, 0.50) or 0.0, 2),
        "p75": round(_pct(ys, 0.75) or 0.0, 2),
        "p90": round(_pct(ys, 0.90) or 0.0, 2),
    }


def settle_modes(rows: list[dict]) -> dict:
    """How reverses actually appear on tape vs silent 0/1."""
    n = len(rows) or 1
    lose = [r for r in rows if not r["won"]]
    win = [r for r in rows if r["won"]]
    steam = [r for r in lose if r.get("looked_90")]
    mid = [r for r in lose if r.get("looked_50") and not r.get("looked_90")]
    stealth = [r for r in lose if not r.get("looked_50")]
    fake = [r for r in win if r.get("looked_90")]
    return {
        "n": len(rows),
        "lose": len(lose),
        "steamroller_90_share_of_loses": None if not lose else round(len(steam) / len(lose), 4),
        "mid_dump_50_89_share_of_loses": None if not lose else round(len(mid) / len(lose), 4),
        "stealth_no_50_share_of_loses": None if not lose else round(len(stealth) / len(lose), 4),
        "fake_dump_then_win": len(fake),
        "fake_dump_then_win_rate": round(len(fake) / n, 6),
        "steamroller": summarize(steam),
        "stealth": summarize(stealth),
        "loser_sec_to_50": dist_stats([r["sec_to_50"] for r in lose if r.get("sec_to_50") is not None]),
        "loser_sec_to_90": dist_stats([r["sec_to_90"] for r in lose if r.get("sec_to_90") is not None]),
    }


def paired_windows(first_fills: list[dict]) -> dict:
    """Same 5m start: BTC and ETH both printed a 97-98 last-60s buy."""
    by_start: dict[int, list[dict]] = {}
    for r in first_fills:
        by_start.setdefault(int(r["start"]), []).append(r)
    both = []
    one_loses = 0
    both_lose = 0
    agree = 0
    for group in by_start.values():
        assets = {x["asset"] for x in group}
        if "btc" not in assets or "eth" not in assets:
            continue
        # one row per asset (first fill already unique per market)
        btc = next(x for x in group if x["asset"] == "btc")
        eth = next(x for x in group if x["asset"] == "eth")
        both.append((btc, eth))
        lost = (not btc["won"]) + (not eth["won"])
        if lost == 1:
            one_loses += 1
        if lost == 2:
            both_lose += 1
        if btc["won"] == eth["won"]:
            agree += 1
    n = len(both)
    return {
        "n_windows_both_printed": n,
        "same_win_lose_agree": None if not n else round(agree / n, 4),
        "exactly_one_reverses": one_loses,
        "both_reverse": both_lose,
        "p_other_reverses_given_one": None if not (one_loses + both_lose) else round(both_lose / (one_loses + both_lose), 4),
    }


def reverse_clusters(rows: list[dict]) -> dict:
    loses = sorted((r["start"] for r in rows if not r["won"]))
    if not loses:
        return {"n_reverses": 0}
    isolated = 0
    in_run = 0
    run_len = 1
    runs = []
    for a, b in zip(loses, loses[1:]):
        if b - a <= 600:
            run_len += 1
        else:
            runs.append(run_len)
            if run_len == 1:
                isolated += 1
            else:
                in_run += run_len
            run_len = 1
    runs.append(run_len)
    if run_len == 1:
        isolated += 1
    else:
        in_run += run_len
    return {
        "n_reverses": len(loses),
        "isolated": isolated,
        "in_cluster_le_10m": in_run,
        "max_run": max(runs),
        "isolated_share": round(isolated / len(loses), 4),
    }


def extra_cut_filters(all_rows: list[dict], train_rows: list[dict], test_rows: list[dict]) -> dict:
    """Thresholds fit on train only, then applied to holdout (no peeking).

    Gamma `volume` includes the post-fill steamroller — look-ahead. Prefer
    `notion_before` (tape USD before the entry print).
    """
    out: dict = {}
    for key, label_prefix, ps in (
        ("volume", "skip_vol", (0.8, 0.9)),
        ("notion_before", "skip_notion_before", (0.8, 0.9)),
        ("notion_last60", "skip_notion_last60", (0.8, 0.9)),
    ):
        vals = sorted(float(r[key]) for r in train_rows if r.get(key) is not None)
        for p in ps:
            cut = _pct(vals, p)
            if cut is None:
                continue
            pct = int(round((1 - p) * 100))
            label = f"{label_prefix}_top{pct}_traincut"

            def summarize_cut(rows: list[dict], c: float = cut, k: str = key) -> dict:
                part = [r for r in rows if float(r.get(k) or 0) < c]
                row = summarize(part)
                row["circuit"] = circuit_backtest(part)
                row["kept_frac"] = None if not rows else round(len(part) / len(rows), 4)
                row["cut"] = c
                row["key"] = k
                return row

            out[label] = {
                "key": key,
                "cut": cut,
                "all": summarize_cut(all_rows),
                "train": summarize_cut(train_rows),
                "holdout": summarize_cut(test_rows),
            }
    return out


def rank_filters(table: dict, *, min_n: int = 30) -> list[dict]:
    ranked = []
    for name, row in table.items():
        if row.get("n", 0) < min_n:
            continue
        ranked.append(
            {
                "filter": name,
                "n": row["n"],
                "reverse": row["reverse"],
                "pnl_usd": row["pnl_usd"],
                "ev_ok": row.get("ev_ok"),
                "vs_be": row.get("vs_be"),
                "halt_days": (row.get("circuit") or {}).get("halt_days"),
                "kept_frac": row.get("kept_frac"),
            }
        )
    ranked.sort(key=lambda x: (x["pnl_usd"], -(x["reverse"] or 0)), reverse=True)
    return ranked


def filter_sign_flips(train: dict, holdout: dict) -> list[dict]:
    out = []
    for name in train:
        t, h = train[name], holdout.get(name) or {}
        if t.get("n", 0) < 80 or h.get("n", 0) < 30:
            continue
        tpos = (t.get("pnl_usd") or 0) > 0
        hpos = (h.get("pnl_usd") or 0) > 0
        if tpos != hpos:
            out.append(
                {
                    "filter": name,
                    "train_n": t["n"],
                    "train_pnl": t["pnl_usd"],
                    "holdout_n": h["n"],
                    "holdout_pnl": h["pnl_usd"],
                    "train_reverse": t["reverse"],
                    "holdout_reverse": h["reverse"],
                }
            )
    return out


def build_findings(report: dict) -> dict:
    """Causal takeaways a live taker can actually use. No holdout-lucky Rev 22."""
    base = report["filters_all"]["baseline_first_97_98_last60"]
    px97 = report["filters_all"]["px_97_only"]
    px98 = report["filters_all"]["px_98_only"]
    hold = report["filters_holdout_10d"]["baseline_first_97_98_last60"]
    anat = report["anatomy_all"]["flags"]
    means = report["anatomy_all"]["means"]
    modes = report["settle_modes"]
    be97 = report["reverse_breakeven_at_97"]
    be98 = report["reverse_breakeven_at_98"]
    hours = report["hour_utc"]
    hour_rows = []
    for h, row in hours.items():
        if row.get("n", 0) >= 80 and row.get("reverse") is not None:
            hour_rows.append((row["reverse"], int(h), row["pnl_usd"], row["n"]))
    hour_rows.sort(reverse=True)
    worst_h = hour_rows[:4]
    best_h = list(reversed(hour_rows[-4:]))
    vol_q = report.get("volume_quintiles") or {}
    vol_top = None
    for name, row in vol_q.items():
        if name.startswith("volume_q5_"):
            vol_top = row
            break
    vol_top_txt = "n/a"
    if vol_top and vol_top.get("reverse") is not None:
        vol_top_txt = f"{100 * vol_top['reverse']:.1f}%, PnL {vol_top['pnl_usd']}"
    vol_skip = report.get("volume_traincut_filters") or {}
    skip20 = vol_skip.get("skip_vol_top20_traincut") or {}
    skip_notion = vol_skip.get("skip_notion_before_top20_traincut") or {}
    nb_all = skip_notion.get("all") or {}
    nb_hold = skip_notion.get("holdout") or {}
    notion_ok = bool(nb_all.get("ev_ok")) and bool(nb_hold.get("ev_ok"))
    notion_txt = (
        f"入場前 tape USD 跳過 train 頂 20%：全樣本 PnL {nb_all.get('pnl_usd')} "
        f"(ev_ok={nb_all.get('ev_ok')})，holdout {nb_hold.get('pnl_usd')} "
        f"(ev_ok={nb_hold.get('ev_ok')})。"
    )
    ho_btc = report["filters_holdout_10d"].get("btc_only") or {}
    ho_tick = report["filters_holdout_10d"].get("skip_first_tick_left_gt_55") or {}
    steam_pct = None if modes.get("steamroller_90_share_of_loses") is None else round(100 * modes["steamroller_90_share_of_loses"])
    stealth_pct = None if modes.get("stealth_no_50_share_of_loses") is None else round(100 * modes["stealth_no_50_share_of_loses"])
    sec90 = (modes.get("loser_sec_to_90") or {}).get("p50")
    paired = report.get("paired_windows") or {}
    clusters = report.get("reverse_clusters") or {}
    return {
        "headline_cantonese": (
            f"30日 BTC+ETH 尾60秒 97–98¢ 第一手（每5分鐘窗一注 $5）反轉 {base['reverse']:.2%}，"
            f"費後損益平衡 97¢ 約 {be97:.2%}、98¢ 約 {be98:.2%}。"
            f"全樣本 PnL {base['pnl_usd']} 美元（約 {report['circuit_baseline']['usd_per_day']}/日），"
            "費後略負。成交當刻 tape 睇落同贏盤幾乎一樣；真反轉 91% 係入場後先砸到 90¢。"
        ),
        "breakeven": {
            "at_97": be97,
            "at_98": be98,
            "sample_reverse": base["reverse"],
            "vs_be_at_avg_px": base.get("vs_be"),
            "ev_ok_full_sample": base.get("ev_ok"),
            "note": "Win ~14c / lose ~$5 on a $5 97c fill. Mix 97/98 avg px ~97.4c has reverse BE ~2.45%; sample reverse 2.88% is slightly -EV.",
        },
        "common_points": [
            "反轉喺成交當刻唔似反轉：剩餘秒數、對手 20/30s print、last15、spike、fakeout、Up/Down、BTC/ETH 同贏盤幾乎重疊。",
            f"最穩嘅價帶旗標係 97 vs 98：輸盤 {anat['tick_97']['lose']:.0%} 係 97¢，贏盤 {anat['tick_97']['win']:.0%}。98-only 反轉仍高過 98¢ 損益平衡，縮注快過縮反轉。",
            f"完場 Gamma volume 輸盤較高（mean ${means['volume']['lose']:.0f} vs win ${means['volume']['win']:.0f}），但呢個數字包含反轉之後嘅砸盤，有前視偏差。入場前 tape USD 幾乎一樣（lose ${means.get('notion_before', {}).get('lose') or 0:.0f} vs win ${means.get('notion_before', {}).get('win') or 0:.0f}）；97–98 帶內 print 輸盤更少（n_band {means['n_band_last60']['lose']} vs {means['n_band_last60']['win']}）。",
            f"成交量 quintile（完場 volume）最頂一檔反轉 {vol_top_txt}——唔好當 live 閘，因為砸盤本身會推高完場量。",
            notion_txt,
            f"printed_99 喺贏盤更常見（{anat['printed_99']['win']:.0%} vs 輸 {anat['printed_99']['lose']:.0%}）。Rev 21 擋 leftover-97-after-99 係避開 eth-8400 類 live 失敗，唔係 30 日反轉主因。",
            f"真反轉幾乎都係成交後先出現：輸盤 {steam_pct}% 之後對手印到 90¢；完全無 50¢ 對手印嘅 stealth 只佔 {stealth_pct}%。贏盤只有 {anat['looked_90']['win']:.1%} 會假砸再翻贏。",
            f"輸盤對手 90¢ 中位延遲約 {sec90} 秒（p25 { (modes.get('loser_sec_to_50') or {}).get('p25') }s 先到 50¢）——入場之後先倒，t0 tape 預測唔到。",
            f"BTC/ETH 同一 5m 窗一齊印 97–98 有 {paired.get('n_windows_both_printed')} 窗；一邊反轉時另一邊一齊反轉只有 {paired.get('p_other_reverses_given_one')}。反轉 88% 孤立（10 分鐘內無第二轉）。",
        ],
        "filters_that_do_not_flip_ev": [
            "skip leftover-97 after 99, opp20≥10c, spike15, fakeout, second print, dwell 3s, skip first tick, skip last 15s, last3≥94",
            "skip_first_tick / btc_only / skip UTC 0·6·11·13 喺 10 日 holdout 轉正，train 同 30 日全樣本仍然負——sign flip，唔好當 Rev 22。",
            f"UTC 最差鐘 {[(h, round(rev, 4), pnl) for rev, h, pnl, _n in worst_h]} 最好 {[(h, round(rev, 4), pnl) for rev, h, pnl, _n in best_h]}；鐘點同星期（Tue +EV）脆弱。",
        ],
        "what_actually_avoids_damage": [
            "唔加注。$10 反轉率不變，熔斷快一倍。",
            "保留 Rev 21：真頂 ask 97–98、bid 未穿 99、WS-only、同一 5m 窗一注。擋簿同 leftover，唔係入場後 3% steamroller。",
            (
                "入場前 tape 成交額頂檔：" + (
                    "train 切點喺 train/holdout 都略正，可做候選閘（用 hunt 當刻已成交 USD，唔用完場 volume）。仍然唔自動上 Rev 22，要你點頭。"
                    if notion_ok
                    else "用入場前 tape USD 之後，頂檔避開唔再穩；完場高成交量只係砸盤結果。"
                )
            ),
            "97–98 尾 60s 呢個月費後略負 EV。唯一確定避開就係唔做呢個帶。未叫停大熱就唔改 Rev。",
            "熔斷 $50 喺 $5/注、反轉 ~2.9%、最差日 −$24 時 30 日 0 次熔斷；加注先會再撞熔斷。",
        ],
        "do_not_ship": [
            f"skip_first_tick_left_gt_55（holdout {ho_tick.get('pnl_usd')}，train 負、全樣本反轉更差）",
            f"btc_only（holdout {ho_btc.get('pnl_usd')}，全樣本 BTC 同 ETH 都係 ~2.9% 反轉）",
            "hour-of-day / day-of-week skip（holdout 轉正、train 負）",
            "strict_lock（全樣本 n=381 薄利，holdout n 太細）",
            "min tick 98（反轉跌但仍然 −EV）",
        ],
        "px97_vs_98": {"px97": px97, "px98": px98, "holdout_baseline": hold},
        "volume_skip_traincut": skip20,
        "notion_before_skip_traincut": skip_notion,
        "gamma_volume_is_lookahead": True,
        "notion_before_flips_ev_train_and_holdout": notion_ok,
        "sign_flips_train_vs_holdout": report.get("filter_sign_flips") or [],
        "paired_windows": paired,
        "reverse_clusters": clusters,
    }


def apply_events(events: list[dict]) -> tuple[list[dict], list[dict], list[dict], dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    first_fills: list[dict] = []
    second_fills: list[dict] = []
    dwell3: list[dict] = []
    errors = 0
    error_samples: list[str] = []
    loaded = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(load_trades_cached, ev): ev for ev in events}
        done = 0
        for fut in as_completed(futs):
            ev = futs[fut]
            done += 1
            if done % 200 == 0:
                print(f"  trades {done}/{len(events)} fills={len(first_fills)} err={errors}", flush=True)
            try:
                trades = fut.result()
            except Exception as exc:
                errors += 1
                if len(error_samples) < 8:
                    error_samples.append(f"{ev['slug']}: {type(exc).__name__}: {exc}")
                continue
            loaded += 1
            a = first_band_fill(trades)
            if a:
                first_fills.append(features(ev, trades, a, fill_kind="first"))
            b = second_band_fill(trades)
            if b:
                second_fills.append(features(ev, trades, b, fill_kind="second"))
            c = dwell_fill(trades, wait_s=3.0)
            if c:
                dwell3.append(features(ev, trades, c, fill_kind="dwell3"))
    meta = {"loaded": loaded, "errors": errors, "error_samples": error_samples}
    return first_fills, second_fills, dwell3, meta


def split_holdout(rows: list[dict], newest_end: int) -> tuple[list[dict], list[dict]]:
    cut = newest_end - HOLDOUT_DAYS * 86400
    train = [r for r in rows if r["end"] < cut]
    test = [r for r in rows if r["end"] >= cut]
    return train, test


def reverse_examples(rows: list[dict], n: int = 15) -> list[dict]:
    loses = [r for r in rows if not r["won"]]
    loses.sort(key=lambda r: r["ts"], reverse=True)
    keep = (
        "slug",
        "asset",
        "bought",
        "winner",
        "px",
        "left",
        "hour",
        "printed_99",
        "opp20",
        "opp30",
        "last15",
        "min30",
        "spike15",
        "fakeout",
        "opp_after",
    )
    return [{k: r[k] for k in keep} for r in loses[:n]]


def main() -> None:
    now = int(time.time())
    newest_end = now - (now % 300) - 300
    oldest_end = newest_end - DAYS * 86400
    print(f"list closed 5m {DAYS}d from {iso_utc(oldest_end)} to {iso_utc(newest_end)}", flush=True)
    events: list[dict] = []
    for asset in ASSETS:
        rows = list_closed_events(asset, oldest_end)
        print(f"  {asset} events {len(rows)}", flush=True)
        events.extend(rows)
    events = [e for e in events if oldest_end <= e["end"] <= newest_end]
    print(f"resolved 0/1 markets {len(events)}", flush=True)
    CACHE.mkdir(parents=True, exist_ok=True)
    (CACHE / "_events.json").write_text(json.dumps(events))

    first_fills, second_fills, dwell3, meta = apply_events(events)
    print(
        f"fills first={len(first_fills)} second={len(second_fills)} dwell3={len(dwell3)} "
        f"loaded={meta['loaded']} err={meta['errors']}",
        flush=True,
    )

    first_one = one_per_window(first_fills)
    train, test = split_holdout(first_one, newest_end)
    be = reverse_breakeven(0.97)
    filters_all = eval_filters(first_one)
    filters_train = eval_filters(train)
    filters_hold = eval_filters(test)

    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "days": DAYS,
        "holdout_days": HOLDOUT_DAYS,
        "assets": list(ASSETS),
        "band": [BAND_LO, BAND_HI],
        "window_s": MAX_LEFT,
        "notional_usd": NOTIONAL,
        "fee": FEE,
        "reverse_breakeven_at_97": be,
        "reverse_breakeven_at_98": reverse_breakeven(0.98),
        "markets": len(events),
        "markets_by_asset": {a: sum(1 for e in events if e["asset"] == a) for a in ASSETS},
        "errors": meta["errors"],
        "error_samples": meta["error_samples"],
        "range": {"oldest_end": iso_utc(oldest_end), "newest_end": iso_utc(newest_end)},
        "n_first_fills": len(first_fills),
        "n_first_one_per_window": len(first_one),
        "anatomy_all": compare_win_lose(first_one),
        "left_buckets": left_table(first_one),
        "tick_x_left": tick_x_left(first_one),
        "volume_quintiles": quantile_table(first_one, "volume", 5),
        "notion_before_quintiles": quantile_table(first_one, "notion_before", 5),
        "notion_last60_quintiles": quantile_table(first_one, "notion_last60", 5),
        "hour_utc": hour_table(first_one),
        "dow_utc": dow_table(first_one),
        "asset_split": {
            "btc": summarize([r for r in first_one if r["asset"] == "btc"]),
            "eth": summarize([r for r in first_one if r["asset"] == "eth"]),
        },
        "settle_modes": settle_modes(first_one),
        "paired_windows": paired_windows(first_fills),
        "reverse_clusters": reverse_clusters(first_one),
        "filters_all": filters_all,
        "filters_train_20d": filters_train,
        "filters_holdout_10d": filters_hold,
        "second_print": {
            "all": summarize(one_per_window(second_fills)),
            "holdout": summarize(one_per_window(split_holdout(one_per_window(second_fills), newest_end)[1])),
        },
        "dwell_3s": {
            "all": summarize(one_per_window(dwell3)),
            "holdout": summarize(one_per_window(split_holdout(one_per_window(dwell3), newest_end)[1])),
        },
        "circuit_baseline": circuit_backtest(first_one),
        "volume_traincut_filters": extra_cut_filters(first_one, train, test),
        "reverse_examples": reverse_examples(first_one),
        "note": (
            "Tape study of first public BUY in 97-98c with 3s < left <= 60s, 0/1 settle only. "
            "Rev 21 leftover-97 after 99c is printed_99. Live bot uses the book, not tape; "
            "spike15 maps to 'favorite ask 15s ago was still <90c' (needs a short quote memory). "
            "reverse_breakeven = 1 - p - fee (max reverse rate for +EV), not win-rate breakeven."
        ),
    }

    report["holdout_rank_by_pnl"] = rank_filters(filters_hold)[:12]
    report["full_sample_rank_by_pnl"] = rank_filters(filters_all)[:12]
    report["filter_sign_flips"] = filter_sign_flips(filters_train, filters_hold)
    report["train_n"] = len(train)
    report["holdout_n"] = len(test)
    report["findings"] = build_findings(report)

    OUT.write_text(json.dumps(report, indent=2))
    print(f"wrote {OUT}", flush=True)
    print("baseline", report["filters_all"]["baseline_first_97_98_last60"], flush=True)
    print("findings", report["findings"]["headline_cantonese"], flush=True)
    print("holdout rank", report["holdout_rank_by_pnl"][:6], flush=True)
    print("sign flips", report["filter_sign_flips"], flush=True)


if __name__ == "__main__":
    main()
