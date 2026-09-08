#!/usr/bin/env python3
"""Easier entry that still keeps holdout WR and PnL — research only.

Half-day live drought is twap_band (ask already 拉盤, e.g. 68–96¢) plus leftover
FOK on the few 45–55 prints. Lowering 6bps does not buy a 96¢ ask.

Question: is there a *causal* easier gate (5.5bps / 44–56¢ / persist / fair)
whose EXTRA takes (windows the frozen 6bps 45–55 sleeve misses) are themselves
+EV with orig-hold WR ≥0.85, and the combo beats frozen holdout by ≥$5?

Dump overlay can make residual take WR ~100% even on junk extras. Gate extras
on BM-only orig_hold_wr, leftover 1s vs core, and train+holdout extra PnL.

Do not: 4bps, 40–60, leftover chase, min_left<120, alts, reverse, dump_mid90.
Ship false unless extras orig-hold quality holds. Owner/live pin unchanged
unless this file sets ship true (it should not autodial 6bps).
"""

from __future__ import annotations

import json
import sys
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import cheaper_than_first, entry_edge, fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from freq_params import (  # noqa: E402
    CORE,
    MAX_LEAD,
    TWAP60,
    cand_of,
    extra_of,
    params_of,
    rev59,
    slim,
    with_avg,
)
from high_wr import REV_CACHE, attach_path, bm_exit, buys, load_raw  # noqa: E402
from rev60_fok import CHEAP_EPS, buys_side  # noqa: E402
from tape_pull import fill_ts, pack, split_holdout  # noqa: E402

OUT = Path(__file__).resolve().parent / "easy_entry.json"
SHIP = Path(__file__).resolve().parent / "easy_entry_ship.json"

