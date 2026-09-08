#!/usr/bin/env python3
"""Rev 59 search: new *follow* overlays, not fade.

Question: keep first-cross direction, cut $0 steamrollers that CLOB 拉盤
confirmed while the settlement oracle had already gone thin.

Human-shaped rejects (already -EV or forbidden): reverse, 8¢ SL, dump_mid90,
min_left 180, 4bps, leftover chase, complement, 97–98.

Mechanical candidates, all causal:
  1. Oracle-fair dump at left<90 even after CLOB 62 (CLOB can 拉盤; TWAP is the 0/1).
  2. Lead-thin dump at left<90 (|signed lead| < k bps).
  3. Retrace dump: 62 printed, then px90 back in 45–55 (拉盤 failed).
  4. Entry skip: first-touch lead (not persistent 15s), knife 6–6.5 bps, thin entry fair.
  5. Same-unix BTC/ETH side disagreement skip.

Ship only if train AND holdout beat shipped first+dump_by90, holdout n stays
≥60% of baseline, holdout take WR ≥0.85. Live n=11 is sanity, not a gate.
"""
from __future__ import annotations

import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from tape_pull import (  # noqa: E402
    CONFIRM,
    CORE,
    HAIRCUT,
    HOLDOUT_DAYS,
    LIVE_STAKE,
    MON,
    NOTIONAL,
    REV,
    TAKES,
    TWAP60,
    buys,
    fill_ts,
    load_raw,
    pack,
    path_of,
    pnl_scratch,
    rec_of,
    scale3,
    split_holdout,
)

OUT = Path(__file__).resolve().parent / "rev59_oracle.json"
LIVE = Path("/tmp/live_dir_path.json")


def oracle(series, start: int, ts: int, side: str, left: float):
    tw_open = series.twap(start, 60)
    tw = series.twap(ts, 60)
    if tw_open is None or tw is None:
        return None
    lead = lead_bps(tw, tw_open)
    if lead is None:
        return None
    vol = series.realized_vol_bps_sqrt_s(ts, 120)
    fair_up = fair_p_up(lead, vol, float(left), lookback=60)
    if fair_up is None:
        return None
    fair = fair_up if side == "Up" else (1.0 - fair_up)
    signed = lead if side == "Up" else -lead
    return {
        "lead": round(float(lead), 4),
        "signed": round(float(signed), 4),
        "fair": round(float(fair), 4),
        "vol": None if vol is None else round(float(vol), 4),
    }


def dump_row(row: dict, why: str, exit_px: float | None = None) -> dict:
    x = dict(row)
    if row.get("scratched"):
        return x
    raw = exit_px if exit_px is not None else row.get("px90") or row.get("px")
    px = max(0.01, float(raw) - HAIRCUT)
    x["scratched"] = True
    x["exit_why"] = why
    x["pnl"] = pnl_scratch(row["px"], px)
    return x


def ship_base(row: dict) -> dict:
    x = dict(row)
    x["pnl"] = float(row["orig_pnl"])
    if (not row.get("orig_scratch")) and (not row.get("ever_62_by90")):
        return dump_row(x, "unconfirmed_by90", row.get("px90") or row.get("px"))
    x["scratched"] = bool(row.get("orig_scratch"))
    return x


def apply_skip(rows: list[dict], pred) -> list[dict]:
    return [r for r in rows if not pred(r)]


def apply_overlay(rows: list[dict], pred, why: str) -> list[dict]:
    out = []
    for r in rows:
        if pred(r):
            out.append(dump_row(r, why))
        else:
            out.append(r)
    return out


def slim(p: dict) -> dict:
    a, t, h = p["all"], p["train"], p["holdout"]
    return {
        "n": a["n"],
        "pnl5": a["pnl5"],
        "pnl3": a["pnl3"],
        "take_wr": a["take_wr"],
        "held": a["held"],
        "scratch_n": a["scratch_n"],
        "train": {"n": t["n"], "pnl5": t["pnl5"], "take_wr": t["take_wr"], "ev_ok": t["ev_ok"]},
        "holdout": {"n": h["n"], "pnl5": h["pnl5"], "take_wr": h["take_wr"], "ev_ok": h["ev_ok"]},
        "robust": p["robust"],
    }


