#!/usr/bin/env python3
"""Per-asset vol and calibrated P(win) — offline, train/holdout, no live patch.

Hypothesis: live alt holds printed fair 0.74–0.80 then settled $0 because the
BM fair_p used a too-quiet 120s vol (or ignored jumps). This script:

1. Replays BTC+ETH CLOB last-take under Rev 47/48 entry (min_left 120, no late
   45¢, lead cap 40) and records vol/z/fair vs held-to-settle wins.
2. Reliability diagrams + min_fair / min_z / vol-scale / quiet-vol skip grids.
   A rule must be +EV on train AND holdout and not lose holdout PnL vs baseline.
3. Downloads a short Binance 1s window for SOL/XRP/DOGE/BNB and compares 120s
   realized vol vs BTC (oracle path, no CLOB). Same BM fair vs actual TWAP
   winner — this is the alt miscalibration test without waiting for a 30d tape.
4. Scores post-Rev47 live fills that stored fair_p (tiny n, not a ship signal).

Proxy: Binance 1s TWAP vs Binance T0. Live = Chainlink vs Chainlink T0.
Do not ship a holdout-only or live-n=15 rule.
"""

from __future__ import annotations

import json
import math
import os
import sys
import urllib.error
import urllib.request
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
    settlement_tau,
    should_scratch,
)
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
import twap_freq as tf  # noqa: E402

OUT = Path(__file__).with_name("calibrate_fair.json")
LIVE_FILLS = Path(os.environ.get("LIVE_FILLS_JSON", "/tmp/live_fills_cal.json"))
ALT_SYMBOL = {
    "sol": "SOLUSDT",
    "xrp": "XRPUSDT",
    "doge": "DOGEUSDT",
    "bnb": "BNBUSDT",
    "hype": "HYPEUSDT",
}
CORE = ("btc", "eth")
VISION_ZIP = "https://data.binance.vision/data/spot/daily/klines"
UA = {"User-Agent": "Mozilla/5.0 surf-arb-research/calibrate-fair"}
RESCORE = te.RESCORE
NOTIONAL = te.NOTIONAL
MAX_LEAD = 40.0
LATE_LEFT = 180.0
LATE_PX = 0.50


def simulate_last(ev, series, prints, params: TwapParams, *, vol_scale: float = 1.0) -> dict | None:
    start, end = int(ev["start"]), int(ev["end"])
    winner = ev["winner"]
    tw_open = series.twap(start, params.lookback)
    if tw_open is None or tw_open <= 0:
        return None
    t0 = start + 15
    t1 = end - int(params.min_left)
    picked = None
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
        pr = te.last_print(prints, ts, side, slack=25)
        if pr is None:
            continue
        if not (params.min_price - 1e-12 <= pr["px"] <= params.max_price + 1e-12):
            continue
        if left < LATE_LEFT and pr["px"] + 1e-12 < LATE_PX:
            continue
        raw_vol = series.realized_vol_bps_sqrt_s(ts, 120)
        if raw_vol is None:
            continue
        vol = raw_vol * float(vol_scale)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) < params.min_edge:
            continue
        z = lead_z(lead if side == "Up" else -lead, vol, float(left), lookback=params.lookback)
        tau = settlement_tau(float(left), params.lookback)
        picked = {
            "ts": ts,
            "left": left,
            "side": side,
            "px": pr["px"],
            "lead": lead,
            "fair": fair,
            "vol": vol,
            "raw_vol": raw_vol,
            "z": z,
            "tau": tau,
        }
    if not picked:
        return None
    shares = NOTIONAL / max(picked["px"], 0.01)
    exit_px = None
    exit_why = "settle"
    for ts in range(picked["ts"] + RESCORE, end - 3, RESCORE):
        left = end - ts
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open) or 0.0
        signed = lead if picked["side"] == "Up" else -lead
        raw_vol = series.realized_vol_bps_sqrt_s(ts, 120)
        vol = None if raw_vol is None else raw_vol * float(vol_scale)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
        mark = te.last_print(prints, ts, picked["side"], slack=30)
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
    return {
        "slug": ev["slug"],
        "asset": ev.get("asset"),
        "end": end,
        "side": picked["side"],
        "px": picked["px"],
        "left": picked["left"],
        "lead": round(picked["lead"], 4),
        "fair": round(picked["fair"], 4),
        "vol": None if picked["vol"] is None else round(picked["vol"], 4),
        "raw_vol": None if picked["raw_vol"] is None else round(picked["raw_vol"], 4),
        "z": None if picked["z"] is None else round(picked["z"], 4),
        "tau": picked["tau"],
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "pnl": round(pnl, 5),
    }


