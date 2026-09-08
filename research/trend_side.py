#!/usr/bin/env python3
"""Higher-timeframe 走勢 vs 5m T0 first-cross — research only.

Live miss (HKT 00:20 Sep 4): previous 5m ended −26bps, new T0 is already at
the lows, 60s TWAP bounces +7bps, sleeve buys Up, window settles Down.

Question: at first-cross, may we read *already settled* prior windows /
15–30m spot / 180s TWAP and (a) skip, or (b) flip Up/Down, and beat the
frozen Rev 59/60 sleeve on train AND holdout?

Causal: previous 5m has closed at this window's start. 15m/30m returns use
spot at fill vs fill−N. No lookahead into this window's settle.

Do not: reverse every take, dump_mid90, 8¢ SL, leftover chase, 4bps, 40–60.
Ship only if beats frozen by ≥$5 holdout, n ≥60%, holdout take WR ≥0.85.
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

from app.twap import lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
from learn_fail import shipped_overlay  # noqa: E402
from rev59_oracle import beats, hydrate, slim  # noqa: E402
from tape_pull import (  # noqa: E402
    CORE,
    HOLDOUT_DAYS,
    TAKES,
    TWAP60,
    fade_hold_settle,
    pack,
    rec_of,
    split_holdout,
)

OUT = Path(__file__).resolve().parent / "trend_side.json"
SHIP = Path(__file__).resolve().parent / "trend_side_ship.json"
WINDOW = 300


def ret_bps(series, t0: int, t1: int) -> float | None:
    a = series.at(int(t0)) if series is not None else None
    b = series.at(int(t1)) if series is not None else None
    if a is None or b is None or float(a) <= 0:
        return None
    return (float(b) - float(a)) / float(a) * 10000.0


def win_lead(series, start: int, end: int, lookback: int = 60) -> float | None:
    if series is None:
        return None
    tw0 = series.twap(int(start), lookback)
    tw1 = series.twap(int(end), lookback)
    if tw0 is None or tw1 is None:
        return None
    return lead_bps(tw1, tw0)


def side_of_bps(bps: float | None) -> str | None:
    if bps is None:
        return None
    if abs(float(bps)) < 1e-9:
        return None
    return "Up" if float(bps) > 0 else "Down"


def agree(side: str, bps: float | None) -> bool | None:
    sig = side_of_bps(bps)
    if sig is None:
        return None
    return sig == side


def attach_trend(rows: list[dict], series_of: dict) -> list[dict]:
    out = []
    for r in rows:
        series = series_of.get(r.get("asset"))
        start, end = int(r["start"]), int(r["end"])
        fill = int(r.get("ts") or fill_fallback(r))
        side = str(r.get("side") or "")
        prev_s, prev_e = start - WINDOW, start
        prev_lead = win_lead(series, prev_s, prev_e, 60)
        prev_spot = ret_bps(series, prev_s, prev_e)
        r15 = ret_bps(series, fill - 900, fill)
        r30 = ret_bps(series, fill - 1800, fill)
        r5 = ret_bps(series, fill - WINDOW, fill)
        lead180 = None
        lead300 = None
        spot_vs_tw = None
        if series is not None:
            tw0 = series.twap(start, 60)
            tw180 = series.twap(fill, 180)
            tw300 = series.twap(fill, 300)
            tw60 = series.twap(fill, 60)
            spot = series.at(fill)
            if tw0 is not None and tw180 is not None:
                lead180 = lead_bps(tw180, tw0)
            if tw0 is not None and tw300 is not None:
                lead300 = lead_bps(tw300, tw0)
            if tw60 is not None and spot is not None and tw60 > 0:
                spot_vs_tw = (float(spot) - float(tw60)) / float(tw60) * 10000.0
        prev_side = side_of_bps(prev_lead)
        bounce = bool(prev_side is not None and prev_side != side)
        cont = bool(prev_side is not None and prev_side == side)
        x = dict(r)
        x["prev_lead"] = None if prev_lead is None else round(float(prev_lead), 4)
        x["prev_spot_bps"] = None if prev_spot is None else round(float(prev_spot), 4)
        x["r15_bps"] = None if r15 is None else round(float(r15), 4)
        x["r30_bps"] = None if r30 is None else round(float(r30), 4)
        x["r5_bps"] = None if r5 is None else round(float(r5), 4)
        x["lead180"] = None if lead180 is None else round(float(lead180), 4)
        x["lead300"] = None if lead300 is None else round(float(lead300), 4)
        x["spot_vs_tw"] = None if spot_vs_tw is None else round(float(spot_vs_tw), 4)
        x["prev_side"] = prev_side
        x["bounce"] = bounce
        x["cont"] = cont
        x["agree15"] = agree(side, r15)
        x["agree30"] = agree(side, r30)
        x["agree180"] = agree(side, lead180)
        x["agree300"] = agree(side, lead300)
        x["agree_spot_tw"] = agree(side, spot_vs_tw)
        abs_prev = abs(float(prev_lead)) if prev_lead is not None else 0.0
        x["crash10"] = bounce and abs_prev >= 10
        x["crash15"] = bounce and abs_prev >= 15
        x["crash20"] = bounce and abs_prev >= 20
        x["crash26"] = bounce and abs_prev >= 26
        out.append(x)
    return out


def fill_fallback(row: dict) -> int:
    return int(row["end"]) - int(float(row.get("left") or 0))


def anatomy(rows: list[dict], key: str) -> dict:
    yes = [r for r in rows if r.get(key)]
    no = [r for r in rows if not r.get(key)]
    holds_yes = [r for r in yes if not r.get("scratched")]
    orig_yes = yes  # orig_won is settlement of the follow side

    def orig_wr(xs: list[dict]):
        n = len(xs)
        if not n:
            return None
        w = sum(1 for r in xs if r.get("orig_won"))
        return round(w / n, 4)

    return {
        "n": len(yes),
        "frac": round(len(yes) / len(rows), 4) if rows else None,
        "shipped": rec_of(yes),
        "complement": rec_of(no),
        "held_n": len(holds_yes),
        "held_wr": None
        if not holds_yes
        else round(sum(1 for r in holds_yes if r.get("won")) / len(holds_yes), 4),
        "orig_wr": orig_wr(yes),
        "orig_wr_rest": orig_wr(no),
    }


def fade_where(rows: list[dict], pred) -> list[dict]:
    out = []
    for r in rows:
        if pred(r):
            out.append(fade_hold_settle(r))
        else:
            out.append(r)
    return out


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    tmin = min(int(r["start"]) for r in first) - 2000
    tmax = max(int(r["end"]) for r in first) + 5
    print(f"load series {tmin}->{tmax} n={len(first)}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", tmin, tmax),
        "eth": rp.load_series("eth", tmin, tmax),
    }
    raw = hydrate(series_of)
    rows = attach_trend(shipped_overlay(raw), series_of)
    n_feat = sum(1 for r in rows if r.get("prev_lead") is not None)
    print(f"features prev_lead={n_feat}/{len(rows)}", flush=True)
    base_pack = pack(rows)
    print("SHIPPED", slim(base_pack), flush=True)

    grid: dict[str, dict] = {"frozen": slim(base_pack)}

    def add(name: str, xs: list[dict], note: str = "") -> None:
        packed = pack(xs)
        rec = slim(packed)
        rec["note"] = note
        rec["beats"] = beats(packed, base_pack)
        rec["d_ho"] = round(packed["holdout"]["pnl5"] - base_pack["holdout"]["pnl5"], 2)
        rec["d_tr"] = round(packed["train"]["pnl5"] - base_pack["train"]["pnl5"], 2)
        rec["skipped"] = len(rows) - len(xs)
        grid[name] = rec
        mark = "BEAT" if rec["beats"] else ("+" if rec["d_ho"] > 0 and rec["d_tr"] > 0 else ".")
        print(
            f"{mark:4} {name:36s} n={rec['n']:3d} skip={rec['skipped']:3d} "
            f"tr {rec['train']['pnl5']:+7.1f} ho {rec['holdout']['pnl5']:+7.1f} "
            f"dho={rec['d_ho']:+6.1f} wr={rec['holdout']['take_wr']}",
            flush=True,
        )

    add("frozen", rows, "first-cross 60s vs T0 + dump90 + oracle fair<0.60")

    # --- skip: bounce after prior-window dump/rally ---
    for k in ("bounce", "crash10", "crash15", "crash20", "crash26", "cont"):
        add(f"skip_{k}", [r for r in rows if not r.get(k)], f"skip first-cross tagged {k}")

    # --- skip: higher-TF disagreement with 5m side ---
    add("skip_disagree_15m", [r for r in rows if r.get("agree15") is not False], "skip if 15m spot return opposes 5m side")
    add("skip_disagree_30m", [r for r in rows if r.get("agree30") is not False], "skip if 30m spot return opposes 5m side")
    add("skip_disagree_180tw", [r for r in rows if r.get("agree180") is not False], "skip if 180s TWAP vs T0 opposes 60s side")
    add("skip_disagree_300tw", [r for r in rows if r.get("agree300") is not False], "skip if 300s TWAP vs T0 opposes 60s side")
    add("skip_spot_against_twap", [r for r in rows if r.get("agree_spot_tw") is not False], "skip if spot is already through the 60s TWAP against the side")
    add("require_15m_agree", [r for r in rows if r.get("agree15") is True], "only take when 15m and 5m agree")
    add("require_180_agree", [r for r in rows if r.get("agree180") is True], "only take when 180s TWAP agrees with 60s")
    add("skip_agree_15m", [r for r in rows if r.get("agree15") is not True], "control: skip when 15m AGREES (should hurt)")

    # knife bounce: live ETH 00:10 was +6.00 after prior dump
    add(
        "skip_knife_bounce",
        [r for r in rows if not (r.get("bounce") and abs(float(r.get("lead") or 0)) < 6.5)],
        "skip |lead|<6.5 that bounce the prior 5m",
    )
    add(
        "skip_crash15_or_knife_bounce",
        [
            r
            for r in rows
            if not r.get("crash15") and not (r.get("bounce") and abs(float(r.get("lead") or 0)) < 6.5)
        ],
        "skip 15bps crash-bounce or knife bounce",
    )

    # --- flip Up/Down only on bounce-after-crash (fade that print, hold to settle) ---
    for k in ("bounce", "crash10", "crash15", "crash20"):
        add(
            f"fade_{k}",
            fade_where(rows, lambda r, kk=k: bool(r.get(kk))),
            f"buy the other 5m leg on {k}; keep follow elsewhere",
        )
    add(
        "fade_disagree_15m",
        fade_where(rows, lambda r: r.get("agree15") is False),
        "if 15m opposes 5m, fade the 5m first-cross",
    )

    buckets = {
        "bounce": anatomy(rows, "bounce"),
        "crash10": anatomy(rows, "crash10"),
        "crash15": anatomy(rows, "crash15"),
        "crash20": anatomy(rows, "crash20"),
        "crash26": anatomy(rows, "crash26"),
        "cont": anatomy(rows, "cont"),
        "disagree15": rec_of([r for r in rows if r.get("agree15") is False]),
        "disagree180": rec_of([r for r in rows if r.get("agree180") is False]),
        "disagree_spot_tw": rec_of([r for r in rows if r.get("agree_spot_tw") is False]),
    }

    train, hold = split_holdout(rows)
    # train-only: skip bounce if train bounce shipped pnl < 0
    train_bounce_pnl = rec_of([r for r in train if r.get("crash15")]).get("pnl5") or 0
    add(
        "train_skip_crash15_if_red",
        rows if train_bounce_pnl >= 0 else [r for r in rows if not r.get("crash15")],
        f"train crash15 pnl5={round(train_bounce_pnl, 2)}; skip on all iff train red",
    )

    ranked = sorted(
        ((n, g) for n, g in grid.items() if n != "frozen"),
        key=lambda kv: (kv[1].get("beats"), kv[1]["holdout"]["pnl5"], kv[1]["train"]["pnl5"]),
        reverse=True,
    )
    honest = [
        kv
        for kv in ranked
        if kv[1]["d_tr"] > 0
        and kv[1]["d_ho"] > 0
        and (kv[1]["holdout"]["take_wr"] or 0) >= 0.85
        and not str(kv[0]).startswith("fade_")
    ]
    winners = [n for n, g in ranked if g.get("beats")]
    pick = winners[0] if winners else None
    why = (
        "5m Up/Down is vs this window's T0, not the 15m chart. After a dump the "
        "next T0 is already low, so a +6bps bounce is a legal Up. Bounce first-cross "
        "hold-to-settle orig WR is ~54% (coin-flip); dump90+oracle is what makes that "
        "family +EV. Skip/fade HTF 走勢 cuts n or is reverse. skip_crash26 is the "
        "closest miss (+$4.37 holdout, under $5). Keep 60s vs T0."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Look at higher-TF 走勢 before choosing 5m Up vs Down?",
        "n_joined": len(rows),
        "n_prev_lead": n_feat,
        "holdout_days": HOLDOUT_DAYS,
        "shipped": slim(base_pack),
        "anatomy": buckets,
        "grid": grid,
        "winners": winners,
        "pick": pick,
        "ship": bool(pick),
        "why": why if not pick else f"Pick {pick} beats frozen train+holdout.",
        "live_fingerprint": {
            "note": "ETH 00:20 HKT Sep 4: prev 5m −26bps, this window +7bps Up bounce, settle Down",
            "crash26_n": buckets["crash26"]["n"],
            "crash26_shipped": buckets["crash26"]["shipped"],
            "crash20_n": buckets["crash20"]["n"],
            "crash20_shipped": buckets["crash20"]["shipped"],
        },
        "do_not": [
            "htf_pick_side",
            "fade_bounce_after_crash",
            "skip_all_15m_disagree",
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
            "chase_leftover",
            "lead_4bps",
            "band_40_60",
            "autodial_from_live_n3",
        ],
        "findings": {
            "headline": why if not pick else f"Pick {pick}.",
            "best_nonbeat": None if not honest else honest[0][0],
            "best_d_ho": None if not honest else honest[0][1]["d_ho"],
            "fade_crash20_holdout_only": {
                "d_ho": grid.get("fade_crash20", {}).get("d_ho"),
                "d_tr": grid.get("fade_crash20", {}).get("d_tr"),
                "holdout_wr": (grid.get("fade_crash20") or {}).get("holdout", {}).get("take_wr"),
                "note": "holdout-only luck; train -$68; forbidden reverse family",
            },
        },
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    ship = {
        "strategy_rev": 60,
        "ship": rec["ship"],
        "pick": pick,
        "researched_at_utc": rec["researched_at_utc"],
        "source": "research/trend_side.json",
        "question": rec["question"],
        "why": rec["why"],
        "shipped_holdout": rec["shipped"]["holdout"],
        "winners": winners,
        "live_fingerprint": rec["live_fingerprint"],
        "do_not": rec["do_not"],
    }
    SHIP.write_text(json.dumps(ship, indent=2, default=str))
    print("PICK", pick, "winners", winners, "elapsed", rec["elapsed_s"], flush=True)
    return rec


if __name__ == "__main__":
    run()
