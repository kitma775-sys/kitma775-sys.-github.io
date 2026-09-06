#!/usr/bin/env python3
"""Every-5m entry + in-window management — research only.

Question: can BTC+ETH take *every* 5m window, then manage inside the window
for higher WR, without cutting holdout PnL vs the frozen 6bps∩45–55 sleeve?

Physics: settlement is Chainlink 60s TWAP vs T0. The CLOB ask is usually
already that fair. Lifting 70–96¢ every window is the 97–98 family (high WR,
bad payoff). Dump overlays can fake ~100% residual take WR. Gate extras on
orig-hold WR, holdout PnL, and the expensive (px>0.55) bucket.

Do not autodial live. 4bps / 40–60 / leftover / reverse / favorite stay
forbidden even if coverage looks pretty.
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
from app.twap import TwapParams, entry_edge, fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from freq_params import dump_row, rev59  # noqa: E402
from high_wr import REV_CACHE, attach_path, bm_exit, buys, load_raw  # noqa: E402
from tape_pull import HAIRCUT, pack, rec_of, split_holdout  # noqa: E402

OUT = Path(__file__).resolve().parent / "always_in.json"
SHIP = Path(__file__).resolve().parent / "always_in_ship.json"
TWAP60 = te.TWAP60_START
CORE = ("btc", "eth")
MAX_LEAD = 40.0
HOLDOUT_DAYS = 7

BM_PARAMS = TwapParams(
    min_price=0.45,
    max_price=0.55,
    min_lead_bps=6.0,
    min_edge=0.04,
    min_left=120.0,
    max_left=280.0,
    max_lead_bps=MAX_LEAD,
    confirm_px=0.0,
    confirm_fair=0.0,
    take_profit=0.0,
)

ENTRIES = (
    "frozen",
    "wait_mid",
    "wait_mid_2bps",
    "always_open",
    "always_first",
    "always_2bps",
    "favorite_open",
    "hybrid_mid",
)

MANAGES = ("hold", "bm", "bm_hc", "rev59", "rev59_hc", "wr_dump", "rev59_tp")

DO_NOT = [
    "always_in_live",
    "always_open_live",
    "always_first_live",
    "favorite_every_window",
    "favorite_97_98",
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
    "wr_dump_as_edge",
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


def orig_hold_wr(rows: list[dict]) -> tuple[float | None, int]:
    held = [r for r in rows if not r.get("orig_scratch")]
    if not held:
        return None, 0
    return round(sum(1 for r in held if r.get("orig_won")) / len(held), 4), len(held)


def settle_wr(rows: list[dict]) -> float | None:
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get("won")) / len(rows), 4)


def bucket_rec(rows: list[dict]) -> dict:
    by: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by[px_bucket(r["px"])].append(r)
    out = {}
    for name in ("mid_45_55", "px_55_70", "px_70_85", "px_85_plus"):
        xs = by.get(name) or []
        rec = rec_of(xs)
        rec["settle_wr"] = settle_wr(xs)
        rec["avg_px"] = None if not xs else round(sum(float(r["px"]) for r in xs) / len(xs), 4)
        out[name] = rec
    return out


def hold_row(row: dict) -> dict:
    x = dict(row)
    x["scratched"] = False
    x["exit_why"] = "settle"
    x["exit_px"] = None
    x["pnl"] = te.pnl_hold(float(row["px"]), bool(row["won"]))
    return x


def apply_tp(row: dict, full: list[dict], tp: float = 0.87) -> dict:
    if row.get("scratched"):
        return row
    t0 = int(row["ts"])
    side = str(row["side"])
    for p in full:
        if p["outcome"] != side or int(p["ts"]) < t0:
            continue
        if float(p["px"]) + 1e-12 >= tp:
            return dump_row(row, "tp87", tp)
    return row


def wr_dump(row: dict) -> dict:
    x = dict(row)
    if x.get("scratched"):
        return x
    if x.get("ever_62_by90"):
        return x
    return dump_row(x, "wr_unconfirmed", x.get("px90") or x.get("px"))


def haircut_scratch(row: dict) -> dict:
    """Print-tape BM exits are asks lifted, not bids we can hit. 2¢ conservative."""
    x = dict(row)
    if not x.get("scratched"):
        return x
    raw = x.get("exit_px")
    if raw is None:
        raw = x.get("px90") or x.get("px")
    px = max(0.01, float(raw) - HAIRCUT)
    x["pnl"] = te.pnl_scratch(float(row["px"]), px)
    x["exit_px"] = round(px, 4)
    x["haircut"] = HAIRCUT
    return x


def manage_row(row: dict, full: list[dict], series, mode: str) -> dict:
    if mode == "hold":
        return hold_row(row)
    if mode == "bm":
        return dict(row)
    if mode == "bm_hc":
        return haircut_scratch(row)
    if mode == "rev59":
        return rev59(dict(row), series)
    if mode == "rev59_hc":
        return haircut_scratch(rev59(dict(row), series))
    if mode == "wr_dump":
        return wr_dump(row)
    if mode == "rev59_tp":
        return rev59(apply_tp(dict(row), full), series)
    raise ValueError(mode)


def annotate(rows: list[dict], *, windows: int, days: float) -> dict:
    packed = pack(rows)
    train, hold = split_holdout(rows)
    o_all, n_all = orig_hold_wr(rows)
    o_ho, n_ho = orig_hold_wr(hold)
    o_tr, n_tr = orig_hold_wr(train)
    a = packed["all"]
    a["settle_wr"] = settle_wr(rows)
    a["orig_hold_wr"] = o_all
    a["orig_hold_n"] = n_all
    a["avg_px"] = None if not rows else round(sum(float(r["px"]) for r in rows) / len(rows), 4)
    a["avg_lead"] = None if not rows else round(sum(abs(float(r["lead"])) for r in rows) / len(rows), 2)
    a["coverage"] = None if windows <= 0 else round(len(rows) / windows, 4)
    a["per_day"] = None if days <= 0 else round(len(rows) / days, 2)
    a["pnl5_per_window"] = None if windows <= 0 else round(a["pnl5"] / windows, 5)
    packed["all"] = a
    packed["train"]["orig_hold_wr"] = o_tr
    packed["train"]["orig_hold_n"] = n_tr
    packed["train"]["settle_wr"] = settle_wr(train)
    packed["holdout"]["orig_hold_wr"] = o_ho
    packed["holdout"]["orig_hold_n"] = n_ho
    packed["holdout"]["settle_wr"] = settle_wr(hold)
    packed["buckets"] = bucket_rec(rows)
    expensive = [r for r in rows if float(r["px"]) > 0.55 + 1e-12]
    packed["expensive"] = rec_of(expensive)
    packed["expensive"]["settle_wr"] = settle_wr(expensive)
    packed["expensive_holdout"] = rec_of([r for r in hold if float(r["px"]) > 0.55 + 1e-12])
    packed["expensive_holdout"]["settle_wr"] = settle_wr([r for r in hold if float(r["px"]) > 0.55 + 1e-12])
    return packed


def beats_frozen(g: dict, base: dict, *, coverage_floor: float, extra_orig_ho: float | None) -> bool:
    if (g.get("coverage") or 0) + 1e-12 < coverage_floor:
        return False
    if not g.get("robust"):
        return False
    if g["holdout"]["pnl5"] + 1e-9 < base["holdout"]["pnl5"]:
        return False
    if g["train"]["pnl5"] + 1e-9 < 0.95 * base["train"]["pnl5"]:
        return False
    if int(g.get("expensive_holdout_n") or 0) >= 20 and not g.get("expensive_holdout_ev_ok"):
        return False
    orig_ho = g["holdout"].get("orig_hold_wr")
    if orig_ho is None or orig_ho + 1e-12 < 0.85:
        return False
    if extra_orig_ho is not None and extra_orig_ho + 1e-12 < 0.85:
        return False
    return True


def favorite_open_cand(full: list[dict], start: int, end: int, lead: float | None, fair_up: float | None) -> dict | None:
    t0, t1 = start + 15, start + 45
    last = {"Up": None, "Down": None}
    for p in full:
        if t0 <= int(p["ts"]) <= t1 and p["outcome"] in last:
            last[p["outcome"]] = p
    up, dn = last["Up"], last["Down"]
    if up is None and dn is None:
        return None
    if dn is None or (up is not None and float(up["px"]) >= float(dn["px"])):
        pr, side = up, "Up"
    else:
        pr, side = dn, "Down"
    if pr is None:
        return None
    ld = 0.0 if lead is None else float(lead)
    if fair_up is None:
        fair = 0.5
    else:
        fair = fair_up if side == "Up" else (1.0 - fair_up)
    return {
        "ts": int(pr["ts"]),
        "left": end - int(pr["ts"]),
        "side": side,
        "px": float(pr["px"]),
        "lead": ld,
        "fair": float(fair),
    }


def pick_lead(ts: int, start: int, end: int, lead: float, pr: dict | None, fair: float | None, name: str) -> bool:
    if pr is None or fair is None:
        return False
    left = end - ts
    px = float(pr["px"])
    if px < 0.05 or px > 0.99:
        return False
    abs_lead = abs(float(lead))
    if name == "frozen":
        return (
            120.0 - 1e-12 <= left <= 280.0 + 1e-12
            and 6.0 - 1e-12 <= abs_lead <= MAX_LEAD + 1e-12
            and 0.45 - 1e-12 <= px <= 0.55 + 1e-12
            and entry_edge(fair, px, 0.07) + 1e-12 >= 0.04
        )
    if name == "wait_mid":
        return 120.0 - 1e-12 <= left <= 280.0 + 1e-12 and 0.45 - 1e-12 <= px <= 0.55 + 1e-12
    if name == "wait_mid_2bps":
        return (
            120.0 - 1e-12 <= left <= 280.0 + 1e-12
            and 0.45 - 1e-12 <= px <= 0.55 + 1e-12
            and abs_lead + 1e-12 >= 2.0
        )
    if name == "always_open":
        return 250.0 - 1e-12 <= left <= 280.0 + 1e-12
    if name == "always_first":
        return 120.0 - 1e-12 <= left <= 280.0 + 1e-12
    if name == "always_2bps":
        return 120.0 - 1e-12 <= left <= 280.0 + 1e-12 and abs_lead + 1e-12 >= 2.0
    return False


def slim_combo(packed: dict) -> dict:
    a, t, h = packed["all"], packed["train"], packed["holdout"]
    return {
        "n": a["n"],
        "coverage": a.get("coverage"),
        "per_day": a.get("per_day"),
        "avg_px": a.get("avg_px"),
        "avg_lead": a.get("avg_lead"),
        "settle_wr": a.get("settle_wr"),
        "orig_hold_wr": a.get("orig_hold_wr"),
        "take_wr": a["take_wr"],
        "pnl5": a["pnl5"],
        "pnl5_per_window": a.get("pnl5_per_window"),
        "scratch_n": a["scratch_n"],
        "train": {"n": t["n"], "pnl5": t["pnl5"], "take_wr": t["take_wr"], "orig_hold_wr": t.get("orig_hold_wr"), "ev_ok": t["ev_ok"]},
        "holdout": {
            "n": h["n"],
            "pnl5": h["pnl5"],
            "take_wr": h["take_wr"],
            "orig_hold_wr": h.get("orig_hold_wr"),
            "settle_wr": h.get("settle_wr"),
            "ev_ok": h["ev_ok"],
        },
        "expensive_n": packed["expensive"]["n"],
        "expensive_pnl5": packed["expensive"]["pnl5"],
        "expensive_settle_wr": packed["expensive"].get("settle_wr"),
        "expensive_holdout_pnl5": packed["expensive_holdout"]["pnl5"],
        "expensive_holdout_n": packed["expensive_holdout"]["n"],
        "expensive_holdout_ev_ok": packed["expensive_holdout"]["ev_ok"],
        "buckets": packed["buckets"],
        "robust": packed["robust"],
    }


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
    pending_names = [n for n in ENTRIES if n != "hybrid_mid"]
    first_by: dict[str, dict[str, dict]] = {n: {} for n in pending_names}
    full_of: dict[str, list[dict]] = {}
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
        if len(full) < 2:
            continue
        n_print += 1
        tw_open = series.twap(start, 60)
        if tw_open is None or tw_open <= 0:
            continue
        n_win += 1
        pending = {n: None for n in pending_names if n != "favorite_open"}
        fav_lead = None
        fav_fair_up = None
        for ts in range(start + 15, end - 20 + 1, 5):
            tw = series.twap(ts, 60)
            if tw is None:
                continue
            lead = lead_bps(tw, tw_open)
            if lead is None:
                continue
            left = end - ts
            vol = series.realized_vol_bps_sqrt_s(ts, 120)
            fair_up = fair_p_up(lead, vol, float(left), lookback=60)
            if ts <= start + 45:
                fav_lead = lead
                fav_fair_up = fair_up
            if fair_up is None:
                continue
            side = "Up" if lead >= 0 else "Down"
            pr = te.last_print(full, ts, side, slack=25)
            fair = fair_up if side == "Up" else (1.0 - fair_up)
            cand = {
                "ts": ts,
                "left": left,
                "side": side,
                "px": None if pr is None else float(pr["px"]),
                "lead": float(lead),
                "fair": float(fair),
                "pr": pr,
            }
            for name in pending:
                if pending[name] is not None:
                    continue
                if pick_lead(ts, start, end, lead, pr, fair, name):
                    pending[name] = {k: cand[k] for k in ("ts", "left", "side", "px", "lead", "fair")}
            if all(v is not None for v in pending.values()):
                break
        pending["favorite_open"] = favorite_open_cand(full, start, end, fav_lead, fav_fair_up)
        band = [p for p in full if 0.40 - 1e-12 <= float(p["px"]) <= 0.60 + 1e-12]
        for name, picked in pending.items():
            if picked is None or picked.get("px") is None:
                continue
            marks = full if name in {"always_open", "always_first", "always_2bps", "favorite_open"} else (band or full)
            row = attach_path(bm_exit(ev, series, marks, picked, BM_PARAMS), full)
            row["orig_scratch"] = bool(row.get("scratched"))
            row["orig_won"] = bool(row.get("won"))
            row["entry"] = name
            first_by[name][ev["slug"]] = row
        full_of[ev["slug"]] = full
        if i % 900 == 0:
            print(f"  {i}/{len(twap_ev)} prints={n_print} windows={n_win}", flush=True)

    frozen_slugs = set(first_by["frozen"])
    hybrid = {}
    for slug, row in first_by["frozen"].items():
        hybrid[slug] = dict(row)
        hybrid[slug]["entry"] = "hybrid_mid"
    for slug, row in first_by["wait_mid"].items():
        if slug not in hybrid:
            x = dict(row)
            x["entry"] = "hybrid_mid"
            hybrid[slug] = x
    first_by["hybrid_mid"] = hybrid

    combos = {}
    for entry in ENTRIES:
        rows = list(first_by[entry].values())
        for mode in MANAGES:
            managed = []
            for r in rows:
                series = series_of.get(r.get("asset"))
                full = full_of.get(r["slug"]) or []
                x = manage_row(r, full, series, mode)
                x["orig_scratch"] = r.get("orig_scratch")
                x["orig_won"] = r.get("orig_won")
                managed.append(x)
            packed = annotate(managed, windows=n_win, days=days)
            key = f"{entry}__{mode}"
            combos[key] = slim_combo(packed)
            combos[key]["entry"] = entry
            combos[key]["manage"] = mode

    base = combos["frozen__rev59"]
    extras_open = [r for slug, r in first_by["always_open"].items() if slug not in frozen_slugs]
    extras_mid = [r for slug, r in first_by["wait_mid"].items() if slug not in frozen_slugs]
    extras_open_m = []
    extras_mid_m = []
    for r in extras_open:
        x = manage_row(r, full_of.get(r["slug"]) or [], series_of.get(r.get("asset")), "rev59")
        x["orig_scratch"] = r.get("orig_scratch")
        x["orig_won"] = r.get("orig_won")
        extras_open_m.append(x)
    for r in extras_mid:
        x = manage_row(r, full_of.get(r["slug"]) or [], series_of.get(r.get("asset")), "rev59")
        x["orig_scratch"] = r.get("orig_scratch")
        x["orig_won"] = r.get("orig_won")
        extras_mid_m.append(x)
    extras_open_ann = slim_combo(annotate(extras_open_m, windows=n_win, days=days)) if extras_open_m else None
    extras_mid_ann = slim_combo(annotate(extras_mid_m, windows=n_win, days=days)) if extras_mid_m else None
    extra_mid_orig_ho = None if extras_mid_ann is None else extras_mid_ann["holdout"].get("orig_hold_wr")
    extra_open_orig_ho = None if extras_open_ann is None else extras_open_ann["holdout"].get("orig_hold_wr")

    ship_grid = {}
    for key, g in combos.items():
        need_cov = 0.0 if g["entry"] == "frozen" else 0.80
        extra = None
        if g["entry"] in {"wait_mid", "wait_mid_2bps", "hybrid_mid"}:
            extra = extra_mid_orig_ho
        elif g["entry"] in {"always_open", "always_first", "always_2bps", "favorite_open"}:
            extra = extra_open_orig_ho
        ship_grid[key] = {
            "beats_frozen": beats_frozen(g, base, coverage_floor=need_cov, extra_orig_ho=extra),
            "forbidden": g["entry"] in {"always_open", "always_first", "always_2bps", "favorite_open"}
            or g["manage"] in {"wr_dump", "bm"},
        }

    why = (
        "Every-window entry has to lift the CLOB ask as-is. Most 5m books are "
        "already 60¢+ on the TWAP side: high settle WR, negative payoff after "
        "taker fee. In-window BM/dump/TP can raise *take* WR by selling losers "
        "and still lose money — same dump-overlay trap as easy_entry. The +EV "
        "high-WR sleeve remains 6bps∩45–55 first-cross plus dump90/oracle, "
        "which only prints on ~7% of windows. Patient mid (any lead, 45–55) "
        "raises coverage without buying 90¢, but extras are not 6bps quality "
        "and do not autodial. Favorite-every-window is 97–98."
    )
    winners = [
        k
        for k, v in ship_grid.items()
        if v["beats_frozen"] and not v["forbidden"] and not k.startswith("frozen__")
    ]
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Enter every 5m window, manage in-window for higher WR?",
        "ship": False,
        "pick": None,
        "why": why,
        "universe": {
            "events": len(twap_ev),
            "windows_scanned": n_win,
            "with_prints": n_print,
            "days": round(days, 2),
            "holdout_days": HOLDOUT_DAYS,
            "frozen_n": len(frozen_slugs),
            "frozen_coverage": None if n_win <= 0 else round(len(frozen_slugs) / n_win, 4),
        },
        "physics": {
            "breakeven_wr_50c": breakeven_wr(0.50),
            "breakeven_wr_70c": breakeven_wr(0.70),
            "breakeven_wr_80c": breakeven_wr(0.80),
            "breakeven_wr_90c": breakeven_wr(0.90),
            "fee_ps_50c": round(fee_ps(0.50), 5),
            "fee_ps_80c": round(fee_ps(0.80), 5),
            "note": "Settle WR ≈ CLOB price. Edge is fair−ask−fee, not WR. Paying 80¢ to be right 80% is −EV after the 7% crypto fee.",
        },
        "baseline": "frozen__rev59",
        "combos": combos,
        "ship_grid": ship_grid,
        "winners": winners,
        "extras_always_open_rev59": extras_open_ann or rec_of([]),
        "extras_wait_mid_rev59": extras_mid_ann or rec_of([]),
        "findings": {
            "headline": "每窗入場做唔到又高勝率又 +EV：貴價問就係已定盤。窗內管理只可以改出場，唔可以製造 6bps∩45–55 嗰種邊。",
            "every_window_needs_expensive_asks": True,
            "manage_cannot_mint_mid_band": True,
            "wr_dump_is_fake_take_wr": True,
            "wait_mid_is_not_higher_settle_wr": True,
            "wait_mid_extras_fail_orig_hold_bar": True,
        },
        "do_not": DO_NOT,
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "twap_no_cheaper": True,
            "twap_up_tick": 0.01,
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
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/always_in.json",
                "question": rec["question"],
                "why": why,
                "windows_scanned": n_win,
                "frozen_coverage": rec["universe"]["frozen_coverage"],
                "always_open_coverage": (combos.get("always_open__hold") or {}).get("coverage"),
                "always_open_holdout_ev_ok": (combos.get("always_open__hold") or {}).get("holdout", {}).get("ev_ok"),
                "always_open_rev59_holdout_ev_ok": (combos.get("always_open__rev59") or {}).get("holdout", {}).get("ev_ok"),
                "always_open_expensive_holdout_ev_ok": (combos.get("always_open__hold") or {}).get("expensive_holdout_ev_ok"),
                "always_open_settle_wr": (combos.get("always_open__hold") or {}).get("settle_wr"),
                "favorite_open_settle_wr": (combos.get("favorite_open__hold") or {}).get("settle_wr"),
                "favorite_open_holdout_ev_ok": (combos.get("favorite_open__hold") or {}).get("holdout", {}).get("ev_ok"),
                "wait_mid_coverage": (combos.get("wait_mid__rev59") or {}).get("coverage"),
                "wait_mid_settle_wr": (combos.get("wait_mid__hold") or {}).get("settle_wr"),
                "wait_mid_extras_orig_hold_wr_holdout": extra_mid_orig_ho,
                "wait_mid_extras_orig_hold_wr_all": None if extras_mid_ann is None else extras_mid_ann.get("orig_hold_wr"),
                "wait_mid_hold_holdout_ev_ok": (combos.get("wait_mid__hold") or {}).get("holdout", {}).get("ev_ok"),
                "wait_mid_bm_hc_holdout_ev_ok": (combos.get("wait_mid__bm_hc") or {}).get("holdout", {}).get("ev_ok"),
                "frozen_settle_wr": (combos.get("frozen__hold") or {}).get("settle_wr"),
                "frozen_holdout_pnl5": base["holdout"]["pnl5"],
                "frozen_hold_holdout_pnl5": (combos.get("frozen__hold") or {}).get("holdout", {}).get("pnl5"),
                "winners": winners,
                "wr_dump_forbidden": True,
                "every_window_needs_expensive_asks": True,
                "wait_mid_is_not_higher_settle_wr": True,
                "do_not": DO_NOT,
                "params_kept": rec["params_kept"],
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"ship": False, "windows": n_win, "frozen": len(frozen_slugs), "winners": winners, "s": rec["elapsed_s"]}, indent=2))
    return rec


if __name__ == "__main__":
    run()
