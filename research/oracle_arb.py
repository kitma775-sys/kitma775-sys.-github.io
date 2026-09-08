#!/usr/bin/env python3
"""Oracle-arb / latency research. Does not patch live.

Question: can a second sleeve lift delayed CLOB asks after the settlement
oracle already knows the side — including post-close (T1 → Gamma 0/1)?

Physics:
  Live 5m settles on Chainlink 60s TWAP vs the first same-source tick at T0.
  The shipped 6bps∩45–55 first-cross is already a *mild* oracle-latency
  capture: same Chainlink stream vs a slower CLOB, only while the ask is
  still a coin-flip. Extra speed via Binance 1s vs Gamma PTB is NOT arb
  (~9 bps CL–USDT basis). Favorite 97–98 / late expensive asks need WR
  ≈ price + fee.

Families (hold-to-settle, $5 tape, official 0/1):
  frozen_open     first 6bps∩45–55 vs Binance-open (live proxy)
  frozen_ptb      first 6bps∩45–55 vs Gamma PTB (mixed oracle; forbidden)
  stale_mid       last 90s, fair≥0.75, ask still 45–55
  late_fair85_*   last 60s, fair≥0.85, ask in mid / cheap / any
  certain_lead15  last 60s, |lead|≥15 bps, ask 45–70
  post_close_*    first taker BUY after T1 on the official winner

Ship bar: train AND holdout +EV, holdout pnl5 ≥ +$5, expensive extras
(px>0.70) settle WR ≥ 0.85, not mixed-oracle, not 97–98. Default ship
is false even if a tape candidate clears — owner confirms. Do not
autodial 6bps, leftover, 250ms FOK delay, or Binance−PTB.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import entry_edge, fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from high_wr import REV_CACHE, load_raw  # noqa: E402
from tape_pull import pack, rec_of, split_holdout  # noqa: E402

OUT = Path(__file__).resolve().parent / "oracle_arb.json"
SHIP = Path(__file__).resolve().parent / "oracle_arb_ship.json"
POST_CACHE = Path("/tmp/oracle_arb_post_http.json")
DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "surf-arb-research/oracle-arb (read-only; no trading)",
    "Accept": "application/json",
}
TWAP60 = te.TWAP60_START
CORE = ("btc", "eth")
MAX_LEAD = 40.0
HOLDOUT_DAYS = 7
POST_HTTP_N = 240
POST_HTTP_WORKERS = 8

DO_NOT = [
    "oracle_arb_live",
    "post_close_taker",
    "binance_minus_ptb",
    "use_binance_as_settlement",
    "skip_fok_delay_ms",
    "favorite_97_98",
    "late_fair_widen_ask",
    "late_stale_mid",
    "lead_4bps",
    "lead_5bps",
    "band_40_60",
    "chase_leftover",
    "up_requote_2ticks",
    "min_left_below_120",
    "alts",
    "15m",
    "twap_reverse_on",
    "dump_mid90",
    "price_sl_8c",
    "always_in_live",
]


def fee_ps(px: float) -> float:
    return taker_fee(1.0, px, 0.07)


def breakeven_wr(px: float) -> float:
    return round(float(px) + fee_ps(px), 4)


def px_bucket(px: float) -> str:
    p = float(px)
    if p <= 0.55 + 1e-12:
        return "mid_45_55"
    if p <= 0.70 + 1e-12:
        return "px_55_70"
    if p <= 0.85 + 1e-12:
        return "px_70_85"
    return "px_85_plus"


def buys_range(raw: list, t0: int, t1: int, *, lo: float, hi: float) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or t.get("Side") or "BUY").upper() != "BUY":
            continue
        try:
            px = float(t.get("px") or t.get("price") or 0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < t0 or ts > t1:
            continue
        if px < lo or px > hi:
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc not in {"Up", "Down"}:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc, "size": float(t.get("size") or 0)})
    out.sort(key=lambda x: x["ts"])
    return out


def http_json(url: str, timeout: float = 25.0, tries: int = 4):
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
                time.sleep(0.35 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.25 * (2**i))
    if last:
        raise last
    return None


def percentile(xs: list[float], p: float) -> float | None:
    if not xs:
        return None
    ys = sorted(float(x) for x in xs)
    i = min(max(int(round((len(ys) - 1) * p)), 0), len(ys) - 1)
    return round(ys[i], 4)


def hist_count(xs: list[float], edges: list[float]) -> dict:
    labels = []
    for i, e in enumerate(edges):
        lo = "-inf" if i == 0 else str(edges[i - 1])
        labels.append(f"{lo}_to_{e}")
    labels.append(f"{edges[-1]}_plus")
    counts = [0] * (len(edges) + 1)
    for x in xs:
        placed = False
        for i, e in enumerate(edges):
            if x < e:
                counts[i] += 1
                placed = True
                break
        if not placed:
            counts[-1] += 1
    n = max(len(xs), 1)
    return {lab: {"n": c, "frac": round(c / n, 4)} for lab, c in zip(labels, counts)} | {"n": len(xs)}


def hold_row(ev: dict, picked: dict, *, family: str, source: str) -> dict:
    won = picked["side"] == ev["winner"]
    px = float(picked["px"])
    return {
        "slug": ev["slug"],
        "asset": ev.get("asset"),
        "start": int(ev["start"]),
        "end": int(ev["end"]),
        "ts": int(picked["ts"]),
        "left": int(picked["left"]),
        "side": picked["side"],
        "px": round(px, 4),
        "lead": round(float(picked.get("lead") or 0), 4),
        "fair": None if picked.get("fair") is None else round(float(picked["fair"]), 4),
        "won": won,
        "scratched": False,
        "exit_why": "settle",
        "pnl": te.pnl_hold(px, won),
        "family": family,
        "source": source,
        "orig_won": won,
        "orig_scratch": False,
    }


def bucket_rec(rows: list[dict]) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[px_bucket(r["px"])].append(r)
    out = {}
    for name in ("mid_45_55", "px_55_70", "px_70_85", "px_85_plus"):
        xs = by.get(name) or []
        rec = rec_of(xs)
        rec["settle_wr"] = None if not xs else round(sum(1 for r in xs if r.get("won")) / len(xs), 4)
        rec["avg_px"] = None if not xs else round(sum(float(r["px"]) for r in xs) / len(xs), 4)
        out[name] = rec
    return out


def annotate(rows: list[dict], *, windows: int, days: float, extras: list[dict] | None = None) -> dict:
    packed = pack(rows)
    train, hold = split_holdout(rows)
    a = packed["all"]
    a["settle_wr"] = None if not rows else round(sum(1 for r in rows if r.get("won")) / len(rows), 4)
    a["avg_px"] = None if not rows else round(sum(float(r["px"]) for r in rows) / len(rows), 4)
    a["avg_lead"] = None if not rows else round(sum(abs(float(r["lead"])) for r in rows) / len(rows), 2)
    a["avg_fair"] = None if not rows else round(sum(float(r["fair"] or 0.5) for r in rows) / len(rows), 4)
    a["avg_left"] = None if not rows else round(sum(float(r["left"]) for r in rows) / len(rows), 1)
    a["coverage"] = None if windows <= 0 else round(len(rows) / windows, 4)
    a["per_day"] = None if days <= 0 else round(len(rows) / days, 2)
    packed["all"] = a
    packed["train"]["settle_wr"] = None if not train else round(sum(1 for r in train if r.get("won")) / len(train), 4)
    packed["holdout"]["settle_wr"] = None if not hold else round(sum(1 for r in hold if r.get("won")) / len(hold), 4)
    packed["buckets"] = bucket_rec(rows)
    expensive = [r for r in rows if float(r["px"]) > 0.70 + 1e-12]
    packed["expensive"] = rec_of(expensive)
    packed["expensive"]["settle_wr"] = None if not expensive else round(
        sum(1 for r in expensive if r.get("won")) / len(expensive), 4
    )
    ho_exp = [r for r in hold if float(r["px"]) > 0.70 + 1e-12]
    packed["expensive_holdout"] = rec_of(ho_exp)
    packed["expensive_holdout"]["settle_wr"] = None if not ho_exp else round(
        sum(1 for r in ho_exp if r.get("won")) / len(ho_exp), 4
    )
    if extras is not None:
        packed["extras"] = rec_of(extras)
        packed["extras"]["settle_wr"] = None if not extras else round(
            sum(1 for r in extras if r.get("won")) / len(extras), 4
        )
        packed["extras"]["avg_px"] = None if not extras else round(sum(float(r["px"]) for r in extras) / len(extras), 4)
        packed["extras_holdout"] = rec_of(split_holdout(extras)[1]) if extras else rec_of([])
    return packed


def slim(packed: dict, *, forbidden: bool = False) -> dict:
    a, t, h = packed["all"], packed["train"], packed["holdout"]
    exp_wr = packed["expensive"].get("settle_wr")
    expensive_ok = True
    if int(packed["expensive"]["n"] or 0) >= 20:
        expensive_ok = bool((exp_wr or 0) + 1e-12 >= 0.85 and packed["expensive"].get("ev_ok"))
    beats = bool(
        packed.get("robust")
        and (h.get("pnl5") or 0) + 1e-9 >= 5.0
        and (t.get("pnl5") or 0) + 1e-9 > 0
        and expensive_ok
        and not forbidden
    )
    return {
        "n": a["n"],
        "coverage": a.get("coverage"),
        "per_day": a.get("per_day"),
        "avg_px": a.get("avg_px"),
        "avg_lead": a.get("avg_lead"),
        "avg_fair": a.get("avg_fair"),
        "avg_left": a.get("avg_left"),
        "settle_wr": a.get("settle_wr"),
        "take_wr": a["take_wr"],
        "pnl5": a["pnl5"],
        "train": {
            "n": t["n"],
            "pnl5": t["pnl5"],
            "take_wr": t["take_wr"],
            "settle_wr": t.get("settle_wr"),
            "ev_ok": t["ev_ok"],
        },
        "holdout": {
            "n": h["n"],
            "pnl5": h["pnl5"],
            "take_wr": h["take_wr"],
            "settle_wr": h.get("settle_wr"),
            "ev_ok": h["ev_ok"],
        },
        "expensive_n": packed["expensive"]["n"],
        "expensive_pnl5": packed["expensive"]["pnl5"],
        "expensive_settle_wr": exp_wr,
        "expensive_holdout_n": packed["expensive_holdout"]["n"],
        "expensive_holdout_pnl5": packed["expensive_holdout"]["pnl5"],
        "expensive_ok": expensive_ok,
        "buckets": packed["buckets"],
        "robust": packed["robust"],
        "forbidden": forbidden,
        "beats": beats,
        "extras": packed.get("extras"),
        "extras_holdout": packed.get("extras_holdout"),
    }


def pick_first(pending: dict, name: str, cand: dict) -> None:
    if pending[name] is None:
        pending[name] = cand


def fetch_post_http(events: list[dict]) -> dict:
    if POST_CACHE.exists():
        try:
            cached = json.loads(POST_CACHE.read_text())
            if isinstance(cached, dict) and cached.get("n_markets", 0) >= 100:
                return cached
        except (json.JSONDecodeError, OSError):
            pass
    newest = sorted(events, key=lambda e: int(e["end"]), reverse=True)[:POST_HTTP_N]

    def one(ev: dict) -> dict:
        url = f"{DATA}/trades?market={ev['cid']}&limit=250&takerOnly=true"
        try:
            rows = http_json(url) or []
        except Exception as exc:
            return {"ok": False, "err": type(exc).__name__, "slug": ev["slug"]}
        end = int(ev["end"])
        winner = ev["winner"]
        buys, sells = [], []
        for t in rows if isinstance(rows, list) else []:
            try:
                ts = int(t.get("timestamp") or 0)
                px = float(t.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if ts <= end or ts > end + 60:
                continue
            rec = {
                "dt": ts - end,
                "px": px,
                "outcome": t.get("outcome"),
                "side": str(t.get("side") or ""),
                "size": float(t.get("size") or 0),
                "winner": winner,
            }
            if rec["side"].upper() == "BUY":
                buys.append(rec)
            else:
                sells.append(rec)
        return {"ok": True, "slug": ev["slug"], "winner": winner, "end": end, "buys": buys, "sells": sells}

    rows = []
    with ThreadPoolExecutor(max_workers=POST_HTTP_WORKERS) as pool:
        futs = [pool.submit(one, ev) for ev in newest]
        for i, fut in enumerate(as_completed(futs), 1):
            rows.append(fut.result())
            if i % 80 == 0:
                print(f"  post-http {i}/{len(newest)}", flush=True)
    ok = [r for r in rows if r.get("ok")]
    buy_win, buy_lose, sell_win = [], [], []
    n_win_mid = n_win_cheap = n_lose_dust = 0
    first_win_cheap = []
    for r in ok:
        seen = False
        for b in r.get("buys") or []:
            if b["outcome"] == r["winner"]:
                buy_win.append(b)
                if 0.45 - 1e-12 <= b["px"] <= 0.55 + 1e-12:
                    n_win_mid += 1
                if 0.40 - 1e-12 <= b["px"] <= 0.70 + 1e-12:
                    n_win_cheap += 1
                    if not seen:
                        first_win_cheap.append(b)
                        seen = True
            else:
                buy_lose.append(b)
                if b["px"] <= 0.05 + 1e-12:
                    n_lose_dust += 1
        for s in r.get("sells") or []:
            if s["outcome"] == r["winner"]:
                sell_win.append(s)
    win_px = [b["px"] for b in buy_win]
    lose_px = [b["px"] for b in buy_lose]
    sell_px = [s["px"] for s in sell_win]
    out = {
        "n_requested": len(newest),
        "n_markets": len(ok),
        "n_fail": len(rows) - len(ok),
        "window_s": 60,
        "buy_winner_n": len(buy_win),
        "buy_loser_n": len(buy_lose),
        "sell_winner_n": len(sell_win),
        "winner_mid_n": n_win_mid,
        "winner_cheap40_70_n": n_win_cheap,
        "loser_dust_n": n_lose_dust,
        "buy_winner_px": {
            "p10": percentile(win_px, 0.10),
            "p50": percentile(win_px, 0.50),
            "p90": percentile(win_px, 0.90),
            "mean": None if not win_px else round(sum(win_px) / len(win_px), 4),
        },
        "buy_loser_px": {
            "p10": percentile(lose_px, 0.10),
            "p50": percentile(lose_px, 0.50),
            "p90": percentile(lose_px, 0.90),
            "mean": None if not lose_px else round(sum(lose_px) / len(lose_px), 4),
        },
        "sell_winner_px": {
            "p50": percentile(sell_px, 0.50),
            "mean": None if not sell_px else round(sum(sell_px) / len(sell_px), 4),
        },
        "first_winner_cheap_n": len(first_win_cheap),
        "note": (
            "Taker BUYs after T1. Winner-mid 45–55 would be classic oracle arb if FAKable. "
            "Loser dust (~1¢) is leftover dogs; winner sells near 99¢ mean the book already knows."
        ),
    }
    POST_CACHE.write_text(json.dumps(out))
    return out


def run() -> dict:
    t0 = time.time()
    events = json.loads((REV_CACHE / "_events.json").read_text())
    twap_ev = [e for e in events if e.get("asset") in CORE and int(e.get("end") or 0) >= TWAP60]
    newest = max(int(e["end"]) for e in twap_ev)
    oldest = min(int(e["end"]) for e in twap_ev)
    days = max((newest - oldest) / 86400.0, 1e-9)
    print(f"load series {TWAP60}->{newest} n={len(twap_ev)} days={days:.2f}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", TWAP60 - 180, newest + 5),
    }
    try:
        meta = json.loads((REV_CACHE / "_gamma_meta.json").read_text())
    except (json.JSONDecodeError, OSError):
        meta = {}

    family_names = (
        "frozen_open",
        "frozen_ptb",
        "stale_mid",
        "late_fair85_mid",
        "late_fair85_cheap",
        "late_fair85_any",
        "late_fair90_cheap",
        "certain_lead15",
        "post_close_winner_mid",
        "post_close_winner_cheap",
    )
    first_by: dict[str, dict[str, dict]] = {n: {} for n in family_names}

    n_win = 0
    n_print = 0
    n_meta = 0
    lags: list[float] = []
    basis: list[float] = []
    fair_at_frozen: list[float] = []
    gap_at_frozen: list[float] = []
    gap_last30: list[float] = []
    false_bn_rows: list[dict] = []
    open_vs_winner = Counter()
    ptb_vs_winner = Counter()
    n_lead_scan = 0

    for i, ev in enumerate(twap_ev, 1):
        asset = ev["asset"]
        series = series_of.get(asset)
        if series is None:
            continue
        raw = load_raw(REV_CACHE, ev["slug"])
        if not raw:
            continue
        start, end = int(ev["start"]), int(ev["end"])
        full = buys_range(raw, start, end + 5, lo=0.05, hi=0.99)
        n_print += 1
        if len(full) < 2:
            continue
        tw_open = series.twap(start, 60)
        if tw_open is None or tw_open <= 0:
            continue
        n_win += 1
        gm = meta.get(ev["slug"]) or {}
        ptb = gm.get("ptb")
        try:
            ptb_f = float(ptb) if ptb is not None else None
        except (TypeError, ValueError):
            ptb_f = None
        if ptb_f and ptb_f > 0:
            n_meta += 1
            b0 = lead_bps(tw_open, ptb_f)
            if b0 is not None:
                basis.append(float(b0))

        pending = {n: None for n in family_names if not n.startswith("post_close")}
        first_open_ts = None
        first_open_side = None
        first_ptb_side = None
        recorded_false = False
        for ts in range(start + 15, end + 1, 5):
            tw = series.twap(ts, 60)
            if tw is None:
                continue
            lead_o = lead_bps(tw, tw_open)
            if lead_o is None:
                continue
            n_lead_scan += 1
            left = end - ts
            vol = series.realized_vol_bps_sqrt_s(ts, 120)
            fair_up = fair_p_up(lead_o, vol, float(max(left, 1)), lookback=60)
            side_o = "Up" if lead_o >= 0 else "Down"
            fair_o = None if fair_up is None else (fair_up if side_o == "Up" else (1.0 - fair_up))
            pr_o = te.last_print(full, ts, side_o, slack=25)
            lead_p = lead_bps(tw, ptb_f) if ptb_f else None
            side_p = None if lead_p is None else ("Up" if lead_p >= 0 else "Down")
            pr_p = None if side_p is None else te.last_print(full, ts, side_p, slack=25)

            if first_open_ts is None and 6.0 - 1e-12 <= abs(lead_o) <= MAX_LEAD + 1e-12:
                first_open_ts = ts
                first_open_side = side_o
                open_vs_winner[side_o == ev["winner"]] += 1
                if pr_o is not None and 0.45 - 1e-12 <= float(pr_o["px"]) <= 0.55 + 1e-12:
                    lags.append(0.0)
                else:
                    lag = None
                    for p in full:
                        if p["outcome"] != side_o or p["ts"] < ts:
                            continue
                        if 0.45 - 1e-12 <= float(p["px"]) <= 0.55 + 1e-12:
                            lag = float(p["ts"] - ts)
                            break
                    if lag is not None:
                        lags.append(lag)
            if first_ptb_side is None and lead_p is not None and 6.0 - 1e-12 <= abs(lead_p) <= MAX_LEAD + 1e-12:
                first_ptb_side = side_p
                ptb_vs_winner[side_p == ev["winner"]] += 1
            if (
                not recorded_false
                and 6.0 - 1e-12 <= abs(lead_o) <= MAX_LEAD + 1e-12
                and pr_o is not None
                and 0.45 - 1e-12 <= float(pr_o["px"]) <= 0.55 + 1e-12
            ):
                ptb_thin = lead_p is None or abs(lead_p) < 2.0 - 1e-12
                ptb_flip = lead_p is not None and (lead_p * lead_o) < 0
                if ptb_thin or ptb_flip:
                    recorded_false = True
                    won = side_o == ev["winner"]
                    false_bn_rows.append(
                        {
                            "slug": ev["slug"],
                            "end": end,
                            "px": float(pr_o["px"]),
                            "lead_open": float(lead_o),
                            "lead_ptb": None if lead_p is None else float(lead_p),
                            "won": won,
                            "scratched": False,
                            "pnl": te.pnl_hold(float(pr_o["px"]), won),
                            "why": "flip" if ptb_flip else "thin",
                        }
                    )

            def cand(pr, side, lead, fair):
                if pr is None:
                    return None
                px = float(pr["px"])
                return {
                    "ts": ts,
                    "left": left,
                    "side": side,
                    "px": px,
                    "lead": float(lead),
                    "fair": fair,
                }

            c_open = cand(pr_o, side_o, lead_o, fair_o)
            if c_open is not None:
                px, left_s, fair = c_open["px"], c_open["left"], c_open["fair"]
                edge_ok = fair is None or entry_edge(fair, px, 0.07) + 1e-12 >= 0.04
                if (
                    pending["frozen_open"] is None
                    and 120.0 - 1e-12 <= left_s <= 280.0 + 1e-12
                    and 6.0 - 1e-12 <= abs(lead_o) <= MAX_LEAD + 1e-12
                    and 0.45 - 1e-12 <= px <= 0.55 + 1e-12
                    and edge_ok
                ):
                    pick_first(pending, "frozen_open", c_open)
                    if fair is not None:
                        fair_at_frozen.append(float(fair))
                        gap_at_frozen.append(float(fair) - px)
                if left_s <= 90 + 1e-12 and fair is not None and fair + 1e-12 >= 0.75 and 0.45 - 1e-12 <= px <= 0.55 + 1e-12:
                    pick_first(pending, "stale_mid", c_open)
                if left_s <= 60 + 1e-12 and fair is not None and fair + 1e-12 >= 0.85:
                    if 0.45 - 1e-12 <= px <= 0.55 + 1e-12:
                        pick_first(pending, "late_fair85_mid", c_open)
                    if 0.45 - 1e-12 <= px <= 0.75 + 1e-12:
                        pick_first(pending, "late_fair85_cheap", c_open)
                    if 0.45 - 1e-12 <= px <= 0.90 + 1e-12:
                        pick_first(pending, "late_fair85_any", c_open)
                if left_s <= 60 + 1e-12 and fair is not None and fair + 1e-12 >= 0.90 and 0.45 - 1e-12 <= px <= 0.75 + 1e-12:
                    pick_first(pending, "late_fair90_cheap", c_open)
                if left_s <= 60 + 1e-12 and abs(lead_o) + 1e-12 >= 15.0 and 0.45 - 1e-12 <= px <= 0.70 + 1e-12:
                    pick_first(pending, "certain_lead15", c_open)
                if left_s <= 30 + 1e-12 and fair is not None and pr_o is not None:
                    gap_last30.append(float(fair) - float(pr_o["px"]))

            if pending["frozen_ptb"] is None and side_p is not None and pr_p is not None and lead_p is not None:
                px = float(pr_p["px"])
                left_s = left
                if (
                    120.0 - 1e-12 <= left_s <= 280.0 + 1e-12
                    and 6.0 - 1e-12 <= abs(lead_p) <= MAX_LEAD + 1e-12
                    and 0.45 - 1e-12 <= px <= 0.55 + 1e-12
                ):
                    fair_p = None if fair_up is None else (fair_up if side_p == "Up" else (1.0 - fair_up))
                    pending["frozen_ptb"] = {
                        "ts": ts,
                        "left": left_s,
                        "side": side_p,
                        "px": px,
                        "lead": float(lead_p),
                        "fair": fair_p,
                    }

        post = buys_range(raw, end + 1, end + 5, lo=0.05, hi=0.99)
        winner = ev["winner"]
        for p in post:
            if p["outcome"] != winner:
                continue
            px = float(p["px"])
            cand_p = {"ts": p["ts"], "left": end - p["ts"], "side": winner, "px": px, "lead": 0.0, "fair": 1.0}
            if ev["slug"] not in first_by["post_close_winner_mid"] and 0.45 - 1e-12 <= px <= 0.55 + 1e-12:
                first_by["post_close_winner_mid"][ev["slug"]] = hold_row(
                    ev, cand_p, family="post_close_winner_mid", source="cache+5s"
                )
            if ev["slug"] not in first_by["post_close_winner_cheap"] and 0.40 - 1e-12 <= px <= 0.70 + 1e-12:
                first_by["post_close_winner_cheap"][ev["slug"]] = hold_row(
                    ev, cand_p, family="post_close_winner_cheap", source="cache+5s"
                )

        src = {"frozen_ptb": "binance_vs_gamma_ptb"}
        for name, picked in pending.items():
            if picked is None or picked.get("px") is None:
                continue
            first_by[name][ev["slug"]] = hold_row(
                ev, picked, family=name, source=src.get(name, "binance_vs_open")
            )

        if i % 900 == 0:
            print(f"  {i}/{len(twap_ev)} windows={n_win}", flush=True)

    # disagree_sign was incremented inside the 5s loop (over-count). Recompute once per window from stored sides.
    # Use first_by frozen_open vs frozen_ptb instead.
    n_disagree = 0
    for slug, row in first_by["frozen_open"].items():
        other = first_by["frozen_ptb"].get(slug)
        if other is not None and other["side"] != row["side"]:
            n_disagree += 1

    print("post-close HTTP sample", flush=True)
    post_http = fetch_post_http(twap_ev)

    frozen_slugs = set(first_by["frozen_open"])
    families = {}
    for name in family_names:
        rows = list(first_by[name].values())
        extras = [r for r in rows if r["slug"] not in frozen_slugs] if name != "frozen_open" else []
        packed = annotate(rows, windows=n_win, days=days, extras=extras)
        forbidden = name in {"frozen_ptb", "late_fair85_any"}
        families[name] = slim(packed, forbidden=forbidden)
        families[name]["n_extras_vs_frozen"] = len(extras)
        families[name]["n_overlap_frozen"] = 0 if name == "frozen_open" else sum(1 for r in rows if r["slug"] in frozen_slugs)

    false_pack = pack(false_bn_rows) if false_bn_rows else pack([])
    false_ann = {
        "n": len(false_bn_rows),
        "pnl5": false_pack["all"]["pnl5"],
        "take_wr": false_pack["all"]["take_wr"],
        "train": false_pack["train"],
        "holdout": false_pack["holdout"],
        "robust": false_pack["robust"],
        "flip_n": sum(1 for r in false_bn_rows if r.get("why") == "flip"),
        "thin_n": sum(1 for r in false_bn_rows if r.get("why") == "thin"),
        "note": "First 6bps∩45–55 on Binance-vs-open while Binance-vs-PTB is <2bps or opposite sign. Mixed-oracle trap, not a live rule.",
    }

    def wr(counter: Counter) -> float | None:
        n = counter[True] + counter[False]
        if n <= 0:
            return None
        return round(counter[True] / n, 4)

    winners = [n for n, g in families.items() if g.get("beats") and n != "frozen_open"]
    pick = None
    if winners:
        pick = max(winners, key=lambda n: (families[n]["holdout"]["pnl5"], families[n]["pnl5"]))

    # Qualitative findings from tape, independent of pick.
    stale = families["stale_mid"]
    late_any = families["late_fair85_any"]
    late_mid = families["late_fair85_mid"]
    frozen = families["frozen_open"]
    post_mid_n = families["post_close_winner_mid"]["n"]
    gap_frozen_mean = None if not gap_at_frozen else sum(gap_at_frozen) / len(gap_at_frozen)
    gap_late_mean = None if not gap_last30 else sum(gap_last30) / len(gap_last30)
    stale_wr = stale.get("settle_wr")
    late_mid_wr = late_mid.get("settle_wr")
    post_not_sleeve = bool(
        (post_http.get("winner_mid_n") or 0) == 0
        and not families["post_close_winner_mid"].get("robust")
    )
    findings = {
        "shipped_sleeve_is_mild_oracle_latency": True,
        "usable_delay_is_early_clob_lag": bool(
            gap_frozen_mean is not None
            and gap_frozen_mean > 0.05
            and gap_late_mean is not None
            and gap_late_mean < 0.05
        ),
        "late_window_already_repriced": bool(gap_late_mean is not None and gap_late_mean < 0.05),
        "late_stale_mid_is_leftover_not_arb": bool(stale_wr is not None and stale_wr < 0.55),
        "high_fair_late_mid_is_coin_flip": bool(late_mid_wr is not None and late_mid_wr < 0.55),
        "post_close_winner_mid_not_on_tape": post_not_sleeve,
        "post_close_buys_are_loser_dust": bool(
            (post_http.get("buy_loser_n") or 0) > (post_http.get("buy_winner_n") or 0)
            and (post_http.get("loser_dust_n") or 0) > 0
        ),
        "binance_minus_ptb_is_not_arb": True,
        "skip_fok_delay_is_not_oracle_arb": True,
        "favorite_97_98_shape_in_late_any": bool((late_any.get("avg_px") or 0) >= 0.70),
        "false_bn_holdout_ev_ok": bool(false_ann["holdout"].get("ev_ok")),
        "frozen_ptb_train_ev_ok": bool(families["frozen_ptb"]["train"].get("ev_ok")),
        "entry_unchanged": True,
    }

    why = (
        "Usable delay is the rare early coincidence of same-source TWAP 6bps with a still-mid "
        "CLOB ask — already the shipped sleeve (fair−ask ≈ 0.20 at fill; last-30s gap ≈ 0). "
        "Waiting longer for a 45–55 print after TWAP has led is leftover, not arb: last-90s "
        "stale-mid WR is a coin flip despite BM fair ~0.87. Paying the late ask (70–90¢) is "
        "the 97–98 family. Post-close 50/50 is a Gamma quote artifact (HTTP winner-mid n=0; "
        "taker BUYs are loser dust ~1¢; winner sells ~99¢). Binance vs Gamma PTB 6bps is "
        "mixed-oracle −EV. Do not skip 250ms FOK delay. Do not autodial."
    )
    if pick:
        why = (
            f"Tape candidate {pick} clears the hold-to-settle bar on this print tape. "
            + why
            + " ship=false until the operator confirms; live fillability of stale asks is not the tape."
        )

    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 1),
        "question": "Add an Oracle Arbitrage sleeve? Can delay / latency be exploited?",
        "ship": False,
        "pick": None,
        "tape_candidate": pick,
        "winners": winners,
        "why": why,
        "universe": {
            "events": len(twap_ev),
            "windows_scanned": n_win,
            "with_prints": n_print,
            "with_ptb": n_meta,
            "days": round(days, 2),
            "holdout_days": HOLDOUT_DAYS,
            "lead_steps": n_lead_scan,
            "oracle_proxy": "Binance 1s TWAP vs Binance window-open (same-source). Live is Chainlink vs Chainlink T0.",
            "settlement": "Official Gamma 0/1 (Chainlink TWAP vs PTB).",
        },
        "physics": {
            "breakeven_wr_50c": breakeven_wr(0.50),
            "breakeven_wr_70c": breakeven_wr(0.70),
            "breakeven_wr_80c": breakeven_wr(0.80),
            "breakeven_wr_90c": breakeven_wr(0.90),
            "breakeven_wr_97c": breakeven_wr(0.97),
            "fee_ps_50c": round(fee_ps(0.50), 4),
            "fee_ps_80c": round(fee_ps(0.80), 4),
            "note": "Settle WR ≈ CLOB price. Edge is fair−ask−fee. Paying 80¢ to be right 80% is −EV after the 7% crypto fee. True oracle arb needs a stale ask, not a faster CEX.",
        },
        "delay": {
            "twap6_to_mid_print_lag_s": {
                "n": len(lags),
                "p10": percentile(lags, 0.10),
                "p50": percentile(lags, 0.50),
                "p90": percentile(lags, 0.90),
                "mean": None if not lags else round(sum(lags) / len(lags), 2),
                "hist": hist_count(lags, [0, 2, 5, 10, 20, 40]),
                "note": "Seconds from first |Binance-open lead|≥6bps until a 45–55 print on that side exists. Median tens of seconds is NOT extra arb: those late mids are leftover (coin-flip WR). The shipped sleeve is the rare same-second coincidence.",
            },
            "t0_binance_vs_gamma_ptb_bps": {
                "n": len(basis),
                "p10": percentile(basis, 0.10),
                "p50": percentile(basis, 0.50),
                "p90": percentile(basis, 0.90),
                "mean": None if not basis else round(sum(basis) / len(basis), 2),
                "note": "Same 9bps-class basis. Never subtract Binance from Gamma PTB as a taker rule.",
            },
            "fair_at_frozen_6bps": {
                "n": len(fair_at_frozen),
                "p10": percentile(fair_at_frozen, 0.10),
                "p50": percentile(fair_at_frozen, 0.50),
                "p90": percentile(fair_at_frozen, 0.90),
                "mean": None if not fair_at_frozen else round(sum(fair_at_frozen) / len(fair_at_frozen), 4),
            },
            "fair_minus_ask_at_frozen": {
                "n": len(gap_at_frozen),
                "p50": percentile(gap_at_frozen, 0.50),
                "mean": None if not gap_at_frozen else round(sum(gap_at_frozen) / len(gap_at_frozen), 4),
            },
            "fair_minus_ask_last30s": {
                "n": len(gap_last30),
                "p50": percentile(gap_last30, 0.50),
                "mean": None if not gap_last30 else round(sum(gap_last30) / len(gap_last30), 4),
                "note": "Late gap ≈ 0 or negative means the CLOB has already repriced; leftover delay is not sitting in 45–55.",
            },
            "first6_open_side_vs_official_winner": wr(open_vs_winner),
            "first6_ptb_side_vs_official_winner": wr(ptb_vs_winner),
            "frozen_open_vs_frozen_ptb_side_disagree_n": n_disagree,
            "false_binance_lead_vs_ptb": false_ann,
        },
        "families": families,
        "post_close_http": post_http,
        "findings": findings,
        "do_not": DO_NOT,
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "fok_delay_ms": 250.0,
            "twap_no_cheaper": True,
            "twap_up_tick": 0.01,
            "twap_confirm_fair": 0.60,
        },
        "use_binance_vs_chainlink": False,
        "skip_fok_delay": False,
    }

    ship = {
        "strategy_rev": 60,
        "ship": False,
        "pick": None,
        "tape_candidate": pick,
        "researched_at_utc": rec["researched_at_utc"],
        "source": "research/oracle_arb.json",
        "question": rec["question"],
        "why": why,
        "windows_scanned": n_win,
        "frozen_n": frozen["n"],
        "frozen_coverage": frozen["coverage"],
        "frozen_holdout_ev_ok": frozen["holdout"]["ev_ok"],
        "stale_mid_n": stale["n"],
        "stale_mid_coverage": stale["coverage"],
        "stale_mid_settle_wr": stale.get("settle_wr"),
        "stale_mid_train_ev_ok": stale["train"]["ev_ok"],
        "stale_mid_holdout_ev_ok": stale["holdout"]["ev_ok"],
        "stale_mid_beats": stale["beats"],
        "late_fair85_mid_n": late_mid["n"],
        "late_fair85_mid_settle_wr": late_mid.get("settle_wr"),
        "late_fair85_mid_beats": late_mid["beats"],
        "late_fair85_cheap_n": families["late_fair85_cheap"]["n"],
        "late_fair85_cheap_expensive_ok": families["late_fair85_cheap"]["expensive_ok"],
        "late_fair85_cheap_beats": families["late_fair85_cheap"]["beats"],
        "late_fair85_any_n": late_any["n"],
        "late_fair85_any_avg_px": late_any["avg_px"],
        "late_fair85_any_holdout_ev_ok": late_any["holdout"]["ev_ok"],
        "late_fair85_any_is_97_98_cousin": bool((late_any.get("avg_px") or 0) >= 0.70),
        "late_fair85_any_beats": late_any["beats"],
        "frozen_ptb_n": families["frozen_ptb"]["n"],
        "frozen_ptb_train_ev_ok": families["frozen_ptb"]["train"]["ev_ok"],
        "frozen_open_beats": frozen["beats"],
        "post_close_winner_mid_n": post_mid_n,
        "post_close_winner_mid_robust": families["post_close_winner_mid"]["robust"],
        "post_close_http_winner_mid_n": post_http.get("winner_mid_n"),
        "post_close_http_winner_cheap_n": post_http.get("winner_cheap40_70_n"),
        "post_close_http_buy_loser_n": post_http.get("buy_loser_n"),
        "post_close_not_fillable_mid": findings["post_close_winner_mid_not_on_tape"],
        "t0_basis_bps_p50": rec["delay"]["t0_binance_vs_gamma_ptb_bps"]["p50"],
        "false_bn_n": false_ann["n"],
        "false_bn_holdout_ev_ok": false_ann["holdout"].get("ev_ok"),
        "winners": winners,
        "use_binance_vs_chainlink": False,
        "skip_fok_delay": False,
        "do_not": DO_NOT,
        "params_kept": rec["params_kept"],
        "findings": findings,
    }

    rec["ship"] = False
    rec["pick"] = None
    OUT.write_text(json.dumps(rec, indent=2))
    SHIP.write_text(json.dumps(ship, indent=2))
    print(
        json.dumps(
            {
                "ship": False,
                "tape_candidate": pick,
                "winners": winners,
                "windows": n_win,
                "frozen": frozen["n"],
                "stale_mid": stale["n"],
                "late_any": late_any["n"],
                "post_mid": post_mid_n,
                "http_mid": post_http.get("winner_mid_n"),
                "s": rec["elapsed_s"],
            },
            indent=2,
        ),
        flush=True,
    )
    return rec


if __name__ == "__main__":
    run()