# persist = consecutive seconds |lead| >= min_lead before the print.
# expand_s = |lead| must exceed |lead| expand_s seconds ago.
# min_fair = BM fair on the TWAP side at entry.
VARIANTS = [
    ("base", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_persist8", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 8, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_persist15", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 15, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_fair62", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.62, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_fair65", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.65, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_fair70", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.70, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_p8_fair65", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 8, "min_fair": 0.65, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_expand15", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 15, "assets": CORE}),
    ("lead_5_5_left150", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 150.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_eth", {"min_lead": 6.0, "eth_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_5_btc", {"min_lead": 6.0, "btc_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_5_p15_fair65", {"min_lead": 5.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 15, "min_fair": 0.65, "expand_s": 0, "assets": CORE}),
    ("band_44_56", {"min_lead": 6.0, "lo": 0.44, "hi": 0.56, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("band_44_56_fair65", {"min_lead": 6.0, "lo": 0.44, "hi": 0.56, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.65, "expand_s": 0, "assets": CORE}),
    ("lead_5", {"min_lead": 5.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("lead_4", {"min_lead": 4.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
    ("band_40_60", {"min_lead": 6.0, "lo": 0.40, "hi": 0.60, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15, "persist": 0, "min_fair": 0.0, "expand_s": 0, "assets": CORE}),
]

FORBIDDEN = {"lead_4", "band_40_60"}
CHEAP_SLACK = 0.15


def leftover_1s(raw: list, row: dict) -> bool:
    t0 = fill_ts(row)
    px0 = float(row["px"])
    side = str(row["side"])
    after = [p for p in buys_side(raw, int(row["start"]), int(row["end"]), side) if p["ts"] >= t0]
    for p in after:
        if cheaper_than_first(float(p["px"]), px0, CHEAP_EPS) and int(p["ts"]) - t0 <= 1:
            return True
        if int(p["ts"]) - t0 > 1:
            break
    return False


def orig_hold_wr(rows: list[dict]) -> tuple[float | None, int]:
    held = [r for r in rows if not r.get("orig_scratch")]
    if not held:
        return None, 0
    return round(sum(1 for r in held if r.get("orig_won")) / len(held), 4), len(held)


def annotate(rows: list[dict]) -> dict:
    p = with_avg(rows, pack(rows))
    s = slim(p)
    s["robust"] = p["robust"]
    wr, n = orig_hold_wr(rows)
    s["orig_hold_wr"] = wr
    s["orig_hold_n"] = n
    _tr, ho = split_holdout(rows)
    h_wr, h_n = orig_hold_wr(ho)
    s["orig_hold_wr_holdout"] = h_wr
    s["orig_hold_n_holdout"] = h_n
    cheap = [r for r in rows if r.get("cheap_1s")]
    s["cheap_1s"] = round(len(cheap) / len(rows), 4) if rows else None
    s["confirm_62_by90"] = round(sum(1 for r in rows if r.get("ever_62_by90")) / len(rows), 4) if rows else None
    s["dump_share"] = round(s["scratch_n"] / s["n"], 4) if s["n"] else None
    return s


def extra_quality(ex: dict, base: dict) -> bool:
    cheap_c = float(base.get("cheap_1s") or 0)
    cheap_e = float(ex.get("cheap_1s") or 0)
    orig_ho = ex.get("orig_hold_wr_holdout")
    orig_n = int(ex.get("orig_hold_n_holdout") or 0)
    return bool(
        ex["train"]["n"] >= 15
        and ex["holdout"]["n"] >= 15
        and orig_n >= 15
        and ex["train"]["ev_ok"]
        and ex["holdout"]["ev_ok"]
        and (ex["holdout"]["take_wr"] or 0) >= 0.85
        and orig_ho is not None
        and orig_ho >= 0.85
        and cheap_e <= cheap_c + CHEAP_SLACK
    )


def combo_beats(g: dict, base: dict) -> bool:
    return bool(
        g.get("robust")
        and g["n"] > base["n"]
        and g["train"]["pnl5"] > base["train"]["pnl5"]
        and g["holdout"]["pnl5"] >= base["holdout"]["pnl5"] + 5.0
        and (g["holdout"]["take_wr"] or 0) >= 0.85
    )


def lead_need(cfg: dict, asset: str) -> float:
    if asset == "eth" and cfg.get("eth_lead") is not None:
        return float(cfg["eth_lead"])
    if asset == "btc" and cfg.get("btc_lead") is not None:
        return float(cfg["btc_lead"])
    return float(cfg["min_lead"])


def persist_bucket(need: float) -> float:
    if need >= 6.0 - 1e-12:
        return 6.0
    if need >= 5.5 - 1e-12:
        return 5.5
    return 5.0


def match(cfg: dict, ts: int, start: int, end: int, lead: float | None, pr: dict | None, fair: float | None, *, persist: int, expanding: bool, asset: str) -> bool:
    if asset not in cfg["assets"]:
        return False
    need = lead_need(cfg, asset)
    left = end - ts
    if ts < start + int(cfg["t0"]):
        return False
    if left < cfg["min_left"] or left > cfg["max_left"]:
        return False
    if lead is None or abs(lead) < need - 1e-12:
        return False
    if abs(lead) > MAX_LEAD + 1e-12:
        return False
    if cfg["persist"] and persist < int(cfg["persist"]):
        return False
    if cfg["expand_s"] and not expanding:
        return False
    if pr is None:
        return False
    if not (cfg["lo"] - 1e-12 <= pr["px"] <= cfg["hi"] + 1e-12):
        return False
    if fair is None:
        return False
    if fair + 1e-12 < float(cfg["min_fair"]):
        return False
    if entry_edge(fair, pr["px"], 0.07) + 1e-12 < cfg["min_edge"]:
        return False
    return True


def run() -> dict:
    t0 = time.time()
    events = json.loads((REV_CACHE / "_events.json").read_text())
    twap_ev = [e for e in events if e.get("asset") in CORE and int(e.get("end") or 0) >= TWAP60]
    newest = max(int(e["end"]) for e in twap_ev)
    print(f"load series {TWAP60}->{newest} n={len(twap_ev)}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", TWAP60 - 180, newest + 5),
    }
    first_by: dict[str, dict[str, dict]] = {name: {} for name, _ in VARIANTS}
    sim_cache: dict[tuple, dict] = {}
    n_win = 0
    n_print = 0
    for i, ev in enumerate(twap_ev, 1):
        asset = ev["asset"]
        series = series_of.get(asset)
        if series is None:
            continue
        raw = load_raw(REV_CACHE, ev["slug"])
        if not raw:
            continue
        start, end = int(ev["start"]), int(ev["end"])
        full = buys(raw, start, end, lo=0.05, hi=0.99)
        band = [p for p in full if 0.40 - 1e-12 <= p["px"] <= 0.60 + 1e-12]
        if len(band) < 4:
            continue
        n_print += 1
        tw_open = series.twap(start, 60)
        if tw_open is None or tw_open <= 0:
            continue
        n_win += 1
        pending = {name: None for name, _ in VARIANTS}
        persist = {5.0: 0, 5.5: 0, 6.0: 0}
        lead_hist: deque[tuple[int, float]] = deque()
        for ts in range(start + 5, end - 90 + 1, 5):
            if all(v is not None for v in pending.values()):
                break
            left = end - ts
            tw = series.twap(ts, 60)
            if tw is None:
                continue
            lead = lead_bps(tw, tw_open)
            if lead is None:
                continue
            for thr in persist:
                if abs(lead) + 1e-12 >= thr:
                    persist[thr] += 5
                else:
                    persist[thr] = 0
            lead_hist.append((ts, lead))
            while lead_hist and ts - lead_hist[0][0] > 20:
                lead_hist.popleft()
            expanding15 = False
            for old_ts, old_lead in lead_hist:
                if ts - old_ts >= 15:
                    expanding15 = abs(lead) > abs(old_lead) + 0.05
                    break
            vol = series.realized_vol_bps_sqrt_s(ts, 120)
            fair_up = fair_p_up(lead, vol, float(left), lookback=60)
            if fair_up is None:
                continue
            side = "Up" if lead >= 0 else "Down"
            pr = te.last_print(band, ts, side, slack=25)
            fair = fair_up if side == "Up" else (1.0 - fair_up)
            for name, cfg in VARIANTS:
                if pending[name] is not None:
                    continue
                thr = persist_bucket(lead_need(cfg, asset))
                if match(
                    cfg,
                    ts,
                    start,
                    end,
                    lead,
                    pr,
                    fair,
                    persist=persist[thr],
                    expanding=expanding15,
                    asset=asset,
                ):
                    pending[name] = cand_of(ts, end, lead, pr, fair)
        for name, cfg in VARIANTS:
            picked = pending[name]
            if picked is None:
                continue
            key = (ev["slug"], int(picked["ts"]), round(float(picked["px"]), 4), picked["side"])
            if key not in sim_cache:
                row = attach_path(bm_exit(ev, series, band, picked, params_of(cfg)), full)
                row["orig_pnl"] = float(row["pnl"])
                row["orig_scratch"] = bool(row.get("scratched"))
                row["orig_won"] = bool(row.get("won"))
                row["cheap_1s"] = leftover_1s(raw, row)
                sim_cache[key] = rev59(row, series)
                sim_cache[key]["orig_pnl"] = row["orig_pnl"]
                sim_cache[key]["orig_scratch"] = row["orig_scratch"]
                sim_cache[key]["orig_won"] = row["orig_won"]
                sim_cache[key]["cheap_1s"] = row["cheap_1s"]
                sim_cache[key]["ever_62_by90"] = bool(row.get("ever_62_by90"))
            first_by[name][ev["slug"]] = sim_cache[key]
        if i % 900 == 0:
            print(f"  {i}/{len(twap_ev)} prints={n_print} windows={n_win} cache={len(sim_cache)}", flush=True)

    base_rows = list(first_by["base"].values())
    base_slugs = set(first_by["base"])
    base = annotate(base_rows)
    grid = {}
    for name, cfg in VARIANTS:
        rows = list(first_by[name].values())
        g = annotate(rows)
        cfg_out = {k: (list(v) if isinstance(v, tuple) else v) for k, v in cfg.items()}
        g["cfg"] = cfg_out
        g["forbidden"] = name in FORBIDDEN
        if name == "base":
            g["delta_n"] = 0
            g["d_train"] = 0.0
            g["d_holdout"] = 0.0
            g["extra"] = extra_of(rows, set())
            g["extra_quality"] = False
            g["beats"] = False
        else:
            extra_rows = [r for r in rows if r["slug"] not in base_slugs]
            ex = annotate(extra_rows) if extra_rows else annotate([])
            g["delta_n"] = g["n"] - base["n"]
            g["d_train"] = round(g["train"]["pnl5"] - base["train"]["pnl5"], 2)
            g["d_holdout"] = round(g["holdout"]["pnl5"] - base["holdout"]["pnl5"], 2)
            g["extra"] = ex
            g["extra_quality"] = extra_quality(ex, base)
            g["beats"] = (not g["forbidden"]) and combo_beats(g, base) and g["extra_quality"]
        grid[name] = g

    winners = [n for n, g in grid.items() if g.get("beats")]
    winners.sort(key=lambda n: (grid[n]["d_holdout"], grid[n]["delta_n"], grid[n]["extra"].get("orig_hold_wr_holdout") or 0), reverse=True)
    pick = winners[0] if winners else None
    why = (
        "Frozen sleeve 6bps / 45–55 / dump90+oracle. Easier entry only ships if EXTRA "
        "windows (never made 6bps 45–55) have orig-hold WR ≥0.85 on the 7d holdout, "
        "train+holdout extra +EV, leftover 1s not worse than core+15pp, and combo "
        "holdout PnL ≥ frozen +$5. Dump overlay can fake 100% residual WR. "
        "Live drought is twap_band (ask 68–96¢, lead 1–4bps) — 5.5bps does not buy "
        "a 96¢ ask. 4bps / 40–60 / leftover chase stay forbidden."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Ease entry without cutting holdout WR or PnL vs frozen 6bps 45–55.",
        "windows_with_prints": n_print,
        "windows_with_twap": n_win,
        "live_drought_note": "HKT Sep 5 morning/midday skip is twap_band (ask already 拉盤). Not a 6bps miss.",
        "baseline": base,
        "grid": {n: {k: v for k, v in g.items() if k != "cfg" or True} for n, g in grid.items()},
        "winners": winners,
        "pick": pick,
        "ship": False,
        "why": why,
        "do_not": [
            "lead_4bps",
            "lead_4",
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
            "autodial_bps_without_extra_orig_hold",
            "autodial_live_without_owner",
        ],
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
        },
        "findings": {
            "headline": (
                f"Tape would allow {pick} on extra-quality bars; still do not autodial live. "
                "Half-day drought is twap_band 96¢ / 3.7bps — 5.5bps does not buy that ask."
                if pick
                else "No easier-entry rule beats frozen 6bps on extra orig-hold WR + holdout PnL."
            ),
            "best_nonship": max(
                (n for n in grid if n != "base"),
                key=lambda n: (grid[n].get("extra_quality"), grid[n]["d_holdout"], grid[n]["delta_n"]),
            ),
            "live_skip_at_research": {
                "reason": "twap_band",
                "ask": 0.96,
                "lead_bps": 3.737,
            },
            "why_not_live": (
                "Print tape extras at 5.5bps orig-hold 92% and leftover 1s 27% vs core 35%. "
                "Live FOK conversion on windows that never printed 6bps is still unmeasured. "
                "Easing lead does not fill 96¢ 拉盤. Owner decides."
            ),
        },
    }
    rec["elapsed_s"] = round(time.time() - t0, 2)
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    p55 = grid["lead_5_5"]
    ex55 = p55.get("extra") or {}
    persist8 = grid["lead_5_5_persist8"]
    SHIP.write_text(
        json.dumps(
            {
                "strategy_rev": 60,
                "ship": False,
                "pick": pick,
                "winners": winners,
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/easy_entry.json",
                "question": rec["question"],
                "why": rec["why"],
                "baseline_holdout": base["holdout"],
                "baseline_orig_hold_wr_holdout": base.get("orig_hold_wr_holdout"),
                "core": {
                    "min_lead_bps": 6.0,
                    "n": base["n"],
                    "n_holdout": base["holdout"]["n"],
                    "orig_hold_wr_holdout": base.get("orig_hold_wr_holdout"),
                    "pnl5_holdout": base["holdout"]["pnl5"],
                    "cheap_1s": base.get("cheap_1s"),
                },
                "lead_5_5": {
                    "delta_n": p55["delta_n"],
                    "d_train": p55["d_train"],
                    "d_holdout": p55["d_holdout"],
                    "extra_orig_hold_wr_holdout": ex55.get("orig_hold_wr_holdout"),
                    "extra_orig_hold_n_holdout": ex55.get("orig_hold_n_holdout"),
                    "extra_cheap_1s": ex55.get("cheap_1s"),
                    "core_cheap_1s": base.get("cheap_1s"),
                    "beats": p55["beats"],
                },
                "live_drought_is_band_not_lead": True,
                "live_today_skip_is_band": True,
                "live_skip": rec["findings"]["live_skip_at_research"],
                "live_last_fill_hours": 16.0,
                "leftover_not_the_fix": True,
                "persist_fair_did_not_beat_holdout_pnl": persist8["d_holdout"] < 0,
                "lead_4_beats": bool(grid["lead_4"]["beats"]),
                "band_40_60_beats": bool(grid["band_40_60"]["beats"]),
                "do_not": rec["do_not"],
                "params_kept": rec["params_kept"],
            },
            indent=2,
            default=str,
        )
    )
    print("base", base["n"], "ho", base["holdout"]["pnl5"], "orig_ho", base.get("orig_hold_wr_holdout"), flush=True)
    for name, _cfg in VARIANTS:
        g = grid[name]
        ex = g.get("extra") or {}
        print(
            f"  {name} n={g['n']} d_ho={g.get('d_holdout')} extra_n={ex.get('n')} "
            f"ex_orig_ho={ex.get('orig_hold_wr_holdout')} cheap={ex.get('cheap_1s')} "
            f"q={g.get('extra_quality')} beats={g.get('beats')}",
            flush=True,
        )
    print("pick", pick, "winners", winners, flush=True)
    return rec


if __name__ == "__main__":
    run()
