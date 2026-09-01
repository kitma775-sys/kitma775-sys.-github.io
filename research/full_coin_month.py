#!/usr/bin/env python3
"""1-month all-coin TWAP backtest: ingest + clock-lock portfolio grid.

Live settlement is Chainlink 60s TWAP vs Chainlink T0. This proxy uses Binance
1s TWAP vs Binance T0 plus CLOB mid-band prints. Do not mix pre-TWAP-60 windows.

Live-like portfolio: one coin per 5m unix (highest |lead| among ready), scratch
as shipped Rev 48, $5 notional then scaled to $3. Hold out newest 7 days.
Never ship a rule that reopens the live alt 120–180 / reverse / wild-lead bleed
even if the Binance proxy likes it.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import (  # noqa: E402
    TwapParams,
    entry_edge,
    fair_p_up,
    lead_bps,
    lead_z,
    should_scratch,
)
import reverse_30d as r30  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402

OUT = Path(__file__).with_name("full_coin_month.json")
CACHE = Path(os.environ.get("TWAP_MONTH_CACHE", "/tmp/twap_month_cache"))
OLD_CACHE = Path(os.environ.get("REVERSE_30D_CACHE", "/tmp/reverse_30d_cache"))
TWAP60 = te.TWAP60_START
HOLDOUT_DAYS = 7
NOTIONAL = te.NOTIONAL  # $5 research; live stake is $3
LIVE_STAKE = 3.0
CORE = ("btc", "eth")
ASSETS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype")
SERIES = {
    "btc": "10684",
    "eth": "10683",
    "sol": "10686",
    "xrp": "10685",
    "doge": "11325",
    "bnb": "11326",
    "hype": "11327",
}
SYMBOL = {
    "btc": "BTCUSDT",
    "eth": "ETHUSDT",
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
}
UA = {
    "User-Agent": "surf-arb-research/full-coin-month (read-only; no trading)",
    "Accept": "application/json",
}
DATA = "https://data-api.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
PX_LO, PX_HI = 0.20, 0.62
MAX_LEAD = 40.0
WORKERS = 16
SKIP_PRINT_ASSETS = {"hype"}  # no Binance 1s → cannot oracle-sim
SIM_CACHE = CACHE / "_takes.json"
DO_NOT_SHIP_NAMES = {
    "clock_all_min120",  # live alt 120–180 held 0/3 (−$8.33)
    "clock_all_min180",  # raises BTC/ETH min_left; holdout historically worse
    "rev48_no_clock",
    "indep_last_min120",
    "indep_first_min120",
    "clock_core_only",  # do not cut Telegram coins
    "clock_alts_only",
}


def http_json(url: str, timeout: float = 25.0, tries: int = 5):
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
                time.sleep(0.4 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.3 * (2**i))
    if last:
        raise last
    return None


def list_events(asset: str, oldest_end: int) -> list[dict]:
    series = SERIES[asset]
    out: list[dict] = []
    seen: set[str] = set()
    end_max = None
    stalled = 0
    pages = 0
    while pages < 500:
        pages += 1
        url = f"{GAMMA}/events?series_id={series}&closed=true&order=endDate&ascending=false&limit=100"
        if end_max:
            url += f"&end_date_max={end_max}"
        page = http_json(url) or []
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
            prices = [float(x) for x in r30.parse_field(market.get("outcomePrices"), ["0", "0"])]
            outcomes = [str(x) for x in r30.parse_field(market.get("outcomes"), ["Up", "Down"])]
            end_iso = market.get("endDate") or ev.get("endDate")
            if not cid or not end_iso or len(prices) < 2:
                continue
            end = r30.end_ts(end_iso)
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
            end_max = r30.iso_utc(r30.end_ts(last_end_iso) - 300)
            if stalled >= 4:
                break
            continue
        stalled = 0
        nxt = r30.iso_utc(r30.end_ts(last_end_iso) - 1)
        end_max = nxt if nxt != end_max else r30.iso_utc(r30.end_ts(last_end_iso) - 300)
        if len(page) < 100:
            end_max = r30.iso_utc(r30.end_ts(last_end_iso) - 300)
        time.sleep(0.03)
        if len(out) >= 32 * 288 + 80:
            break
    return out


def slim_from_raw(raw: list[dict], cid: str, end: int) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or "").upper() != "BUY":
            continue
        if cid and str(t.get("conditionId") or "") not in {"", str(cid)}:
            continue
        try:
            px = float(t.get("price") or t.get("px") or 0)
            ts = int(t.get("timestamp") or t.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if px < PX_LO or px > PX_HI:
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc not in {"Up", "Down"}:
            continue
        left = end - ts
        if left < -2 or left > 305:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc})
    out.sort(key=lambda x: x["ts"])
    return out


def slim_from_old(path: Path, end: int) -> list[dict]:
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    rows = raw if isinstance(raw, list) else raw.get("trades") or []
    fake = []
    for t in rows:
        fake.append(
            {
                "side": t.get("side") or "BUY",
                "price": t.get("px") or t.get("price"),
                "timestamp": t.get("ts") or t.get("timestamp"),
                "outcome": t.get("outcome"),
                "conditionId": "",
            }
        )
    return slim_from_raw(fake, "", end)


def fetch_prints(ev: dict) -> list[dict]:
    dest = CACHE / f"{ev['slug']}.json"
    if dest.exists() and dest.stat().st_size > 8:
        try:
            rows = json.loads(dest.read_text())
            if isinstance(rows, list):
                return rows
        except (json.JSONDecodeError, OSError):
            pass
    old = OLD_CACHE / f"{ev['slug']}.json"
    if old.exists():
        rows = slim_from_old(old, ev["end"])
        if rows:
            dest.write_text(json.dumps(rows))
            return rows
    pages = 6 if ev["asset"] in CORE else 4
    raw: list[dict] = []
    for page in range(pages):
        chunk = (
            http_json(f"{DATA}/trades?market={ev['cid']}&limit=1000&offset={page * 1000}&takerOnly=true")
            or []
        )
        raw.extend(chunk)
        if len(chunk) < 1000:
            break
        lefts = []
        for t in chunk:
            try:
                lefts.append(ev["end"] - int(t.get("timestamp") or 0))
            except (TypeError, ValueError):
                pass
        if lefts and max(lefts) >= 270:
            break
        time.sleep(0.02)
    rows = slim_from_raw(raw, ev["cid"], ev["end"])
    dest.write_text(json.dumps(rows))
    return rows


def ingest() -> list[dict]:
    CACHE.mkdir(parents=True, exist_ok=True)
    oldest = int(time.time()) - 30 * 86400
    ev_path = CACHE / "_events.json"
    events: list[dict] = []
    if ev_path.exists():
        try:
            events = json.loads(ev_path.read_text())
        except (json.JSONDecodeError, OSError):
            events = []
    have = {e["slug"] for e in events}
    for asset in ASSETS:
        print(f"list {asset}", flush=True)
        r30.SERIES[asset] = SERIES[asset]
        got = list_events(asset, oldest)
        n_new = 0
        for e in got:
            if e["slug"] not in have:
                events.append(e)
                have.add(e["slug"])
                n_new += 1
        print(f"  {asset} listed {len(got)} new {n_new}", flush=True)
    events.sort(key=lambda e: e["end"], reverse=True)
    ev_path.write_text(json.dumps(events))
    twap_ev = [e for e in events if e["end"] >= TWAP60]
    print(f"events {len(events)} twap60 {len(twap_ev)}", flush=True)
    reused = 0
    for e in twap_ev:
        dest = CACHE / f"{e['slug']}.json"
        if dest.exists() and dest.stat().st_size > 8:
            continue
        old = OLD_CACHE / f"{e['slug']}.json"
        if old.exists():
            rows = slim_from_old(old, e["end"])
            dest.write_text(json.dumps(rows))
            reused += 1
    print(f"reused old cache {reused}", flush=True)
    need = [
        e
        for e in twap_ev
        if e["asset"] not in SKIP_PRINT_ASSETS
        and not ((CACHE / f"{e['slug']}.json").exists() and (CACHE / f"{e['slug']}.json").stat().st_size > 8)
    ]
    print(f"prints to fetch {len(need)} skip={sorted(SKIP_PRINT_ASSETS)}", flush=True)
    done = 0
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(fetch_prints, e): e for e in need}
        for fut in as_completed(futs):
            ev = futs[fut]
            done += 1
            try:
                rows = fut.result()
            except Exception as exc:
                print(f"  fail {ev['slug']} {type(exc).__name__}", flush=True)
                continue
            if done % 200 == 0 or done == len(need):
                print(f"  prints {done}/{len(need)} last {ev['slug']} n={len(rows)}", flush=True)
    return events


def load_prints(ev: dict) -> list[dict]:
    path = CACHE / f"{ev['slug']}.json"
    if not path.exists():
        return []
    try:
        rows = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return rows if isinstance(rows, list) else []


def last_print(prints, ts, outcome, slack=25):
    return te.last_print(prints, ts, outcome, slack=slack)


def simulate_window(ev, series, prints, params: TwapParams) -> dict[str, dict | None]:
    """One 5s walk → first and last take under loose entry (min_left=120, no late cheap)."""
    start, end = int(ev["start"]), int(ev["end"])
    winner = ev["winner"]
    tw_open = series.twap(start, params.lookback)
    if tw_open is None or tw_open <= 0:
        return {"first": None, "last": None}
    t0 = start + 15
    t1 = end - int(params.min_left)
    first = last = None
    for ts in range(t0, t1 + 1, 5):
        left = end - ts
        if left > params.max_left or left < params.min_left:
            continue
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open)
        if lead is None or abs(lead) < params.min_lead_bps:
            continue
        if abs(lead) > MAX_LEAD + 1e-12:
            continue
        side = "Up" if lead >= 0 else "Down"
        pr = last_print(prints, ts, side, slack=25)
        if pr is None:
            continue
        if not (params.min_price - 1e-12 <= pr["px"] <= params.max_price + 1e-12):
            continue
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) < params.min_edge:
            continue
        z = lead_z(lead if side == "Up" else -lead, vol, float(left), lookback=params.lookback)
        cand = {
            "ts": ts,
            "left": left,
            "side": side,
            "px": pr["px"],
            "lead": lead,
            "fair": fair,
            "vol": vol,
            "z": z,
        }
        if first is None:
            first = cand
        last = cand
    out = {"first": None, "last": None}
    for key, picked in (("first", first), ("last", last)):
        if picked is None:
            continue
        shares = NOTIONAL / max(picked["px"], 0.01)
        exit_px = None
        exit_why = "settle"
        for ts in range(picked["ts"] + te.RESCORE, end - 3, te.RESCORE):
            left = end - ts
            tw = series.twap(ts, params.lookback)
            if tw is None:
                continue
            lead = lead_bps(tw, tw_open) or 0.0
            signed = lead if picked["side"] == "Up" else -lead
            vol = series.realized_vol_bps_sqrt_s(ts, 120)
            fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
            fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
            mark = last_print(prints, ts, picked["side"], slack=30)
            bid = None if mark is None else mark["px"]
            go, why = should_scratch(
                fair_p=fair,
                lead_bps_signed=signed,
                bid=bid,
                shares=shares,
                fee_rate=0.07,
                left=float(left),
                params=params,
                asset=ev.get("asset"),
            )
            if not go:
                continue
            nxt = te.next_print(prints, ts, picked["side"], slack=8) or mark
            if nxt is None:
                continue
            exit_px = nxt["px"]
            exit_why = why
            break
        won = picked["side"] == winner
        if exit_px is not None:
            pnl = te.pnl_scratch(picked["px"], exit_px)
            scratched = True
        else:
            pnl = te.pnl_hold(picked["px"], won)
            scratched = False
        out[key] = {
            "slug": ev["slug"],
            "asset": ev["asset"],
            "start": start,
            "end": end,
            "side": picked["side"],
            "px": picked["px"],
            "left": picked["left"],
            "lead": round(picked["lead"], 4),
            "fair": round(picked["fair"], 4),
            "vol": None if picked["vol"] is None else round(picked["vol"], 4),
            "z": None if picked["z"] is None else round(picked["z"], 4),
            "won": won,
            "scratched": scratched,
            "exit_why": exit_why,
            "pnl": round(pnl, 5),
            "edge": round(entry_edge(picked["fair"], picked["px"], 0.07), 4),
        }
    return out


def ok_row(
    row: dict,
    *,
    alt_min_left: float,
    core_min_left: float,
    min_lead: float,
    late_cheap: bool,
    min_edge: float = 0.04,
    min_fair: float = 0.0,
    max_left: float = 280.0,
    max_vol: float | None = None,
    min_vol: float | None = None,
) -> bool:
    asset = row["asset"]
    need = core_min_left if asset in CORE else alt_min_left
    if row["left"] < need or row["left"] > max_left:
        return False
    if abs(row["lead"]) < min_lead:
        return False
    if late_cheap and row["left"] < 180 and row["px"] + 1e-12 < 0.50:
        return False
    if float(row.get("edge") or 0) + 1e-12 < min_edge:
        return False
    if float(row.get("fair") or 0) + 1e-12 < min_fair:
        return False
    vol = row.get("vol")
    if max_vol is not None and vol is not None and float(vol) > max_vol:
        return False
    if min_vol is not None and (vol is None or float(vol) < min_vol):
        return False
    return True


def clock_lock(rows: list[dict], *, rank: str) -> list[dict]:
    by: dict[int, list[dict]] = {}
    for r in rows:
        by.setdefault(int(r["start"]), []).append(r)
    out = []
    for start, xs in by.items():
        if rank == "edge":
            xs = sorted(xs, key=lambda r: (float(r.get("edge") or 0), abs(r["lead"])), reverse=True)
        else:
            xs = sorted(xs, key=lambda r: (abs(r["lead"]), -float(r["left"])), reverse=True)
        out.append(xs[0])
    return out


def split_holdout(rows: list[dict], days: int = HOLDOUT_DAYS):
    if not rows:
        return [], []
    newest = max(r["end"] for r in rows)
    cut = newest - days * 86400
    return [r for r in rows if r["end"] < cut], [r for r in rows if r["end"] >= cut]


def summarize(rows: list[dict]) -> dict:
    rec = te.summarize(rows)
    rec["pnl_live_usd"] = round(rec["pnl_usd"] * (LIVE_STAKE / NOTIONAL), 2)
    rec["n_assets"] = len({r["asset"] for r in rows})
    rec["by_asset"] = {}
    for a in ASSETS:
        sub = [r for r in rows if r["asset"] == a]
        if sub:
            rec["by_asset"][a] = te.summarize(sub)
    return rec


def pack(rows: list[dict]) -> dict:
    train, hold = split_holdout(rows)
    rec = {"all": summarize(rows), "train": summarize(train), "holdout": summarize(hold)}
    rec["robust"] = bool(rec["train"]["ev_ok"] and rec["holdout"]["ev_ok"] and rec["train"]["n"] >= 25 and rec["holdout"]["n"] >= 25)
    return rec


def grid(events: list[dict]) -> dict:
    twap_ev = [e for e in events if e["end"] >= TWAP60]
    newest = max(e["end"] for e in twap_ev)
    t0, t1 = TWAP60 - 180, newest + 5
    rp.SYMBOL.update(SYMBOL)
    series_of = {}
    for asset in ASSETS:
        if asset in SKIP_PRINT_ASSETS:
            print(f"load series skip {asset}", flush=True)
            series_of[asset] = None
            continue
        print(f"load series {asset}", flush=True)
        try:
            series_of[asset] = rp.load_series(asset, t0, t1)
        except Exception as exc:
            print(f"  no series {asset} {type(exc).__name__}", flush=True)
            series_of[asset] = None

    params_loose = TwapParams(
        min_price=0.45,
        max_price=0.55,
        min_lead_bps=6.0,
        min_edge=0.04,
        min_left=120.0,
        max_left=280.0,
        late_left=0.0,
        late_min_price=0.0,
        max_lead_bps=40.0,
    )

    last_rows: list[dict] = []
    first_rows: list[dict] = []
    if SIM_CACHE.exists():
        try:
            cached = json.loads(SIM_CACHE.read_text())
            last_rows = list(cached.get("last") or [])
            first_rows = list(cached.get("first") or [])
            print(f"sim cache last={len(last_rows)} first={len(first_rows)}", flush=True)
        except (json.JSONDecodeError, OSError):
            last_rows, first_rows = [], []
    have_last = {r["slug"] for r in last_rows}
    have_first = {r["slug"] for r in first_rows}

    for asset in ASSETS:
        series = series_of.get(asset)
        if series is None:
            print(f"sim skip {asset} (no 1s series)", flush=True)
            continue
        evs = [e for e in twap_ev if e["asset"] == asset]
        n_ok = 0
        for i, ev in enumerate(evs, 1):
            if ev["slug"] in have_last or ev["slug"] in have_first:
                if ev["slug"] in have_last:
                    n_ok += 1
                continue
            prints = load_prints(ev)
            if not prints:
                continue
            got = simulate_window(ev, series, prints, params_loose)
            if got.get("last"):
                last_rows.append(got["last"])
                have_last.add(ev["slug"])
                n_ok += 1
            if got.get("first"):
                first_rows.append(got["first"])
                have_first.add(ev["slug"])
            if i % 400 == 0:
                print(f"  {asset} {i}/{len(evs)} last={n_ok}", flush=True)
                SIM_CACHE.write_text(json.dumps({"last": last_rows, "first": first_rows}))
        print(
            f"sim {asset} windows={len(evs)} last={sum(1 for r in last_rows if r['asset']==asset)} "
            f"first={sum(1 for r in first_rows if r['asset']==asset)}",
            flush=True,
        )
        SIM_CACHE.write_text(json.dumps({"last": last_rows, "first": first_rows}))

    variants = []

    def add(name, rows, **meta):
        rec = pack(rows)
        rec["name"] = name
        rec.update(meta)
        variants.append(rec)
        h = rec["holdout"]
        print(
            f"{name:28s} n={rec['all']['n']:4d} all ${rec['all']['pnl_usd']:+7.1f} "
            f"(${rec['all']['pnl_live_usd']:+.1f} @$3) hold n={h['n']:3d} "
            f"hit={h.get('take_win_rate')} pnl={h['pnl_usd']:+.1f} robust={rec['robust']}",
            flush=True,
        )

    # Independent (overstated vs live)
    add("indep_last_min120", last_rows, clock=False, pick="last")
    add("indep_first_min120", first_rows, clock=False, pick="first")

    def v(name, src, *, alt_min=180.0, core_min=120.0, min_lead=6.0, late=True, rank="lead", clock=True, assets=None, min_edge=0.04, min_fair=0.0, max_left=280.0, max_vol=None, min_vol=None):
        pool = src
        if assets is not None:
            pool = [r for r in pool if r["asset"] in assets]
        ready = [
            r
            for r in pool
            if ok_row(
                r,
                alt_min_left=alt_min,
                core_min_left=core_min,
                min_lead=min_lead,
                late_cheap=late,
                min_edge=min_edge,
                min_fair=min_fair,
                max_left=max_left,
                max_vol=max_vol,
                min_vol=min_vol,
            )
        ]
        locked = clock_lock(ready, rank=rank) if clock else ready
        add(
            name,
            locked,
            clock=clock,
            alt_min_left=alt_min,
            core_min_left=core_min,
            min_lead=min_lead,
            late_cheap=late,
            rank=rank,
            min_edge=min_edge,
            min_fair=min_fair,
            max_left=max_left,
        )

    v("rev48_clock_last", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, rank="lead")
    v("rev48_clock_first", first_rows, alt_min=180, core_min=120, min_lead=6, late=True, rank="lead")
    v("clock_all_min120", last_rows, alt_min=120, core_min=120, min_lead=6, late=True)
    v("clock_all_min180", last_rows, alt_min=180, core_min=180, min_lead=6, late=True)
    v("clock_alt200", last_rows, alt_min=200, core_min=120, min_lead=6, late=True)
    v("clock_alt210", last_rows, alt_min=210, core_min=120, min_lead=6, late=True)
    v("clock_alt240", last_rows, alt_min=240, core_min=120, min_lead=6, late=True)
    v("clock_lead7", last_rows, alt_min=180, core_min=120, min_lead=7, late=True)
    v("clock_lead8", last_rows, alt_min=180, core_min=120, min_lead=8, late=True)
    v("clock_lead10", last_rows, alt_min=180, core_min=120, min_lead=10, late=True)
    v("clock_edge05", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, min_edge=0.05)
    v("clock_fair58", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, min_fair=0.58)
    v("clock_fair62", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, min_fair=0.62)
    v("clock_maxleft260", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, max_left=260)
    v("clock_no_late_cheap", last_rows, alt_min=180, core_min=120, min_lead=6, late=False)
    v("clock_pick_edge", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, rank="edge")
    v("clock_core_only", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, assets=set(CORE))
    v("clock_alts_only", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, assets=set(ASSETS) - set(CORE))
    v("rev48_no_clock", last_rows, alt_min=180, core_min=120, min_lead=6, late=True, clock=False)
    # Same filters on first-take (closer to live FOK).
    v("first_alt200", first_rows, alt_min=200, core_min=120, min_lead=6, late=True)
    v("first_lead8", first_rows, alt_min=180, core_min=120, min_lead=8, late=True)
    v("first_edge05", first_rows, alt_min=180, core_min=120, min_lead=6, late=True, min_edge=0.05)

    base = next(x for x in variants if x["name"] == "rev48_clock_last")
    base_first = next(x for x in variants if x["name"] == "rev48_clock_first")
    base_h = base["holdout"]["pnl_usd"]

    def core_hold_pnl(rec: dict) -> float:
        by = rec["holdout"].get("by_asset") or {}
        return round(sum(float((by.get(a) or {}).get("pnl_usd") or 0) for a in CORE), 2)

    base_core = core_hold_pnl(base)
    winners = []
    for x in variants:
        if x["name"] == "rev48_clock_last":
            continue
        if not x["robust"]:
            continue
        if x["holdout"]["pnl_usd"] + 1e-9 < base_h:
            continue
        if x["name"] in DO_NOT_SHIP_NAMES:
            continue
        cand_core = core_hold_pnl(x)
        if base_core > 0 and cand_core + 1e-9 < 0.95 * base_core:
            continue
        winners.append(
            {
                "name": x["name"],
                "holdout_pnl": x["holdout"]["pnl_usd"],
                "holdout_hit": x["holdout"].get("take_win_rate"),
                "train_pnl": x["train"]["pnl_usd"],
                "delta_vs_rev48_hold": round(x["holdout"]["pnl_usd"] - base_h, 2),
                "core_holdout_pnl": cand_core,
                "core_holdout_vs_base": round(cand_core - base_core, 2),
            }
        )
    winners.sort(key=lambda z: z["delta_vs_rev48_hold"], reverse=True)

    coverage = {}
    for a in ASSETS:
        evs = [e for e in twap_ev if e["asset"] == a]
        n_print = sum(1 for e in evs if (CACHE / f"{e['slug']}.json").exists() and load_prints(e))
        coverage[a] = {
            "windows": len(evs),
            "with_prints": n_print,
            "series": series_of.get(a) is not None,
            "last_takes": sum(1 for r in last_rows if r["asset"] == a),
        }

    return {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": "Binance 1s TWAP vs Binance T0 + CLOB 20–62¢ BUY prints. Live = Chainlink vs Chainlink T0.",
        "window": {
            "twap60_start": TWAP60,
            "newest_end": newest,
            "holdout_days": HOLDOUT_DAYS,
            "notional_research": NOTIONAL,
            "notional_live": LIVE_STAKE,
            "assets": list(ASSETS),
        },
        "coverage": coverage,
        "variants": [
            {
                "name": x["name"],
                "all": x["all"],
                "train": x["train"],
                "holdout": x["holdout"],
                "robust": x["robust"],
                "clock": x.get("clock"),
                "alt_min_left": x.get("alt_min_left"),
                "core_min_left": x.get("core_min_left"),
                "min_lead": x.get("min_lead"),
                "late_cheap": x.get("late_cheap"),
                "rank": x.get("rank"),
                "min_edge": x.get("min_edge"),
                "min_fair": x.get("min_fair"),
                "max_left": x.get("max_left"),
            }
            for x in variants
        ],
        "shippable_vs_rev48_clock": winners,
        "baselines": {
            "rev48_clock_last_holdout": base_h,
            "rev48_clock_first_holdout": base_first["holdout"]["pnl_usd"],
            "rev48_clock_last_core_holdout": base_core,
        },
        "do_not_ship": [
            "clock_all_min120 — live alt 120–180 held 0/3 (−$8.33) even if proxy likes it",
            "clock_all_min180 — do not raise BTC/ETH min_left to 180",
            "rev48_no_clock / indep_* — live is one coin per unix",
            "clock_core_only — do not cut Telegram coins",
            "scratch_adverse 0.08",
            "lower 6bps without dual-split +EV",
        ],
    }


def main() -> None:
    stage = (sys.argv[1] if len(sys.argv) > 1 else "all").lower()
    events = []
    if stage in {"ingest", "all"}:
        events = ingest()
    else:
        events = json.loads((CACHE / "_events.json").read_text())
    if stage == "ingest":
        print("ingest done", flush=True)
        return
    report = grid(events)
    if not report.get("shippable_vs_rev48_clock"):
        report["findings"] = {
            "headline": "Keep Rev 48. No clock-lock variant beat it on holdout without reopening a live-known bleed.",
            "ship": False,
        }
    else:
        report["findings"] = {
            "headline": f"Candidates: {[w['name'] for w in report['shippable_vs_rev48_clock'][:5]]}",
            "ship": True,
        }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT, flush=True)
    print("findings", report.get("findings"), flush=True)


if __name__ == "__main__":
    main()
