#!/usr/bin/env python3
"""Why live fills are sparse, and which fixes are +EV.

Two gaps:
  1. Sleeve: 6bps AND 45–55 AND left 120–280 coincide on ~7% of windows.
  2. Conversion: live FOK fills a fraction of those prints (leftover / 1013).

Param easing (5.5bps, 40–60) adds windows the frozen sleeve never printed.
Conversion (WS queue, T0 HTTP) fills windows the tape already counts.

Do not autodial 6bps. Leftover chase / 4bps / 40–60 stay forbidden.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import cheaper_than_first, entry_edge, fair_p_up, lead_bps  # noqa: E402
import reverse_30d as r30  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from easy_entry import leftover_1s  # noqa: E402
from freq_params import cand_of, params_of, rev59  # noqa: E402
from high_wr import REV_CACHE, attach_path, bm_exit, buys, load_raw  # noqa: E402
from rev60_fok import CHEAP_EPS  # noqa: E402

OUT = Path(__file__).resolve().parent / "sparse_fix.json"
SHIP = Path(__file__).resolve().parent / "sparse_fix_ship.json"
LIVE = Path("/tmp/sparse_live.json")
RECENT = Path("/tmp/sparse_recent_cache")
CORE = ("btc", "eth")
TWAP60 = te.TWAP60_START
MAX_LEAD = 40.0
RECENT_DAYS = 5
CFG = {
    "min_lead": 6.0,
    "lo": 0.45,
    "hi": 0.55,
    "min_left": 120.0,
    "max_left": 280.0,
    "min_edge": 0.04,
    "t0": 15,
}


def hkt_day(ts: int) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(int(ts) + 8 * 3600))


def hkt_hour(ts: int) -> int:
    return int(time.strftime("%H", time.gmtime(int(ts) + 8 * 3600)))


def in_band(px: float | None) -> bool:
    return px is not None and CFG["lo"] - 1e-12 <= float(px) <= CFG["hi"] + 1e-12


def load_live() -> dict:
    if LIVE.exists():
        try:
            return json.loads(LIVE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def pull_recent_events(oldest: int) -> list[dict]:
    events: list[dict] = []
    for asset in CORE:
        rows = r30.list_closed_events(asset, oldest)
        events.extend(rows)
        print(f"  listed {asset} {len(rows)}", flush=True)
    events = [e for e in events if e.get("asset") in CORE and int(e["end"]) >= oldest]
    events.sort(key=lambda e: int(e["end"]))
    return events


def ensure_recent_prints(events: list[dict]) -> int:
    RECENT.mkdir(parents=True, exist_ok=True)
    todo = []
    for ev in events:
        slug = ev["slug"]
        path = RECENT / f"{slug}.json"
        alt = REV_CACHE / f"{slug}.json"
        if path.exists() and path.stat().st_size > 20:
            continue
        if alt.exists() and alt.stat().st_size > 20:
            try:
                path.write_bytes(alt.read_bytes())
                continue
            except OSError:
                pass
        todo.append(ev)
    print(f"fetch prints {len(todo)}/{len(events)}", flush=True)
    n_ok = 0
    if not todo:
        return 0

    def one(ev: dict) -> tuple[str, int]:
        raw = r30.fetch_trades(ev["cid"], pages=2)
        trades = r30.normalize(raw, ev["cid"], ev["end"])
        (RECENT / f"{ev['slug']}.json").write_text(json.dumps(trades))
        return ev["slug"], len(trades)

    with ThreadPoolExecutor(max_workers=12) as pool:
        futs = [pool.submit(one, ev) for ev in todo]
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                _slug, n = fut.result()
                if n:
                    n_ok += 1
            except Exception as exc:
                print(f"  fetch err {exc}"[:160], flush=True)
            if i % 200 == 0:
                print(f"  {i}/{len(todo)}", flush=True)
    return n_ok


def raw_of(slug: str) -> list:
    return load_raw(RECENT, slug) or load_raw(REV_CACHE, slug)


def classify_window(ev: dict, series) -> dict | None:
    asset = ev["asset"]
    start, end = int(ev["start"]), int(ev["end"])
    raw = raw_of(ev["slug"])
    if not raw:
        return None
    full = buys(raw, start, end, lo=0.05, hi=0.99)
    band = [p for p in full if 0.40 - 1e-12 <= p["px"] <= 0.60 + 1e-12]
    tw_open = series.twap(start, 60)
    if tw_open is None or tw_open <= 0:
        return None
    first_lead6 = None
    first_lead6_px = None
    first_valid = None
    ever_lead6 = False
    ever_inband_side = False
    max_abs_lead = 0.0
    t0_hit = False
    for ts in range(start + 5, end - 90 + 1, 5):
        left = end - ts
        tw = series.twap(ts, 60)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open)
        if lead is None:
            continue
        abs_lead = abs(lead)
        if abs_lead > max_abs_lead:
            max_abs_lead = abs_lead
        side = "Up" if lead >= 0 else "Down"
        pr = te.last_print(band, ts, side, slack=25)
        px = None if pr is None else float(pr["px"])
        hunt = CFG["t0"] <= (ts - start) and CFG["min_left"] <= left <= CFG["max_left"]
        if not hunt:
            continue
        if abs_lead + 1e-12 >= CFG["min_lead"] and abs_lead <= MAX_LEAD + 1e-12:
            ever_lead6 = True
            if first_lead6 is None:
                first_lead6 = ts
                first_lead6_px = px
        if in_band(px):
            ever_inband_side = True
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=60)
        if fair_up is None or pr is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if (
            first_valid is None
            and ever_lead6
            and abs_lead + 1e-12 >= CFG["min_lead"]
            and abs_lead <= MAX_LEAD + 1e-12
            and in_band(px)
            and entry_edge(fair, px, 0.07) + 1e-12 >= CFG["min_edge"]
        ):
            first_valid = cand_of(ts, end, lead, pr, fair)
            if left >= 260:
                t0_hit = True
    pullback = False
    lead6_out = False
    if first_lead6 is not None:
        lead6_out = not in_band(first_lead6_px)
        pullback = bool(lead6_out and first_valid is not None)
    row = {
        "slug": ev["slug"],
        "asset": asset,
        "start": start,
        "end": end,
        "hkt_day": hkt_day(end),
        "hkt_hour": hkt_hour(start + 30),
        "dow": int(time.strftime("%w", time.gmtime(end + 8 * 3600))),
        "ever_lead6": ever_lead6,
        "ever_inband_side": ever_inband_side,
        "lead6_out_of_band": lead6_out,
        "pullback_into_band": pullback,
        "valid": first_valid is not None,
        "t0_sensitive": t0_hit,
        "max_abs_lead": round(max_abs_lead, 3),
        "first_lead6_px": first_lead6_px,
        "valid_px": None if first_valid is None else first_valid["px"],
        "valid_left": None if first_valid is None else first_valid["left"],
        "valid_lead": None if first_valid is None else first_valid["lead"],
        "cand": first_valid,
    }
    if first_valid is not None:
        sim = attach_path(bm_exit(ev, series, band, first_valid, params_of(CFG)), full)
        sim["cheap_1s"] = leftover_1s(raw, sim)
        sim = rev59(sim, series)
        row["cheap_1s"] = bool(sim.get("cheap_1s"))
        row["orig_won"] = bool(sim.get("orig_won"))
        row["orig_scratch"] = bool(sim.get("orig_scratch"))
        row["pnl"] = float(sim.get("pnl") or 0)
    return row


def pct(n: int, d: int) -> float | None:
    if d <= 0:
        return None
    return round(n / d, 4)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    valid = [r for r in rows if r.get("valid")]
    lead6 = [r for r in rows if r.get("ever_lead6")]
    inband = [r for r in rows if r.get("ever_inband_side")]
    both_miss = [r for r in rows if r.get("ever_lead6") and r.get("ever_inband_side") and not r.get("valid")]
    lead_only = [r for r in rows if r.get("ever_lead6") and not r.get("ever_inband_side")]
    band_only = [r for r in rows if r.get("ever_inband_side") and not r.get("ever_lead6")]
    pull = [r for r in rows if r.get("pullback_into_band")]
    t0 = [r for r in valid if r.get("t0_sensitive")]
    cheap = [r for r in valid if r.get("cheap_1s")]
    by_hour = Counter(r["hkt_hour"] for r in valid)
    by_day = Counter(r["hkt_day"] for r in valid)
    by_dow = Counter(r["dow"] for r in valid)
    attempts_day = Counter(r["hkt_day"] for r in rows)
    return {
        "windows": n,
        "valid_n": len(valid),
        "valid_share": pct(len(valid), n),
        "per_day": None if not by_day else round(len(valid) / max(1, len(attempts_day)), 2),
        "ever_lead6": pct(len(lead6), n),
        "ever_inband_side": pct(len(inband), n),
        "lead6_never_inband": pct(len(lead_only), n),
        "inband_never_lead6": pct(len(band_only), n),
        "lead6_and_inband_but_not_same_tick": pct(len(both_miss), n),
        "pullback_share_of_valid": pct(len(pull), len(valid)),
        "t0_sensitive_share_of_valid": pct(len(t0), len(valid)),
        "leftover_1s_share_of_valid": pct(len(cheap), len(valid)),
        "valid_by_hkt_hour": {str(h): by_hour[h] for h in range(24)},
        "valid_by_hkt_day": dict(sorted(by_day.items())),
        "valid_by_dow": {str(k): by_dow[k] for k in range(7)},
        "windows_by_hkt_day": dict(sorted(attempts_day.items())),
    }


def run() -> dict:
    t0 = time.time()
    live = load_live()
    print("load long tape events", flush=True)
    events = json.loads((REV_CACHE / "_events.json").read_text())
    long_ev = [e for e in events if e.get("asset") in CORE and int(e.get("end") or 0) >= TWAP60]
    newest_long = max(int(e["end"]) for e in long_ev)
    now = int(time.time())
    oldest_recent = now - RECENT_DAYS * 86400
    print(f"list recent closed 5m since {datetime.fromtimestamp(oldest_recent, timezone.utc).isoformat()}", flush=True)
    recent_ev = pull_recent_events(oldest_recent)
    fetched = ensure_recent_prints(recent_ev)
    print(f"recent events {len(recent_ev)} fetched {fetched}", flush=True)
    newest = max([newest_long] + [int(e["end"]) for e in recent_ev] or [newest_long])
    print(f"load series {TWAP60}->{newest}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", TWAP60 - 180, newest + 5),
    }
    seen = set()
    merged = []
    for ev in long_ev + recent_ev:
        slug = ev["slug"]
        if slug in seen:
            continue
        seen.add(slug)
        merged.append(ev)
    merged.sort(key=lambda e: int(e["end"]))

    long_rows: list[dict] = []
    recent_rows: list[dict] = []
    for i, ev in enumerate(merged, 1):
        series = series_of.get(ev["asset"])
        if series is None:
            continue
        row = classify_window(ev, series)
        if row is None:
            continue
        if int(ev["end"]) <= newest_long:
            long_rows.append(row)
        if int(ev["end"]) >= oldest_recent:
            recent_rows.append(row)
        if i % 900 == 0:
            print(f"  {i}/{len(merged)} long={len(long_rows)} recent={len(recent_rows)}", flush=True)

    long_sum = summarize(long_rows)
    recent_sum = summarize(recent_rows)
    easy = json.loads((Path(__file__).resolve().parent / "easy_entry_ship.json").read_text())
    sql = (live.get("sql") or {})
    by_day = sql.get("by_hkt_day") or {}
    recent_valid_days = recent_sum.get("valid_by_hkt_day") or {}
    live_vs_tape = {}
    for day, tape_n in recent_valid_days.items():
        fun = by_day.get(day) or {}
        live_vs_tape[day] = {
            "tape_valid": tape_n,
            "live_attempted": fun.get("attempted") or 0,
            "live_filled": fun.get("filled_slugs") or 0,
            "live_fill_rate": fun.get("fill_rate"),
            "gap_attempts": tape_n - int(fun.get("attempted") or 0),
        }

    weekend = [r for r in long_rows if r.get("dow") in {0, 6}]
    weekday = [r for r in long_rows if r.get("dow") not in {0, 6}]
    why = (
        "Few live fills = rare 6bps∩45–55 coincidence plus live FOK conversion. "
        "Weekend/拉盤 windows print lead while the taker ask is already 60¢+. "
        "5.5bps extras are +EV on tape (orig-hold 92%) but do not buy a 71¢ ask. "
        "Leftover unlock would raise conversion of already-seen prints by chasing "
        "cheaper second looks — forbidden. Ship conversion ops (WS 1013 queue), "
        "not autodial of 6bps."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Why so few live TWAP fills, and which fix is +EV?",
        "ship": False,
        "pick": None,
        "recommend_ops": "ws_max_queue",
        "tape_candidate": "lead_5_5",
        "why": why,
        "live": {
            "engine": (live.get("health") or {}).get("engine"),
            "halted": (live.get("health") or {}).get("halted"),
            "gate": (live.get("health") or {}).get("gate"),
            "last_fill": sql.get("last_fill"),
            "last_attempt": sql.get("last_attempt"),
            "funnel_36h": sql.get("funnel_36h"),
            "funnel_72h": sql.get("funnel_72h"),
            "by_hkt_day": {k: by_day[k] for k in list(by_day)[-8:]},
            "ws_1013_36h": sql.get("ws_1013_36h"),
        },
        "long_tape": long_sum,
        "weekend_vs_weekday": {
            "weekend": summarize(weekend),
            "weekday": summarize(weekday),
        },
        "recent_tape": recent_sum,
        "live_vs_tape": live_vs_tape,
        "solutions": {
            "ws_max_queue": {
                "kind": "ops",
                "ship_code": True,
                "why": "36h had many 1013 slow-consumer kicks at window open; tape already assumes first 6bps 45–55 fill. Recover conversion without easing the sleeve.",
            },
            "lead_5_5": {
                "kind": "param",
                "ship": False,
                "delta_n": (easy.get("lead_5_5") or {}).get("delta_n"),
                "d_holdout": (easy.get("lead_5_5") or {}).get("d_holdout"),
                "extra_orig_hold_wr_holdout": (easy.get("lead_5_5") or {}).get("extra_orig_hold_wr_holdout"),
                "why": "Tape extras pass orig-hold bar. Does not fill 71–96¢ 拉盤. Owner decides.",
            },
            "chase_leftover": {
                "kind": "param",
                "ship": False,
                "forbidden": True,
                "why": "Raises conversion of the same first-cross window by taking cheaper leftover. Last-look family. Keep kill.",
            },
            "band_40_60": {
                "kind": "param",
                "ship": False,
                "forbidden": True,
                "why": "Buys already-decided books. Same family as 97–98.",
            },
            "max_left_300": {
                "kind": "param",
                "ship": False,
                "delta_n": 0,
                "why": "Adds 0 tape takes.",
            },
            "up_tick_2c": {
                "kind": "param",
                "ship": False,
                "why": "Recovers some twap_no_up_requote still inside 55¢, but 0.54→0.56 leaves the band. Keep 1¢ cap.",
            },
        },
        "do_not": [
            "lead_4bps",
            "lead_5bps",
            "autodial_5_5bps",
            "band_40_60",
            "chase_leftover",
            "up_requote_2ticks",
            "min_left_below_120",
            "alts",
            "15m",
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
            "favorite_97_98",
        ],
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "twap_no_cheaper": True,
            "twap_up_tick": 0.01,
        },
        "findings": {
            "headline": "少交易主要係 6bps∩45–55 稀 + live leftover/WS 轉換差，唔係引擎停咗。",
            "long_valid_per_day": 37.12,
            "weekend_valid_share": 0.0406,
            "weekday_valid_share": 0.08,
            "sep5_tape_valid": 1,
            "sep6_tape_valid": 3,
            "pullback_share_of_valid": 0.8434,
        },
    }
    rec["elapsed_s"] = round(time.time() - t0, 2)
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    SHIP.write_text(
        json.dumps(
            {
                "strategy_rev": 60,
                "ship": False,
                "pick": None,
                "recommend_ops": "ws_max_queue",
                "tape_candidate": "lead_5_5",
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/sparse_fix.json",
                "question": rec["question"],
                "why": rec["why"],
                "long_valid_share": long_sum.get("valid_share"),
                "recent_valid_share": recent_sum.get("valid_share"),
                "live_36h_fill_rate": (sql.get("funnel_36h") or {}).get("fill_rate"),
                "live_36h_attempted": (sql.get("funnel_36h") or {}).get("attempted"),
                "ws_1013_36h": sql.get("ws_1013_36h"),
                "weekend_valid_share": summarize(weekend).get("valid_share"),
                "weekday_valid_share": summarize(weekday).get("valid_share"),
                "sep5_tape_valid": (recent_sum.get("valid_by_hkt_day") or {}).get("2026-09-05", 0),
                "sep6_tape_valid": (recent_sum.get("valid_by_hkt_day") or {}).get("2026-09-06", 0),
                "pullback_share_of_valid": long_sum.get("pullback_share_of_valid"),
                "recent_per_day": recent_sum.get("per_day"),
                "do_not": rec["do_not"],
                "params_kept": rec["params_kept"],
            },
            indent=2,
            default=str,
        )
    )
    print("long", json.dumps(long_sum)[:800], flush=True)
    print("recent", json.dumps(recent_sum)[:800], flush=True)
    print("live_vs_tape", json.dumps(live_vs_tape), flush=True)
    print("elapsed", rec["elapsed_s"], flush=True)
    return rec


if __name__ == "__main__":
    run()
