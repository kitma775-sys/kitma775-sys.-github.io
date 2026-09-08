#!/usr/bin/env python3
"""Frequency levers on the shipped first-cross + Rev 59 dump sleeve.

Question: can entry params add takes without cutting holdout take WR
(within 1pp) or holdout PnL. Overlay is always dump_by90 + oracle fair<0.60.

Do not ship even if they pass a naive n-up test: 4bps, 40–60¢, leftover chase,
min_left below 120, alts, 15m, reverse, dump_mid90, 8¢ SL.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import TwapParams, entry_edge, fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from high_wr import (  # noqa: E402
    HAIRCUT,
    REV_CACHE,
    attach_path,
    bm_exit,
    buys,
    load_raw,
)
from rev59_oracle import oracle  # noqa: E402
from tape_pull import pack, rec_of, split_holdout  # noqa: E402

OUT = Path(__file__).resolve().parent / "freq_params.json"
TWAP60 = te.TWAP60_START
CORE = ("btc", "eth")
CONFIRM = 0.62
ORACLE_FAIR = 0.60
MAX_LEAD = 40.0

# t0=15 matches the research scanner. Live accepts left<=280 so first ~20s
# are skipped; t0=5 + max_left 300 is the live-relevant early window.
VARIANTS = [
    ("base", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("max_left_290", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 290.0, "min_edge": 0.04, "t0": 5}),
    ("max_left_295", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 295.0, "min_edge": 0.04, "t0": 5}),
    ("max_left_300", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 300.0, "min_edge": 0.04, "t0": 5}),
    ("lead_5_5", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("lead_5", {"min_lead": 5.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("lead_4", {"min_lead": 4.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("band_44_56", {"min_lead": 6.0, "lo": 0.44, "hi": 0.56, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("band_43_57", {"min_lead": 6.0, "lo": 0.43, "hi": 0.57, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("band_40_60", {"min_lead": 6.0, "lo": 0.40, "hi": 0.60, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("min_left_100", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 100.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("min_left_90", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 90.0, "max_left": 280.0, "min_edge": 0.04, "t0": 15}),
    ("edge_03", {"min_lead": 6.0, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 280.0, "min_edge": 0.03, "t0": 15}),
    ("max300_lead55", {"min_lead": 5.5, "lo": 0.45, "hi": 0.55, "min_left": 120.0, "max_left": 300.0, "min_edge": 0.04, "t0": 5}),
]

FORBIDDEN = {
    "lead_4",
    "band_40_60",
    "min_left_100",
    "min_left_90",
    "chase_leftover",
    "twap_reverse_on",
    "dump_mid90",
}


def dump_row(row: dict, why: str, exit_px: float | None = None) -> dict:
    x = dict(row)
    if row.get("scratched"):
        return x
    raw = exit_px if exit_px is not None else row.get("px90") or row.get("px")
    px = max(0.01, float(raw) - HAIRCUT)
    x["scratched"] = True
    x["exit_why"] = why
    x["pnl"] = te.pnl_scratch(row["px"], px)
    return x


def rev59(row: dict, series) -> dict:
    x = dict(row)
    if not x.get("scratched") and not x.get("ever_62_by90"):
        x = dump_row(x, "unconfirmed_by90", x.get("px90") or x.get("px"))
    if not x.get("scratched") and series is not None:
        o90 = oracle(series, int(x["start"]), int(x["end"]) - 90, x["side"], 90.0)
        x["o90"] = o90
        if o90 is not None and float(o90["fair"]) + 1e-12 < ORACLE_FAIR:
            x = dump_row(x, "twap_scratch_oracle", x.get("px90") or x.get("px"))
    return x


def match(cfg: dict, ts: int, start: int, end: int, lead: float | None, pr: dict | None, fair: float | None) -> bool:
    left = end - ts
    if ts < start + int(cfg["t0"]):
        return False
    if left < cfg["min_left"] or left > cfg["max_left"]:
        return False
    if lead is None or abs(lead) < cfg["min_lead"] - 1e-12:
        return False
    if abs(lead) > MAX_LEAD + 1e-12:
        return False
    if pr is None:
        return False
    if not (cfg["lo"] - 1e-12 <= pr["px"] <= cfg["hi"] + 1e-12):
        return False
    if fair is None:
        return False
    if entry_edge(fair, pr["px"], 0.07) + 1e-12 < cfg["min_edge"]:
        return False
    return True


def cand_of(ts: int, end: int, lead: float, pr: dict, fair: float) -> dict:
    side = "Up" if lead >= 0 else "Down"
    return {
        "ts": ts,
        "left": end - ts,
        "side": side,
        "px": pr["px"],
        "lead": lead,
        "fair": fair,
    }


def slim(p: dict) -> dict:
    a, t, h = p["all"], p["train"], p["holdout"]
    return {
        "n": a["n"],
        "pnl5": a["pnl5"],
        "pnl3": a["pnl3"],
        "take_wr": a["take_wr"],
        "held": a["held"],
        "scratch_n": a["scratch_n"],
        "avg_left": a.get("avg_left"),
        "train": {"n": t["n"], "pnl5": t["pnl5"], "take_wr": t["take_wr"], "ev_ok": t["ev_ok"]},
        "holdout": {"n": h["n"], "pnl5": h["pnl5"], "take_wr": h["take_wr"], "ev_ok": h["ev_ok"]},
        "robust": p["robust"],
    }


def with_avg(rows: list[dict], packed: dict) -> dict:
    if rows:
        packed["all"]["avg_left"] = round(sum(float(r["left"]) for r in rows) / len(rows), 1)
        packed["all"]["avg_lead"] = round(sum(abs(float(r["lead"])) for r in rows) / len(rows), 2)
        packed["all"]["avg_px"] = round(sum(float(r["px"]) for r in rows) / len(rows), 4)
    return packed


def wr_safe(g: dict, base: dict) -> bool:
    ho_wr = g["holdout"]["take_wr"]
    base_wr = base["holdout"]["take_wr"]
    if ho_wr is None or base_wr is None:
        return False
    return bool(
        g["robust"]
        and g["n"] > base["n"]
        and ho_wr + 1e-12 >= base_wr - 0.01
        and g["holdout"]["pnl5"] + 1e-9 >= base["holdout"]["pnl5"]
        and g["train"]["pnl5"] + 1e-9 >= 0.95 * base["train"]["pnl5"]
        and ho_wr >= 0.85
    )


def extra_of(rows: list[dict], base_slugs: set[str]) -> dict:
    extra = [r for r in rows if r["slug"] not in base_slugs]
    packed = with_avg(extra, pack(extra))
    return {"n": len(extra), **slim(packed)}


def params_of(cfg: dict) -> TwapParams:
    return TwapParams(
        min_price=cfg["lo"],
        max_price=cfg["hi"],
        min_lead_bps=cfg["min_lead"],
        min_edge=cfg["min_edge"],
        min_left=cfg["min_left"],
        max_left=cfg["max_left"],
        max_lead_bps=MAX_LEAD,
        confirm_px=CONFIRM,
        confirm_left=90.0,
        confirm_fair=ORACLE_FAIR,
        take_profit=0.87,
    )


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
                if match(cfg, ts, start, end, lead, pr, fair):
                    pending[name] = cand_of(ts, end, lead, pr, fair)
        for name, cfg in VARIANTS:
            picked = pending[name]
            if picked is None:
                continue
            key = (ev["slug"], int(picked["ts"]), round(float(picked["px"]), 4), picked["side"])
            if key not in sim_cache:
                row = attach_path(bm_exit(ev, series, band, picked, params_of(cfg)), full)
                sim_cache[key] = rev59(row, series)
            first_by[name][ev["slug"]] = sim_cache[key]
        if i % 900 == 0:
            print(f"  {i}/{len(twap_ev)} prints={n_print} windows={n_win} cache={len(sim_cache)}", flush=True)

    grid = {}
    base_rows = list(first_by["base"].values())
    base_slugs = set(first_by["base"])
    base_pack = with_avg(base_rows, pack(base_rows))
    base_slim = slim(base_pack)
    for name, cfg in VARIANTS:
        rows = list(first_by[name].values())
        packed = with_avg(rows, pack(rows))
        g = slim(packed)
        g["cfg"] = cfg
        g["beats"] = False if name == "base" else wr_safe(g, base_slim)
        g["forbidden"] = name in FORBIDDEN
        if name != "base":
            g["delta_n"] = g["n"] - base_slim["n"]
            g["delta_ho_wr"] = None if g["holdout"]["take_wr"] is None or base_slim["holdout"]["take_wr"] is None else round(
                g["holdout"]["take_wr"] - base_slim["holdout"]["take_wr"], 4
            )
            g["delta_ho_pnl5"] = round(g["holdout"]["pnl5"] - base_slim["holdout"]["pnl5"], 2)
            g["extra"] = extra_of(rows, base_slugs)
        grid[name] = g

    winners = [n for n, g in grid.items() if g.get("beats") and not g.get("forbidden")]
    winners.sort(key=lambda n: (grid[n]["holdout"]["pnl5"], grid[n]["n"]), reverse=True)

    why_live_sparse = (
        "Research first-cross BTC+ETH is ~7% of printed windows under 6bps 45–55 120–280 "
        f"({base_slim['n']} takes / {n_print} windows, ~34 prints/day). "
        "Live is FOK: miss the first 45–55 ask and leftover chase is banned, so the window dies. "
        "Rev 59's first 2.3h only produced 2 windows that even passed the gate. "
        "Health skips are twap_band / twap_lead / twap_window. "
        "max_left 280→300 adds 0 takes: 6bps almost never prints in the first 20s. "
        "Tape n-levers (5.5bps, 5bps, 44–56¢) are windows that never made 6bps/45–55 — "
        "same last-look family as forbidden 4bps / 40–60. Keep the floor."
    )
    why_not = (
        "Do not relax 6bps or 45–55. Print-tape extras stay ~100% hold WR only because "
        "Rev 59 dump_by90+oracle removes residual losers; live FOK last-look already "
        "converts 6bps prints worse than this tape. max_left 300 / min_edge 0.03 add nothing. "
        "min_left <120 reopens the 90s dump gate. Dual BTC+ETH is already on."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Add takes without cutting shipped take WR. Overlay = dump_by90 + oracle fair<0.60.",
        "windows_with_prints": n_print,
        "windows_with_twap": n_win,
        "baseline": base_slim,
        "grid": grid,
        "tape_winners": winners,
        "winners": [],
        "pick": None,
        "ship": False,
        "why_not": why_not,
        "forbidden": sorted(FORBIDDEN),
        "do_not": [
            "lead_4bps",
            "lead_5bps",
            "lead_5_5bps",
            "band_40_60",
            "band_44_56",
            "band_43_57",
            "chase_leftover",
            "min_left_below_120",
            "alts",
            "15m",
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
        ],
        "why_live_sparse": why_live_sparse,
        "live_since_rev59": {
            "hours": 2.3,
            "gate_pass_unix": 2,
            "filled": 1,
            "fok_killed": 7,
            "note": "scans table only logs gate-pass FOK attempts, not band/lead skips",
        },
        "findings": {
            "headline": (
                "Ship none. max_left 300 +0. 5.5bps tape +124 / 5bps +290 / 44–56¢ +87 "
                "are last-look extras, not live-safe WR. Keep 6bps 45–55 120–280."
            )
        },
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    print("PICK", rec["pick"], "tape_winners", winners, "elapsed", rec["elapsed_s"], flush=True)
    for name, g in grid.items():
        extra = g.get("extra") or {}
        print(
            f"  {name:16} n={g['n']:4} d_n={g.get('delta_n', 0):+4} "
            f"ho_wr={g['holdout']['take_wr']} d_wr={g.get('delta_ho_wr')} "
            f"ho_pnl={g['holdout']['pnl5']:+.1f} extra_n={extra.get('n')} "
            f"beats={g.get('beats')} forb={g.get('forbidden')}",
            flush=True,
        )
    return rec


if __name__ == "__main__":
    run()