def run_tape(tape, series, params: TwapParams, *, vol_scale: float = 1.0) -> list[dict]:
    rows = []
    for ev, prints in tape:
        row = simulate_last(ev, series, prints, params, vol_scale=vol_scale)
        if row:
            rows.append(row)
    return rows


def held(rows: list[dict]) -> list[dict]:
    return [r for r in rows if not r.get("scratched")]


def reliability(rows: list[dict], edges=None) -> list[dict]:
    edges = edges or (0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 1.01)
    xs = held(rows)
    out = []
    for lo, hi in zip(edges, edges[1:]):
        bucket = [r for r in xs if lo - 1e-12 <= float(r["fair"]) < hi]
        n = len(bucket)
        w = sum(1 for r in bucket if r["won"])
        mean_fair = None if n == 0 else round(sum(r["fair"] for r in bucket) / n, 4)
        hit = None if n == 0 else round(w / n, 4)
        out.append(
            {
                "lo": lo,
                "hi": hi,
                "n": n,
                "mean_fair": mean_fair,
                "hit": hit,
                "gap": None if hit is None or mean_fair is None else round(hit - mean_fair, 4),
                "pnl_usd": round(sum(r["pnl"] for r in bucket), 2),
            }
        )
    return out


def ece(rows: list[dict]) -> float | None:
    rel = [b for b in reliability(rows) if b["n"] > 0 and b["hit"] is not None]
    n = sum(b["n"] for b in rel)
    if n == 0:
        return None
    return round(sum(b["n"] * abs(b["gap"]) for b in rel) / n, 4)


def pack(rows: list[dict]) -> dict:
    train, hold = te.split_holdout(rows)
    rec = {"all": te.summarize(rows), "train": te.summarize(train), "holdout": te.summarize(hold)}
    rec["robust"] = bool(
        rec["train"]["ev_ok"]
        and rec["holdout"]["ev_ok"]
        and rec["train"]["n"] >= 25
        and rec["holdout"]["n"] >= 25
    )
    rec["ece_held_all"] = ece(rows)
    rec["ece_held_holdout"] = ece(hold)
    return rec


def filt(rows, *, min_fair=None, min_z=None, max_vol=None, min_vol=None):
    out = []
    for r in rows:
        if min_fair is not None and float(r["fair"]) < min_fair:
            continue
        if min_z is not None and (r["z"] is None or abs(float(r["z"])) < min_z):
            continue
        if max_vol is not None and (r["raw_vol"] is None or float(r["raw_vol"]) > max_vol):
            continue
        if min_vol is not None and (r["raw_vol"] is None or float(r["raw_vol"]) < min_vol):
            continue
        out.append(r)
    return out


def quantiles(xs: list[float]) -> dict | None:
    if not xs:
        return None
    s = sorted(xs)
    def q(p):
        i = min(len(s) - 1, max(0, int(round((len(s) - 1) * p))))
        return round(s[i], 4)
    return {
        "n": len(s),
        "p10": q(0.10),
        "p25": q(0.25),
        "p50": q(0.50),
        "p75": q(0.75),
        "p90": q(0.90),
        "mean": round(sum(s) / len(s), 4),
    }


def download_zip(sym: str, day: str) -> Path | None:
    dest_dir = rp.BN_ROOT / sym
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{sym}-1s-{day}.zip"
    if dest.exists() and dest.stat().st_size > 1000:
        return dest
    url = f"{VISION_ZIP}/{sym}/1s/{sym}-1s-{day}.zip"
    req = urllib.request.Request(url, headers=UA)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        print(f"  downloaded {dest.name} {dest.stat().st_size}", flush=True)
        return dest
    except urllib.error.HTTPError as exc:
        print(f"  zip {sym} {day} HTTP {exc.code}", flush=True)
        if dest.exists():
            dest.unlink()
        return None
    except Exception as exc:
        print(f"  zip {sym} {day} {type(exc).__name__}", flush=True)
        if dest.exists():
            dest.unlink()
        return None