def beats(g: dict, base: dict) -> bool:
    return bool(
        g["robust"]
        and g["train"]["pnl5"] + 1e-9 >= base["train"]["pnl5"]
        and g["holdout"]["pnl5"] >= base["holdout"]["pnl5"] + 5.0
        and g["holdout"]["n"] >= 0.6 * base["holdout"]["n"]
        and g["all"]["n"] >= 0.6 * base["all"]["n"]
        and (g["holdout"]["take_wr"] or 0) >= 0.85
    )


def hydrate(series_of: dict) -> list[dict]:
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    rows = []
    n_or = 0
    for r in first:
        slug = r["slug"]
        start, end = int(r["start"]), int(r["end"])
        t_fill = fill_ts(r)
        raw = load_raw(REV, slug) or load_raw(MON, slug)
        full = buys(raw, start, end, lo=0.05, hi=0.99) if raw else []
        feat = path_of(full, r["side"], t_fill, end) if full else {}
        series = series_of.get(r["asset"])
        o_fill = oracle(series, start, t_fill, r["side"], float(r["left"])) if series else None
        o90 = oracle(series, start, end - 90, r["side"], 90.0) if series else None
        o60 = oracle(series, start, end - 60, r["side"], 60.0) if series else None
        o30 = oracle(series, start, end - 30, r["side"], 30.0) if series else None
        o_pre = oracle(series, start, t_fill - 15, r["side"], float(r["left"]) + 15) if series else None
        if o_fill:
            n_or += 1
        row = dict(r)
        row["ts"] = t_fill
        row["orig_pnl"] = float(r["pnl"])
        row["orig_scratch"] = bool(r.get("scratched"))
        row["orig_won"] = bool(r["won"])
        row.update(feat)
        row["o_fill"] = o_fill
        row["o90"] = o90
        row["o60"] = o60
        row["o30"] = o30
        row["persist15"] = bool(o_pre and abs(o_pre["lead"]) >= 6.0 - 1e-12)
        row["knife"] = abs(float(r["lead"])) < 6.5
        rows.append(row)
    print(f"hydrated {len(rows)} oracle_fill={n_or}", flush=True)
    # same-unix disagreement
    by = defaultdict(list)
    for r in rows:
        by[int(r["start"])].append(r)
    disagree = set()
    for start, xs in by.items():
        sides = {x["side"] for x in xs}
        if len(xs) >= 2 and len(sides) > 1:
            for x in xs:
                disagree.add(x["slug"])
    for r in rows:
        r["disagree"] = r["slug"] in disagree
    return rows


