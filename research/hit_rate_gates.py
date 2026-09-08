#!/usr/bin/env python3
"""Why research held ~80% and live held ~24%, and which gates recover 70-80%.

Research take_win_rate = held-to-settle wins / held (scratch excluded).
The shipped sim takes the FIRST 6bps print (avg left ~186s, avg px ~0.53).
Live kept hunting later 45¢ dogs. This grid measures first vs last take,
min_left, min_px, min_fair — train/holdout, do not ship a holdout-only rule.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import TwapParams, entry_edge, fair_p_up, lead_bps, should_scratch  # noqa: E402
from app.fees import taker_fee  # noqa: E402
import twap_engine as te  # noqa: E402
import twap_freq as tf  # noqa: E402

OUT = Path(__file__).with_name("hit_rate_gates.json")
NOTIONAL = te.NOTIONAL
RESCORE = te.RESCORE


def simulate_market(ev, series, prints, params: TwapParams, *, pick: str) -> dict | None:
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
        side = "Up" if lead >= 0 else "Down"
        pr = te.last_print(prints, ts, side, slack=25)
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
        cand = {
            "ts": ts,
            "left": left,
            "side": side,
            "px": pr["px"],
            "lead": lead,
            "fair": fair,
        }
        if pick == "first":
            picked = cand
            break
        picked = cand
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
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
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
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "pnl": round(pnl, 5),
    }


def run_tape(tape, series, params: TwapParams, *, pick: str) -> list[dict]:
    rows = []
    for ev, prints in tape:
        row = simulate_market(ev, series, prints, params, pick=pick)
        if row:
            rows.append(row)
    return rows


def filt(rows: list[dict], *, min_left=None, min_px=None, min_fair=None, min_lead=None) -> list[dict]:
    out = []
    for r in rows:
        if min_left is not None and r["left"] < min_left:
            continue
        if min_px is not None and r["px"] < min_px:
            continue
        if min_fair is not None and r["fair"] < min_fair:
            continue
        if min_lead is not None and abs(r["lead"]) < min_lead:
            continue
        out.append(r)
    return out


def pack(rows: list[dict]) -> dict:
    train, hold = te.split_holdout(rows)
    rec = {
        "all": te.summarize(rows),
        "train": te.summarize(train),
        "holdout": te.summarize(hold),
    }
    rec["robust"] = bool(
        rec["train"]["ev_ok"]
        and rec["holdout"]["ev_ok"]
        and rec["train"]["n"] >= 25
        and rec["holdout"]["n"] >= 25
        and (rec["holdout"]["take_win_rate"] or 0) >= 0.70
    )
    rec["hit70"] = bool(
        rec["holdout"]["take_win_rate"] is not None and rec["holdout"]["take_win_rate"] >= 0.70
    )
    rec["hit80"] = bool(
        rec["holdout"]["take_win_rate"] is not None and rec["holdout"]["take_win_rate"] >= 0.80
    )
    return rec


def late_cheap(rows: list[dict]) -> dict:
    late = [r for r in rows if r["left"] < 120]
    cheap = [r for r in rows if r["px"] < 0.48]
    late_cheap_rows = [r for r in rows if r["left"] < 180 and r["px"] < 0.50]
    early = [r for r in rows if r["left"] >= 180]
    return {
        "late_lt120": te.summarize(late),
        "cheap_lt48": te.summarize(cheap),
        "late_cheap": te.summarize(late_cheap_rows),
        "early_ge180": te.summarize(early),
        "px45_46": te.summarize([r for r in rows if r["px"] < 0.47]),
        "px50_55": te.summarize([r for r in rows if r["px"] >= 0.50]),
    }


def main() -> None:
    events = json.loads((tf.rp.CACHE / "_events.json").read_text())
    newest = max(e["end"] for e in events)
    t0 = te.TWAP60_START - 180
    t1 = newest + 5
    print("load series", flush=True)
    series_of = {
        "btc": tf.rp.load_series("btc", t0, t1),
        "eth": tf.rp.load_series("eth", t0, t1),
    }
    tapes = {
        "btc": tf.load_asset_tape(events, "btc"),
        "eth": tf.load_asset_tape(events, "eth"),
    }
    print(f"tape btc {len(tapes['btc'])} eth {len(tapes['eth'])}", flush=True)

    shipped = TwapParams(min_price=0.45, max_price=0.55, min_lead_bps=6.0, min_edge=0.04, min_left=12.0, max_left=280.0)
    rev46 = TwapParams(min_price=0.45, max_price=0.55, min_lead_bps=6.0, min_edge=0.04, min_left=120.0, max_left=280.0)

    first_rows = []
    last_rows = []
    first46 = []
    last46 = []
    for asset, series in series_of.items():
        first_rows.extend(run_tape(tapes[asset], series, shipped, pick="first"))
        last_rows.extend(run_tape(tapes[asset], series, shipped, pick="last"))
        first46.extend(run_tape(tapes[asset], series, rev46, pick="first"))
        last46.extend(run_tape(tapes[asset], series, rev46, pick="last"))
        print(f"  {asset} first12={sum(1 for r in first_rows if r['asset']==asset)} last12={sum(1 for r in last_rows if r['asset']==asset)}", flush=True)

    gates = []

    def add(name: str, rows: list[dict], **meta):
        rec = pack(rows)
        rec["name"] = name
        rec.update(meta)
        gates.append(rec)
        h = rec["holdout"]
        print(
            f"{name:42s} n={rec['all']['n']:4d} all ${rec['all']['pnl_usd']:+7.1f} "
            f"hold n={h['n']:3d} hit={h.get('take_win_rate')} pnl={h['pnl_usd']:+.1f} "
            f"avgL={h.get('avg_left')} avgPx={h.get('avg_px')} robust={rec['robust']}",
            flush=True,
        )

    add("research_first_min12", first_rows, pick="first", min_left=12)
    add("live_like_last_min12", last_rows, pick="last", min_left=12)
    add("rev46_first_min120", first46, pick="first", min_left=120)
    add("rev46_last_min120", last46, pick="last", min_left=120)

    add("first_min180", filt(first_rows, min_left=180), pick="first", min_left=180)
    add("last_min180", filt(last_rows, min_left=180), pick="last", min_left=180)
    add("first_px50", filt(first_rows, min_px=0.50), pick="first", min_px=0.50)
    add("last_px50", filt(last_rows, min_px=0.50), pick="last", min_px=0.50)
    add("first_fair55", filt(first_rows, min_fair=0.55), pick="first", min_fair=0.55)
    add("last_fair55", filt(last_rows, min_fair=0.55), pick="last", min_fair=0.55)
    add("first_lead8", filt(first_rows, min_lead=8), pick="first", min_lead=8)
    add("last_lead8", filt(last_rows, min_lead=8), pick="last", min_lead=8)

    add("first_min120_px48", filt(first46, min_px=0.48), pick="first", min_left=120, min_px=0.48)
    add("last_min120_px48", filt(last46, min_px=0.48), pick="last", min_left=120, min_px=0.48)
    add("first_min120_fair55", filt(first46, min_fair=0.55), pick="first", min_left=120, min_fair=0.55)
    add("last_min120_fair55", filt(last46, min_fair=0.55), pick="last", min_left=120, min_fair=0.55)
    add("first_min180_px48", filt(first_rows, min_left=180, min_px=0.48), pick="first", min_left=180, min_px=0.48)
    add("last_min180_px48", filt(last_rows, min_left=180, min_px=0.48), pick="last", min_left=180, min_px=0.48)
    add("first_no_late_cheap", [r for r in first_rows if not (r["left"] < 180 and r["px"] < 0.50)], pick="first")
    add("last_no_late_cheap", [r for r in last_rows if not (r["left"] < 180 and r["px"] < 0.50)], pick="last")
    add("last_min120_no_late_cheap", [r for r in last46 if not (r["left"] < 180 and r["px"] < 0.50)], pick="last", min_left=120)
    add("last_min120_px50_fair55", filt(last46, min_px=0.50, min_fair=0.55), pick="last", min_left=120, min_px=0.50, min_fair=0.55)
    add("first_min150", filt(first_rows, min_left=150), pick="first", min_left=150)
    add("last_min150", filt(last_rows, min_left=150), pick="last", min_left=150)

    # BTC-only vs both (research shipped both)
    add("first_btc_only", [r for r in first_rows if r["asset"] == "btc"], pick="first", asset="btc")
    add("last_btc_only", [r for r in last_rows if r["asset"] == "btc"], pick="last", asset="btc")

    robust = [g for g in gates if g["robust"]]
    robust.sort(key=lambda g: (g["holdout"]["take_win_rate"] or 0, g["holdout"]["pnl_usd"]), reverse=True)
    hit70 = [g for g in gates if g["hit70"] and g["holdout"]["ev_ok"] and g["train"]["ev_ok"]]
    hit70.sort(key=lambda g: (g["holdout"]["pnl_usd"] + g["train"]["pnl_usd"], g["holdout"]["take_win_rate"] or 0), reverse=True)

    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": "Binance 1s TWAP vs Binance open. Live = Chainlink vs Chainlink T0.",
        "take_win_rate": "held-to-settle wins / held; scratch excluded (same as twap_freq.json)",
        "slices": {
            "research_first_min12": late_cheap(first_rows),
            "live_like_last_min12": late_cheap(last_rows),
            "rev46_last_min120": late_cheap(last46),
        },
        "gates": gates,
        "robust_hit70": [{"name": g["name"], "holdout": g["holdout"], "train": g["train"], "all": g["all"]} for g in robust[:12]],
        "best_plus_ev_hit70": [{"name": g["name"], "holdout": g["holdout"], "train": g["train"], "all": g["all"]} for g in hit70[:8]],
        "findings": {
            "headline": (
                "80% 係 first-cross（窗內第一下 ≥6bps），唔係 45¢ 尾盤。"
                "last-take（現場追價）持有命中會塌。"
            )
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT, flush=True)
    print("best robust", [g["name"] for g in robust[:5]], flush=True)
    print("best +EV hit70", [g["name"] for g in hit70[:5]], flush=True)


if __name__ == "__main__":
    main()