def oracle_samples(asset: str, series, t0: int, t1: int) -> list[dict]:
    """BM fair vs actual 60s-TWAP winner, no CLOB. One sample per 5m at left=180 if |lead|>=6."""
    out = []
    start = t0 - (t0 % 300) + 300
    while start + 300 <= t1:
        end = start + 300
        tw_open = series.twap(start, 60)
        tw_end = series.twap(end, 60)
        if tw_open is None or tw_end is None or tw_open <= 0:
            start += 300
            continue
        winner = "Up" if tw_end >= tw_open else "Down"
        for left in (240, 200, 180, 150, 120):
            ts = end - left
            tw = series.twap(ts, 60)
            if tw is None:
                continue
            lead = lead_bps(tw, tw_open)
            if lead is None or abs(lead) < 6.0 or abs(lead) > MAX_LEAD:
                continue
            vol = series.realized_vol_bps_sqrt_s(ts, 120)
            fair_up = fair_p_up(lead, vol, float(left))
            if fair_up is None or vol is None:
                continue
            side = "Up" if lead >= 0 else "Down"
            fair = fair_up if side == "Up" else (1.0 - fair_up)
            z = lead_z(lead if side == "Up" else -lead, vol, float(left))
            out.append(
                {
                    "asset": asset,
                    "start": start,
                    "left": left,
                    "lead": round(lead, 4),
                    "vol": round(vol, 4),
                    "fair": round(fair, 4),
                    "z": None if z is None else round(z, 4),
                    "won": side == winner,
                    "scratched": False,
                    "pnl": 0.0,
                }
            )
        start += 300
    return out


def live_report(path: Path) -> dict:
    if not path.exists():
        return {"n": 0, "note": "no live fills file"}
    try:
        rows = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"n": 0, "note": "unreadable live fills"}
    if isinstance(rows, dict):
        rows = rows.get("rows") or []
    with_fair = [r for r in rows if r.get("fair") is not None]
    held_rows = [r for r in with_fair if r.get("held")]
    core = [r for r in held_rows if r.get("asset") in CORE]
    alts = [r for r in held_rows if r.get("asset") not in CORE]

    def hit(xs):
        n = len(xs)
        w = sum(1 for r in xs if r.get("won"))
        return {
            "n": n,
            "hit": None if n == 0 else round(w / n, 4),
            "mean_fair": None if n == 0 else round(sum(float(r["fair"]) for r in xs) / n, 4),
            "mean_abs_lead": None if n == 0 else round(sum(abs(float(r.get("lead") or 0)) for r in xs) / n, 3),
        }

    implied = []
    for r in with_fair:
        fair = float(r["fair"])
        left = r.get("left")
        lead = r.get("lead")
        if left is None or lead is None or fair <= 0.5 or fair >= 1.0:
            continue
        tau = settlement_tau(float(left))
        # invert BM: z = Φ^{-1}(fair); vol = |lead| / (z sqrt tau)
        # Ackley-ish inverse CDF via binary search
        lo, hi = -8.0, 8.0
        target = fair
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            cdf = 0.5 * (1.0 + math.erf(mid / math.sqrt(2.0)))
            if cdf < target:
                lo = mid
            else:
                hi = mid
        z = 0.5 * (lo + hi)
        if tau is None or abs(z) < 1e-6:
            continue
        vol = abs(float(lead)) / (abs(z) * math.sqrt(tau))
        implied.append(
            {
                "asset": r.get("asset"),
                "fair": round(fair, 4),
                "left": left,
                "lead": lead,
                "vol_impl": round(vol, 4),
                "held": r.get("held"),
                "won": r.get("won"),
                "slug": r.get("slug"),
            }
        )
    return {
        "n_closed_like": len(rows),
        "n_with_fair": len(with_fair),
        "held_all": hit(held_rows),
        "held_core": hit(core),
        "held_alts": hit(alts),
        "implied_vol_held_alts": quantiles([x["vol_impl"] for x in implied if x["held"] and x["asset"] not in CORE]),
        "implied_vol_held_core": quantiles([x["vol_impl"] for x in implied if x["held"] and x["asset"] in CORE]),
        "held_alt_rows": [
            {
                "slug": r.get("slug"),
                "fair": r.get("fair"),
                "lead": r.get("lead"),
                "left": r.get("left"),
                "px": r.get("px"),
                "won": r.get("won"),
            }
            for r in held_rows
            if r.get("asset") not in CORE
        ],
    }