def live_sanity(series_of: dict) -> dict:
    if not LIVE.exists():
        return {"n": 0}
    live = json.loads(LIVE.read_text())
    out = []
    for h in live.get("rows") or []:
        if not h.get("since57"):
            continue
        slug = h["slug"]
        win = int(str(slug).rsplit("-", 1)[-1])
        start, end = win, win + 300
        side = "Up" if str(h.get("leg") or "").lower() == "up" else "Down"
        series = series_of.get(h["asset"])
        t_fill = int(h["ts"])
        left = float(h.get("left") or (end - t_fill))
        o_fill = oracle(series, start, t_fill, side, left) if series else None
        o90 = oracle(series, start, end - 90, side, 90.0) if series else None
        out.append({
            "slug": slug,
            "held": h.get("held"),
            "won": h.get("follow_won"),
            "net": h.get("net"),
            "max": h.get("max_after"),
            "ever_62_by90": h.get("ever_62_by90"),
            "o_fill": o_fill,
            "o90": o90,
            "oracle70_would_dump": bool(o90 and o90["fair"] < 0.70),
            "lead3_would_dump": bool(o90 and o90["signed"] < 3.0),
            "retrace": bool(h.get("ever_62_by90") and h.get("px90") is not None and float(h["px90"]) < 0.55),
        })
    return {"n": len(out), "rows": out}


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    tmin = min(int(r["start"]) for r in first) - 180
    tmax = max(int(r["end"]) for r in first) + 5
    # live fills through ~Sep 2 10:00
    tmax = max(tmax, 1788342900 + 5)
    print(f"load series {tmin}->{tmax}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", tmin, tmax),
        "eth": rp.load_series("eth", tmin, tmax),
    }
    rows = hydrate(series_of)
    base_rows = [ship_base(r) for r in rows]
    base = pack(base_rows)
    print("BASE shipped dump90", slim(base), flush=True)

    grid = {"base_dump90": slim(base)}

    def add(name, xs, note=""):
        p = pack(xs)
        rec = slim(p)
        rec["note"] = note
        rec["beats"] = beats(p, base)
        rec["d_ho"] = round(p["holdout"]["pnl5"] - base["holdout"]["pnl5"], 2)
        rec["d_tr"] = round(p["train"]["pnl5"] - base["train"]["pnl5"], 2)
        grid[name] = rec
        mark = "BEAT" if rec["beats"] else ("+" if rec["d_ho"] > 0 and rec["d_tr"] > 0 else ".")
        print(
            f"{mark:4} {name:28s} n={rec['n']:3d} all ${rec['pnl5']:+7.1f} "
            f"tr {rec['train']['pnl5']:+6.1f} ho {rec['holdout']['pnl5']:+6.1f} "
            f"dho={rec['d_ho']:+6.1f} wr={rec['holdout']['take_wr']} robust={rec['robust']}",
            flush=True,
        )

    add("base_dump90", base_rows, "shipped first-cross + unconfirmed dump")

    # --- entry skips (keep BM+dump90 on survivors) ---
    add("skip_knife_6_5", [r for r in base_rows if not r.get("knife")], "skip |lead|<6.5 at first-cross")
    add(
        "skip_first_touch",
        [r for r in base_rows if r.get("persist15") or r.get("o_fill") is None],
        "require |lead|>=6 already 15s earlier; keep if no oracle",
    )
    add(
        "skip_entry_fair70",
        [r for r in base_rows if r.get("o_fill") is None or r["o_fill"]["fair"] >= 0.70],
        "skip entry fair<0.70",
    )
    add(
        "skip_entry_fair75",
        [r for r in base_rows if r.get("o_fill") is None or r["o_fill"]["fair"] >= 0.75],
        "skip entry fair<0.75",
    )
    add(
        "skip_entry_fair80",
        [r for r in base_rows if r.get("o_fill") is None or r["o_fill"]["fair"] >= 0.80],
        "skip entry fair<0.80",
    )
    add("skip_disagree_unix", [r for r in base_rows if not r.get("disagree")], "skip when BTC/ETH first-cross sides differ")
    add("btc_only", [r for r in base_rows if r["asset"] == "btc"])
    add("eth_only", [r for r in base_rows if r["asset"] == "eth"])

    # --- overlays on residual holds after dump90 ---
    for thr in (0.55, 0.60, 0.65, 0.70, 0.75, 0.80):
        add(
            f"oracle_fair90_{int(thr * 100)}",
            apply_overlay(
                base_rows,
                lambda r, t=thr: (not r.get("scratched")) and r.get("o90") is not None and r["o90"]["fair"] < t,
                f"oracle_fair90_{thr}",
            ),
            f"at 90s left dump if BM fair < {thr}, even if CLOB confirmed 62",
        )
    for k in (2.0, 3.0, 4.0, 5.0, 6.0):
        add(
            f"lead_thin90_{int(k)}bps",
            apply_overlay(
                base_rows,
                lambda r, kk=k: (not r.get("scratched")) and r.get("o90") is not None and r["o90"]["signed"] < kk,
                f"lead_thin90_{k}",
            ),
            f"at 90s left dump if signed oracle lead < {k} bps",
        )
    for px90_cap in (0.52, 0.55, 0.58):
        add(
            f"retrace90_{int(px90_cap * 100)}",
            apply_overlay(
                base_rows,
                lambda r, cap=px90_cap: (
                    (not r.get("scratched"))
                    and r.get("ever_62_by90")
                    and r.get("px90") is not None
                    and float(r["px90"]) < cap
                ),
                f"retrace90_{px90_cap}",
            ),
            f"confirmed 62 then last print before 90s left < {px90_cap}",
        )

    # continuous: dump if fair thin at 90 OR 60 OR 30
    for thr in (0.60, 0.70):
        add(
            f"oracle_fair_late_{int(thr * 100)}",
            apply_overlay(
                base_rows,
                lambda r, t=thr: (not r.get("scratched"))
                and any(
                    (r.get(k) or {}).get("fair") is not None and r[k]["fair"] < t for k in ("o90", "o60", "o30")
                ),
                f"oracle_fair_late_{thr}",
            ),
            f"dump if fair < {thr} at 90, 60, or 30s left",
        )

    # combo: persist + oracle 70
    persist = [r for r in base_rows if r.get("persist15") or r.get("o_fill") is None]
    add(
        "persist_and_oracle70",
        apply_overlay(
            persist,
            lambda r: (not r.get("scratched")) and r.get("o90") is not None and r["o90"]["fair"] < 0.70,
            "oracle_fair90_0.7",
        ),
        "persist 15s AND oracle fair90<0.70",
    )
    add(
        "oracle70_and_retrace55",
        apply_overlay(
            apply_overlay(
                base_rows,
                lambda r: (not r.get("scratched")) and r.get("o90") is not None and r["o90"]["fair"] < 0.70,
                "oracle70",
            ),
            lambda r: (
                (not r.get("scratched"))
                and r.get("ever_62_by90")
                and r.get("px90") is not None
                and float(r["px90"]) < 0.55
            ),
            "retrace55",
        ),
    )

    # peak-retrace from high-water (max_after>=0.62 and px90<=high-0.12) causal at 90s
    add(
        "peak_fade_12c",
        apply_overlay(
            base_rows,
            lambda r: (
                (not r.get("scratched"))
                and float(r.get("max_after") or 0) + 1e-12 >= CONFIRM
                and r.get("px90") is not None
                and float(r["max_after"]) - float(r["px90"]) >= 0.12
            ),
            "peak_fade_12c",
        ),
        "high-water >=62 and px90 is ≥12¢ below peak (not 8¢ from entry)",
    )

    ranked = sorted(
        ((n, g) for n, g in grid.items() if n != "base_dump90"),
        key=lambda kv: (kv[1].get("beats"), kv[1]["holdout"]["pnl5"], kv[1]["train"]["pnl5"]),
        reverse=True,
    )
    winners = [n for n, g in ranked if g.get("beats")]
    live = live_sanity(series_of)
    pick = winners[0] if winners else None
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "New follow overlay that beats shipped dump_by90 on train+holdout without flipping direction.",
        "n_joined": len(rows),
        "baseline": slim(base),
        "grid": grid,
        "winners": winners,
        "pick": pick,
        "live_sanity": live,
        "ship": bool(pick),
        "do_not": [
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
            "scratch_adverse_0.08",
            "btc_eth_min_left_180",
            "chase_leftover",
            "complement",
            "favorite_97_98",
        ],
        "findings": {
            "headline": (
                f"Pick {pick or 'none'}. "
                f"Baseline holdout ${base['holdout']['pnl5']} wr={base['holdout']['take_wr']}. "
                f"Winners={winners[:5]}."
            )
        },
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    print("PICK", pick, "winners", winners, "elapsed", rec["elapsed_s"], flush=True)
    if live.get("rows"):
        for r in live["rows"]:
            print(
                f"  live {r['slug'][-12:]} held={r['held']} won={r['won']} "
                f"o90={r['o90']} dump70={r['oracle70_would_dump']} lead3={r['lead3_would_dump']}",
                flush=True,
            )
    return rec


if __name__ == "__main__":
    run()