def main() -> None:
    events = json.loads((rp.CACHE / "_events.json").read_text())
    newest = max(e["end"] for e in events)
    t0 = te.TWAP60_START - 180
    t1 = newest + 5
    print("load btc/eth series", flush=True)
    series_of = {"btc": rp.load_series("btc", t0, t1), "eth": rp.load_series("eth", t0, t1)}
    tapes = {"btc": tf.load_asset_tape(events, "btc"), "eth": tf.load_asset_tape(events, "eth")}
    print(f"tape btc {len(tapes['btc'])} eth {len(tapes['eth'])}", flush=True)

    params = TwapParams(
        min_price=0.45,
        max_price=0.55,
        min_lead_bps=6.0,
        min_edge=0.04,
        min_left=120.0,
        max_left=280.0,
    )
    print("simulate last-take rev47/48 baseline", flush=True)
    base_rows = []
    for asset, series in series_of.items():
        part = run_tape(tapes[asset], series, params, vol_scale=1.0)
        print(f"  {asset} n={len(part)}", flush=True)
        base_rows.extend(part)

    gates = []

    def add(name: str, rows: list[dict], **meta):
        rec = pack(rows)
        rec["name"] = name
        rec.update(meta)
        gates.append(rec)
        h = rec["holdout"]
        print(
            f"{name:40s} n={rec['all']['n']:4d} all ${rec['all']['pnl_usd']:+7.1f} "
            f"hold n={h['n']:3d} hit={h.get('take_win_rate')} pnl={h['pnl_usd']:+.1f} "
            f"ece={rec.get('ece_held_holdout')} robust={rec['robust']}",
            flush=True,
        )

    add("baseline_last120_no_late_cheap", base_rows)
    add("btc_only", [r for r in base_rows if r["asset"] == "btc"], asset="btc")
    add("eth_only", [r for r in base_rows if r["asset"] == "eth"], asset="eth")
    for mf in (0.55, 0.58, 0.62, 0.65, 0.70, 0.75):
        add(f"min_fair_{mf:.2f}", filt(base_rows, min_fair=mf), min_fair=mf)
    for mz in (0.25, 0.35, 0.45, 0.55, 0.70, 0.90):
        add(f"min_z_{mz:.2f}", filt(base_rows, min_z=mz), min_z=mz)
    vols = [r["raw_vol"] for r in base_rows if r.get("raw_vol")]
    q = quantiles(vols) or {}
    if q:
        add("skip_vol_above_p75", filt(base_rows, max_vol=q["p75"]), max_vol=q["p75"])
        add("skip_vol_below_p25", filt(base_rows, min_vol=q["p25"]), min_vol=q["p25"])
        add("skip_quiet_p10", filt(base_rows, min_vol=q["p10"]), min_vol=q["p10"])
    OUT.write_text(json.dumps({"partial": True, "gates": [{"name": g["name"], "all": g["all"], "train": g["train"], "holdout": g["holdout"], "robust": g["robust"], "ece_held_holdout": g.get("ece_held_holdout")} for g in gates], "reliability_baseline": reliability(base_rows)}, indent=2) + "\n")
    print("checkpoint", OUT, flush=True)

    print("vol-scale resims (entry+scratch)", flush=True)
    scaled = {}
    for k in (1.5, 2.0):
        rows = []
        for asset, series in series_of.items():
            rows.extend(run_tape(tapes[asset], series, params, vol_scale=k))
        scaled[k] = rows
        add(f"vol_scale_{k:.2f}", rows, vol_scale=k)

    # Binance alt 1s vs BTC, last 7 days that exist in the BTC cache.
    btc_days = sorted(p.name.split("-1s-")[-1].replace(".zip", "") for p in (rp.BN_ROOT / "BTCUSDT").glob("BTCUSDT-1s-*.zip"))
    btc_days = [d for d in btc_days if d >= "2026-08-14"][-7:]
    print("alt 1s days", btc_days, flush=True)
    alt_vol = {}
    oracle = {}
    if btc_days:
        d0 = datetime.fromisoformat(btc_days[0]).replace(tzinfo=timezone.utc)
        d1 = datetime.fromisoformat(btc_days[-1]).replace(tzinfo=timezone.utc) + timedelta(days=1)
        a0, a1 = int(d0.timestamp()), int(d1.timestamp()) - 1
        # ensure alt zips
        for asset, sym in ALT_SYMBOL.items():
            ok = 0
            for day in btc_days:
                if download_zip(sym, day):
                    ok += 1
            print(f"  {asset} zips {ok}/{len(btc_days)}", flush=True)
        rp.SYMBOL.update(ALT_SYMBOL)
        rp.SYMBOL["btc"] = "BTCUSDT"
        print("load alt series", flush=True)
        btc_s = rp.load_series("btc", a0, a1)
        samples_btc = oracle_samples("btc", btc_s, a0, a1)
        oracle["btc"] = {
            "vol": quantiles([s["vol"] for s in samples_btc]),
            "reliability_left180": reliability([s for s in samples_btc if s["left"] == 180]),
            "n": len(samples_btc),
            "hit_all": None
            if not samples_btc
            else round(sum(1 for s in samples_btc if s["won"]) / len(samples_btc), 4),
        }
        alt_vol["btc"] = oracle["btc"]["vol"]
        for asset in ALT_SYMBOL:
            try:
                series = rp.load_series(asset, a0, a1)
            except Exception as exc:
                print(f"  skip {asset} {type(exc).__name__}", flush=True)
                continue
            samples = oracle_samples(asset, series, a0, a1)
            if not samples:
                print(f"  {asset} oracle n=0", flush=True)
                continue
            o = {
                "vol": quantiles([s["vol"] for s in samples]),
                "reliability_left180": reliability([s for s in samples if s["left"] == 180]),
                "n": len(samples),
                "hit_all": round(sum(1 for s in samples if s["won"]) / len(samples), 4),
            }
            if alt_vol.get("btc") and o["vol"]:
                o["vol_vs_btc_p50"] = round(o["vol"]["p50"] / max(alt_vol["btc"]["p50"], 1e-9), 3)
            oracle[asset] = o
            alt_vol[asset] = o["vol"]
            print(
                f"  {asset} n={o['n']} vol_p50={o['vol']['p50'] if o['vol'] else None} "
                f"vs_btc={o.get('vol_vs_btc_p50')} hit={o['hit_all']}",
                flush=True,
            )

    live = live_report(LIVE_FILLS)

    # Ship rule: train+holdout +EV, holdout PnL >= 95% of baseline holdout, n>=25.
    base_hold = next(g for g in gates if g["name"] == "baseline_last120_no_late_cheap")["holdout"]["pnl_usd"]
    shippable = []
    for g in gates:
        if g["name"] == "baseline_last120_no_late_cheap":
            continue
        if not g["robust"]:
            continue
        if g["holdout"]["pnl_usd"] + 1e-9 < 0.95 * base_hold:
            continue
        if (g["holdout"].get("take_win_rate") or 0) < 0.70:
            continue
        shippable.append(
            {
                "name": g["name"],
                "holdout_pnl": g["holdout"]["pnl_usd"],
                "holdout_hit": g["holdout"].get("take_win_rate"),
                "train_pnl": g["train"]["pnl_usd"],
                "ece_holdout": g.get("ece_held_holdout"),
                "delta_vs_base_hold": round(g["holdout"]["pnl_usd"] - base_hold, 2),
            }
        )
    shippable.sort(key=lambda x: (x["delta_vs_base_hold"], x["holdout_hit"] or 0), reverse=True)

    vol_by_asset = {
        a: quantiles([r["raw_vol"] for r in base_rows if r["asset"] == a and r.get("raw_vol")]) for a in CORE
    }
    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": "Binance 1s TWAP vs Binance T0. Live = Chainlink vs Chainlink T0.",
        "question": "Does per-asset vol / calibrated P(win) beat Rev 48 mechanical gates on holdout?",
        "baseline": "CLOB last-take, min_left=120, skip left<180 & px<50c, lead 6-40, scratch as live (alts-only book dump).",
        "vol_clob_btc_eth": vol_by_asset,
        "reliability_baseline": reliability(base_rows),
        "reliability_btc": reliability([r for r in base_rows if r["asset"] == "btc"]),
        "reliability_eth": reliability([r for r in base_rows if r["asset"] == "eth"]),
        "gates": [{"name": g["name"], "all": g["all"], "train": g["train"], "holdout": g["holdout"], "robust": g["robust"], "ece_held_holdout": g.get("ece_held_holdout")} for g in gates],
        "shippable_vs_baseline": shippable,
        "oracle_alt_vs_btc": oracle,
        "live_fair_fills": live,
        "findings": {
            "do_not_ship_if_empty": "If shippable_vs_baseline is empty, keep Rev 48. Do not raise BTC/ETH min_fair off a holdout-only bump.",
            "live_n_is_not_a_grid": "Post-Rev47 held alts with stored fair are a diagnostic, not a production threshold.",
        },
    }
    # headline after we see numbers
    if not shippable:
        report["findings"]["headline"] = (
            "BTC+ETH CLOB holdout does not reward tightening fair/z/vol beyond Rev 48. "
            "Alt/BTC 1s vol ratio is the remaining lever; it must be an alt-only floor, not a global min_fair."
        )
    else:
        report["findings"]["headline"] = (
            f"Candidates that keep holdout PnL: {[s['name'] for s in shippable[:5]]}. "
            "Still do not apply a BTC-fitted min_fair to alts without an alt CLOB tape."
        )
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT, flush=True)
    print("shippable", shippable[:8], flush=True)


if __name__ == "__main__":
    main()
