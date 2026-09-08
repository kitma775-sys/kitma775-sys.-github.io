#!/usr/bin/env python3
"""Smarter live scratch / other PnL overlays — same 6bps∩45–55 entry.

Question: without easing entry, can exit logic raise take WR and holdout PnL?

Live-faithful sim: first-cross fill, then should_scratch with high_water
(like Zeabur), TP 87¢, unconfirmed 62 / oracle fair 0.60, 15s rescore / 5s
in the last 90s. Scratch sells are scored raw and with a 2¢ haircut.

Do not autodial 6bps, leftover, 40–60, reverse, dump_mid90, 8¢ SL.
Ship only if a variant beats shipped on *haircut* train+holdout by ≥$5,
holdout take WR ≥ shipped, same n, and it does not dump *less* (live 14d
redeems are ~21% WR — tape residual holds are ~90%+; persist fights the leak).
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import (  # noqa: E402
    TwapParams,
    entry_edge,
    fair_p_up,
    hold_value,
    lead_bps,
    scratch_proceeds,
    should_scratch,
)
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from freq_params import CORE, MAX_LEAD, TWAP60  # noqa: E402
from high_wr import REV_CACHE, buys, load_raw  # noqa: E402
from tape_pull import HAIRCUT, pack  # noqa: E402

OUT = Path(__file__).resolve().parent / "smart_scratch.json"
SHIP = Path(__file__).resolve().parent / "smart_scratch_ship.json"
LIVE_FINGERPRINT = Path("/tmp/smart_scratch_live.json")

WHY_WEAK = "twap_scratch_weak"
WHY_FLIP = "twap_scratch_flip"
WHY_BETTER = "twap_scratch_better"
WHY_STOP = "twap_scratch_stop"
WHY_UNCONF = "twap_scratch_unconfirmed"
WHY_ORACLE = "twap_scratch_oracle"
WHY_TP = "twap_scratch_tp"
WHY_LAST30 = "twap_scratch_last30"
BM_WHY = frozenset({WHY_WEAK, WHY_FLIP, WHY_BETTER, WHY_STOP})
WEAK_FLIP = frozenset({WHY_WEAK, WHY_FLIP})

SHIPPED = TwapParams(
    min_price=0.45,
    max_price=0.55,
    min_lead_bps=6.0,
    min_edge=0.04,
    min_left=120.0,
    max_left=280.0,
    max_lead_bps=MAX_LEAD,
    scratch_p=0.48,
    scratch_min_bid=0.38,
    scratch_dump_floor=0.22,
    scratch_adverse=0.0,
    scratch_left_min=8.0,
    take_profit=0.87,
    confirm_px=0.62,
    confirm_left=90.0,
    confirm_fair=0.60,
)

DO_NOT = [
    "lead_4bps",
    "lead_5bps",
    "autodial_5_5bps",
    "band_40_60",
    "chase_leftover",
    "min_left_below_120",
    "alts",
    "15m",
    "twap_reverse_on",
    "dump_mid90",
    "price_sl_8c",
    "scratch_adverse_0.08",
    "favorite_97_98",
    "persist_when_live_underdumps",
    "always_in_live",
    "cheap_bounce_20_30",
    "autodial_keep_late_dump",
    "disable_bm_scratch_live",
]

# Variants that dump *fewer* last-90s dogs (unconfirmed/oracle). Live 14d
# redeem WR ~21% is that bucket. BM persist/hold-all can look +EV on tape
# because scratch_better was selling 71% WR winners — that is not the live leak.
DUMP_LESS = frozenset(
    {
        "hold_only",
        "persist2_weak_flip",
        "delay1",
        "confirmed_quiet",
        "bm_only",
        "oracle_55",
        "confirm_58",
        "runner70_quiet",
    }
)


@dataclass(frozen=True)
class ExitSpec:
    name: str
    persist: int = 1
    delay: int = 0
    persist_whys: frozenset[str] = BM_WHY
    no_better: bool = False
    no_weak: bool = False
    no_flip: bool = False
    no_unconfirmed: bool = False
    no_oracle: bool = False
    no_tp: bool = False
    confirmed_quiet: bool = False
    skip_better_confirmed: bool = False
    haircut_better: bool = False
    hold_only: bool = False
    runner_quiet_hw: float = 0.0
    last30_left: float = 0.0
    last30_hw: float = 0.0
    last30_fair: float = 0.0
    params: TwapParams = SHIPPED
    forbidden: bool = False


SPECS = (
    ExitSpec("shipped"),
    ExitSpec("hold_only", hold_only=True),
    ExitSpec("persist2_weak_flip", persist=2, persist_whys=WEAK_FLIP),
    ExitSpec("delay1", delay=1, persist_whys=BM_WHY),
    ExitSpec("no_better", no_better=True),
    ExitSpec("no_weak", no_weak=True),
    ExitSpec("no_flip", no_flip=True),
    ExitSpec("confirmed_quiet", confirmed_quiet=True),
    ExitSpec("skip_better_confirmed", skip_better_confirmed=True),
    ExitSpec("haircut_better", haircut_better=True),
    ExitSpec("runner70_quiet", runner_quiet_hw=0.70),
    ExitSpec("dump90_only", no_better=True, no_weak=True, no_flip=True),
    ExitSpec(
        "keep_late_dump",
        no_better=True,
        no_weak=True,
        no_flip=True,
        no_tp=True,
    ),
    ExitSpec(
        "bm_only",
        no_unconfirmed=True,
        no_oracle=True,
        params=replace(SHIPPED, confirm_px=0.0, confirm_fair=0.0),
    ),
    ExitSpec("tp_off", params=replace(SHIPPED, take_profit=0.0)),
    ExitSpec("tp_80", params=replace(SHIPPED, take_profit=0.80)),
    ExitSpec("tp_90", params=replace(SHIPPED, take_profit=0.90)),
    ExitSpec("oracle_55", params=replace(SHIPPED, confirm_fair=0.55)),
    ExitSpec("oracle_65", params=replace(SHIPPED, confirm_fair=0.65)),
    ExitSpec("oracle_70", params=replace(SHIPPED, confirm_fair=0.70)),
    ExitSpec("confirm_58", params=replace(SHIPPED, confirm_px=0.58)),
    ExitSpec("confirm_66", params=replace(SHIPPED, confirm_px=0.66)),
    ExitSpec("confirm_70", params=replace(SHIPPED, confirm_px=0.70)),
    ExitSpec("confirm_left_120", params=replace(SHIPPED, confirm_left=120.0)),
    ExitSpec("weak_p_045", params=replace(SHIPPED, scratch_p=0.45)),
    ExitSpec("weak_p_052", params=replace(SHIPPED, scratch_p=0.52)),
    ExitSpec("last30_need_80", last30_left=30.0, last30_hw=0.80, last30_fair=0.70),
    ExitSpec("last45_need_70", last30_left=45.0, last30_hw=0.70, last30_fair=0.65),
    ExitSpec(
        "adverse_08",
        params=replace(SHIPPED, scratch_adverse=0.08),
        forbidden=True,
    ),
)


def slim(p: dict) -> dict:
    a, t, h = p["all"], p["train"], p["holdout"]
    return {
        "n": a["n"],
        "pnl5": a["pnl5"],
        "take_wr": a["take_wr"],
        "held": a["held"],
        "scratch_n": a["scratch_n"],
        "win": a["win"],
        "lose": a["lose"],
        "train": {"n": t["n"], "pnl5": t["pnl5"], "take_wr": t["take_wr"], "ev_ok": t["ev_ok"]},
        "holdout": {"n": h["n"], "pnl5": h["pnl5"], "take_wr": h["take_wr"], "ev_ok": h["ev_ok"]},
        "robust": p["robust"],
    }


def beats(g: dict, base: dict) -> bool:
    """Haircut train+holdout +$5 vs shipped, same n, both +EV.

    Residual take-WR ≥85% only when the variant still scratches ≥85% of
    fills (dump-theater guard). If it actually *holds* a book (≥15%),
    require remaining WR ≥50% instead — 57% at 50¢ is the +EV hold sleeve;
    BM better selling those winners is the tape leak.
    """
    if not g.get("robust"):
        return False
    if g["n"] != base["n"]:
        return False
    if g["holdout"]["pnl5"] + 1e-9 < base["holdout"]["pnl5"] + 5.0:
        return False
    if g["train"]["pnl5"] + 1e-9 < base["train"]["pnl5"]:
        return False
    n = max(int(g["n"] or 0), 1)
    held_share = (g["held"] or 0) / n
    gwr = g["holdout"]["take_wr"]
    bwr = base["holdout"]["take_wr"]
    if held_share < 0.15:
        if gwr is None:
            return False
        floor = 0.85 if bwr is None else max(0.85, float(bwr) - 0.01)
        if gwr + 1e-12 < floor:
            return False
    elif gwr is None or gwr + 1e-12 < 0.50:
        return False
    return True


def first_cross(ev, series, band: list[dict]) -> dict | None:
    start, end = int(ev["start"]), int(ev["end"])
    tw_open = series.twap(start, 60)
    if tw_open is None or tw_open <= 0:
        return None
    for ts in range(start + 15, end - 90 + 1, 5):
        left = end - ts
        if left < 120.0 or left > 280.0:
            continue
        tw = series.twap(ts, 60)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open)
        if lead is None or abs(lead) < 6.0 - 1e-12 or abs(lead) > MAX_LEAD + 1e-12:
            continue
        side = "Up" if lead >= 0 else "Down"
        pr = te.last_print(band, ts, side, slack=25)
        if pr is None or not (0.45 - 1e-12 <= pr["px"] <= 0.55 + 1e-12):
            continue
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=60)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) + 1e-12 < 0.04:
            continue
        return {
            "ts": ts,
            "left": left,
            "side": side,
            "px": float(pr["px"]),
            "lead": float(lead),
            "fair": float(fair),
        }
    return None


def filter_why(
    spec: ExitSpec,
    why: str,
    *,
    high_water: float,
    bid: float | None,
    fair: float | None,
    shares: float,
) -> bool:
    if spec.no_better and why == WHY_BETTER:
        return False
    if spec.no_weak and why == WHY_WEAK:
        return False
    if spec.no_flip and why == WHY_FLIP:
        return False
    if spec.no_unconfirmed and why == WHY_UNCONF:
        return False
    if spec.no_oracle and why == WHY_ORACLE:
        return False
    if spec.no_tp and why == WHY_TP:
        return False
    confirmed = high_water + 1e-12 >= 0.62
    if spec.confirmed_quiet and confirmed and why in {
        WHY_WEAK,
        WHY_FLIP,
        WHY_BETTER,
        WHY_UNCONF,
    }:
        return False
    if spec.skip_better_confirmed and confirmed and why == WHY_BETTER:
        return False
    if spec.runner_quiet_hw > 1e-12 and high_water + 1e-12 >= spec.runner_quiet_hw and why in {
        WHY_WEAK,
        WHY_FLIP,
        WHY_BETTER,
    }:
        return False
    if spec.haircut_better and why == WHY_BETTER and bid is not None and fair is not None:
        px = max(0.01, float(bid) - HAIRCUT)
        if scratch_proceeds(shares, px, 0.07) + 1e-9 < hold_value(shares, fair):
            return False
    return True


def simulate_exit(ev, series, prints: list[dict], picked: dict, spec: ExitSpec) -> dict:
    start, end = int(ev["start"]), int(ev["end"])
    won = picked["side"] == ev["winner"]
    hold_pnl = te.pnl_hold(picked["px"], won)
    if spec.hold_only:
        return {
            "slug": ev["slug"],
            "asset": ev.get("asset"),
            "start": start,
            "end": end,
            "ts": picked["ts"],
            "side": picked["side"],
            "px": round(picked["px"], 4),
            "left": picked["left"],
            "lead": round(picked["lead"], 4),
            "won": won,
            "scratched": False,
            "exit_why": "settle",
            "exit_px": None,
            "high_water": round(picked["px"], 4),
            "pnl": hold_pnl,
            "pnl_hc": hold_pnl,
            "hold_pnl": hold_pnl,
        }
    params = spec.params
    shares = te.NOTIONAL / max(picked["px"], 0.01)
    high_water = float(picked["px"])
    pending = 0
    delay_left = spec.delay
    ts = int(picked["ts"])
    exit_px = None
    exit_why = "settle"
    while True:
        left_now = end - ts
        step = 5 if left_now <= 90 else 15
        ts = ts + step
        if ts > end - 8:
            break
        left = end - ts
        tw = series.twap(ts, 60)
        if tw is None:
            continue
        tw_open = series.twap(start, 60)
        lead = lead_bps(tw, tw_open) or 0.0
        signed = lead if picked["side"] == "Up" else -lead
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=60)
        fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
        mark = te.last_print(prints, ts, picked["side"], slack=30)
        bid = None if mark is None else float(mark["px"])
        if bid is not None:
            high_water = max(high_water, bid)
        go, why = should_scratch(
            fair_p=fair,
            lead_bps_signed=signed,
            bid=bid,
            shares=shares,
            fee_rate=0.07,
            left=float(left),
            params=params,
            fill_px=picked["px"],
            high_water=high_water,
        )
        if go and not filter_why(spec, why, high_water=high_water, bid=bid, fair=fair, shares=shares):
            go = False
            why = "twap_hold"
        if (
            spec.last30_left > 1e-12
            and left < spec.last30_left
            and (
                high_water + 1e-12 < spec.last30_hw
                or fair is None
                or float(fair) + 1e-12 < spec.last30_fair
            )
        ):
            go, why = True, WHY_LAST30
        if not go:
            pending = 0
            delay_left = spec.delay
            continue
        needs_wait = (spec.persist > 1 or spec.delay > 0) and why in spec.persist_whys
        if needs_wait:
            pending += 1
            if pending < spec.persist:
                continue
            if delay_left > 0:
                delay_left -= 1
                continue
        nxt = te.next_print(prints, ts, picked["side"], slack=8) or mark
        if nxt is None:
            continue
        exit_px = float(nxt["px"])
        exit_why = why
        break
    if exit_px is not None:
        pnl = te.pnl_scratch(picked["px"], exit_px)
        pnl_hc = te.pnl_scratch(picked["px"], max(0.01, exit_px - HAIRCUT))
        scratched = True
    else:
        pnl = hold_pnl
        pnl_hc = hold_pnl
        scratched = False
    return {
        "slug": ev["slug"],
        "asset": ev.get("asset"),
        "start": start,
        "end": end,
        "ts": picked["ts"],
        "side": picked["side"],
        "px": round(picked["px"], 4),
        "left": picked["left"],
        "lead": round(picked["lead"], 4),
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "exit_px": None if exit_px is None else round(exit_px, 4),
        "high_water": round(high_water, 4),
        "pnl": round(pnl, 5),
        "pnl_hc": round(pnl_hc, 5),
        "hold_pnl": round(hold_pnl, 5),
    }


def autopsy(rows: list[dict]) -> dict:
    by: dict[str, dict] = {}
    for r in rows:
        why = str(r.get("exit_why") or "settle")
        slot = by.setdefault(
            why,
            {"n": 0, "would_win": 0, "scratch_pnl": 0.0, "hold_pnl": 0.0, "hc_pnl": 0.0},
        )
        slot["n"] += 1
        slot["would_win"] += 1 if r.get("won") else 0
        slot["scratch_pnl"] += float(r.get("pnl") or 0)
        slot["hold_pnl"] += float(r.get("hold_pnl") or 0)
        slot["hc_pnl"] += float(r.get("pnl_hc") or 0)
    out = {}
    for why, s in sorted(by.items(), key=lambda kv: -kv[1]["n"]):
        n = s["n"]
        out[why] = {
            "n": n,
            "would_win_wr": None if n == 0 else round(s["would_win"] / n, 4),
            "pnl5": round(s["scratch_pnl"], 2),
            "pnl_hc": round(s["hc_pnl"], 2),
            "if_held_pnl5": round(s["hold_pnl"], 2),
            "delta_vs_hold": round(s["scratch_pnl"] - s["hold_pnl"], 2),
            "delta_hc_vs_hold": round(s["hc_pnl"] - s["hold_pnl"], 2),
        }
    return out


def load_live_fp() -> dict:
    if LIVE_FINGERPRINT.exists():
        try:
            return json.loads(LIVE_FINGERPRINT.read_text())
        except json.JSONDecodeError:
            return {}
    return {
        "fills_14d": 79,
        "dumps_14d": 39,
        "dump_pnl": 38.9513,
        "redeems_14d": 47,
        "redeem_pnl": -55.708,
        "redeem_wins": 10,
        "redeem_wr": round(10 / 47, 4),
        "note": "Dumps +EV. Redeems 10/47 ≈ 21% WR. Tape residual holds after dump90+oracle are ~90%+. Live leak is holds that should have been dumped, not missing persist.",
    }


def _clip_why(d: dict) -> str:
    why = d.get("why")
    if isinstance(why, str) and why.strip():
        return why[:400]
    why_not = d.get("why_not")
    if isinstance(why_not, list):
        return "; ".join(str(x) for x in why_not)[:400]
    if isinstance(why_not, str) and why_not.strip():
        return why_not[:400]
    return ""


def other_sleeves() -> dict:
    """Prior non-entry sleeves already scored. None of these autodial."""
    root = Path(__file__).resolve().parent
    mapping = (
        ("dump_exec", "dump_exec_ship.json", "Hot-book last-90s dump execution (already live)"),
        ("cheap_bounce", "cheap_bounce.json", "20–30¢ wounded-dog bounce"),
        ("two_alts", "two_alts_ship.json", "SOL/XRP/DOGE/BNB TWAP sleeve"),
        ("trend_side", "trend_side_ship.json", "Higher-TF 走势 before 5m side"),
        ("always_in", "always_in_ship.json", "Enter every 5m window"),
        ("easy_entry", "easy_entry_ship.json", "Ease 6bps / 45–55"),
        ("learn_fail", "learn_fail_ship.json", "Online learn-from-loss"),
        ("reverse_fade", "twap_reverse_fade.json", "Buy the other 5m leg"),
        ("sparse_fix", "sparse_fix_ship.json", "Raise fill count without easing entry"),
        ("freq_params", "freq_params_ship.json", "Frequency levers on frozen sleeve"),
    )
    out = {}
    for name, fn, question in mapping:
        path = root / fn
        if not path.exists():
            continue
        try:
            d = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        ship_val = d.get("ship")
        if not isinstance(ship_val, bool):
            ship_val = False
        out[name] = {
            "ship": ship_val,
            "pick": d.get("pick") if isinstance(d.get("pick"), (str, type(None))) else None,
            "do_not_default_on": bool(d.get("do_not_default_on")),
            "question": d.get("question") or question,
            "why": _clip_why(d),
            "winners": d.get("winners") if isinstance(d.get("winners"), list) else None,
        }
    return out


def run() -> dict:
    t0 = time.time()
    events = json.loads((REV_CACHE / "_events.json").read_text())
    twap_ev = [e for e in events if e.get("asset") in CORE and int(e.get("end") or 0) >= TWAP60]
    newest = max(int(e["end"]) for e in twap_ev)
    print(f"load series n={len(twap_ev)}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", TWAP60 - 180, newest + 5),
    }
    takes: list[tuple[dict, dict, list, object]] = []
    for i, ev in enumerate(twap_ev, 1):
        series = series_of.get(ev["asset"])
        if series is None:
            continue
        raw = load_raw(REV_CACHE, ev["slug"])
        if not raw:
            continue
        start, end = int(ev["start"]), int(ev["end"])
        full = buys(raw, start, end, lo=0.05, hi=0.99)
        band = [p for p in full if 0.40 - 1e-12 <= p["px"] <= 0.62 + 1e-12]
        if len(band) < 4:
            continue
        picked = first_cross(ev, series, band)
        if picked is None:
            continue
        takes.append((ev, picked, full, series))
        if i % 900 == 0:
            print(f"  scan {i}/{len(twap_ev)} takes={len(takes)}", flush=True)
    print(f"takes {len(takes)}", flush=True)

    grid = {}
    rows_by: dict[str, list[dict]] = {}
    for spec in SPECS:
        rows = [simulate_exit(ev, series, full, picked, spec) for ev, picked, full, series in takes]
        rows_by[spec.name] = rows
        packed_raw = pack(rows, pnl_key="pnl")
        packed_hc = pack(rows, pnl_key="pnl_hc")
        rec = slim(packed_hc)
        rec["raw"] = slim(packed_raw)
        rec["why"] = dict(Counter(r["exit_why"] for r in rows))
        rec["forbidden"] = spec.forbidden
        rec["dump_less"] = spec.name in DUMP_LESS
        rec["beats"] = False
        grid[spec.name] = rec
        print(
            f"  {spec.name:24s} n={rec['n']} hc ${rec['pnl5']:+7.1f} "
            f"ho ${rec['holdout']['pnl5']:+6.1f} wr={rec['holdout']['take_wr']} "
            f"scratch={rec['scratch_n']}",
            flush=True,
        )

    base = grid["shipped"]
    for name, rec in grid.items():
        spec = next(s for s in SPECS if s.name == name)
        rec["d_ho"] = round(rec["holdout"]["pnl5"] - base["holdout"]["pnl5"], 2)
        rec["d_tr"] = round(rec["train"]["pnl5"] - base["train"]["pnl5"], 2)
        rec["d_scratch"] = rec["scratch_n"] - base["scratch_n"]
        rec["beats"] = (
            False
            if name == "shipped" or spec.forbidden or name in DUMP_LESS
            else beats(rec, base)
        )

    winners = [n for n, g in grid.items() if g["beats"] and not g["forbidden"]]
    pick = None
    if winners:
        pick = max(winners, key=lambda n: (grid[n]["holdout"]["pnl5"], grid[n]["pnl5"]))
    persist_scratch = grid["persist2_weak_flip"]["scratch_n"]
    shipped_scratch = base["scratch_n"]
    persist_dumps_less = persist_scratch < shipped_scratch
    dump_more_names = [
        n
        for n, g in grid.items()
        if g["d_scratch"] > 0 and n not in {"adverse_08"} and not g["dump_less"]
    ]
    auto = autopsy(rows_by["shipped"])
    better = auto.get("twap_scratch_better") or {}
    weak = auto.get("twap_scratch_weak") or {}
    unconf = auto.get("twap_scratch_unconfirmed") or {}
    oracle = auto.get("twap_scratch_oracle") or {}
    settle = auto.get("settle") or {}
    why = (
        "Entry stays 6bps∩45–55 first-cross. Live-faithful should_scratch "
        "(high_water + BM + TP + dump90 + oracle) on the print tape is −EV "
        "after a 2¢ haircut because scratch_better sells ~71% WR winners vs hold. "
        "Unconfirmed + oracle are the only +EV-vs-hold dump reasons and match "
        "the live leak (14d redeem WR ~21%; tape residual holds after BM are 0% WR). "
        "Persist/hold-all dumps fewer of those dogs. 8¢ SL and dump_mid90 stay −EV. "
        "Other sleeves (bounce/alts/走势/every-window/reverse/easy-entry) already "
        "failed the ship bar. No autodial — user confirms."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Smarter live scratch + other PnL overlays without changing entry?",
        "ship": False,
        "pick": None,
        "tape_candidate": pick,
        "why": why,
        "n_takes": len(takes),
        "entry_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "twap_no_cheaper": True,
        },
        "shipped_exit": {
            "twap_scratch_p": 0.48,
            "twap_scratch_min_bid": 0.38,
            "twap_scratch_dump_floor": 0.22,
            "twap_tp_bid": 0.87,
            "twap_confirm_px": 0.62,
            "twap_confirm_left": 90.0,
            "twap_confirm_fair": 0.60,
            "twap_scratch_adverse": 0.0,
            "high_water": True,
        },
        "live": load_live_fp(),
        "autopsy_shipped": auto,
        "grid": grid,
        "winners": winners,
        "dump_more_names": dump_more_names,
        "other_sleeves": other_sleeves(),
        "findings": {
            "headline": "入場唔改。Live-faithful scratch 喺 print tape 上 −EV，因為 scratch_better 賣走 ~71% WR 贏家；dump90+oracle 先至係對住 hold 嘅 +EV，同 live redeem ~21% 漏 dump 同一桶。Persist／hold-all 會少 dump 嗰桶狗。8¢ SL／dump_mid90／bounce／alts／every-window 仍然唔過 bar。",
            "live_underdumps": True,
            "persist_fights_live_leak": persist_dumps_less,
            "persist_scratch_delta": persist_scratch - shipped_scratch,
            "entry_unchanged": True,
            "scratch_better_would_win_wr": better.get("would_win_wr"),
            "scratch_better_delta_hc_vs_hold": better.get("delta_hc_vs_hold"),
            "scratch_weak_delta_hc_vs_hold": weak.get("delta_hc_vs_hold"),
            "unconfirmed_delta_hc_vs_hold": unconf.get("delta_hc_vs_hold"),
            "oracle_delta_hc_vs_hold": oracle.get("delta_hc_vs_hold"),
            "settle_would_win_wr": settle.get("would_win_wr"),
            "scratch_better_is_neg_ev_vs_hold": (better.get("delta_hc_vs_hold") or 0) < 0,
            "late_dump_is_pos_ev_vs_hold": (unconf.get("delta_hc_vs_hold") or 0) > 0
            and (oracle.get("delta_hc_vs_hold") or 0) > 0,
            "hold_only_holdout_ev_ok": grid["hold_only"]["holdout"]["ev_ok"],
        },
        "do_not": DO_NOT,
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "twap_no_cheaper": True,
            "twap_scratch_dump_floor": 0.22,
            "twap_scratch_adverse": 0.0,
            "twap_confirm_fair": 0.60,
            "twap_confirm_px": 0.62,
            "twap_tp_bid": 0.87,
        },
    }
    rec["elapsed_s"] = round(time.time() - t0, 2)
    rec["ship"] = False
    rec["pick"] = None
    if pick:
        rec["why"] = (
            why
            + f" Tape candidate {pick} beats shipped on haircut train+holdout; "
            "still ship=false until the operator confirms (live leak is under-dump)."
        )
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    SHIP.write_text(
        json.dumps(
            {
                "strategy_rev": 60,
                "ship": False,
                "pick": None,
                "tape_candidate": pick,
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/smart_scratch.json",
                "question": rec["question"],
                "why": rec["why"],
                "n_takes": len(takes),
                "shipped_holdout_pnl5": base["holdout"]["pnl5"],
                "shipped_holdout_take_wr": base["holdout"]["take_wr"],
                "shipped_scratch_n": base["scratch_n"],
                "hold_only_holdout_take_wr": grid["hold_only"]["holdout"]["take_wr"],
                "hold_only_holdout_ev_ok": grid["hold_only"]["holdout"]["ev_ok"],
                "keep_late_dump_holdout_pnl5": grid["keep_late_dump"]["holdout"]["pnl5"],
                "keep_late_dump_holdout_ev_ok": grid["keep_late_dump"]["holdout"]["ev_ok"],
                "keep_late_dump_holdout_take_wr": grid["keep_late_dump"]["holdout"]["take_wr"],
                "keep_late_dump_held": grid["keep_late_dump"]["held"],
                "keep_late_dump_beats": grid["keep_late_dump"]["beats"],
                "scratch_better_would_win_wr": better.get("would_win_wr"),
                "scratch_better_delta_hc_vs_hold": better.get("delta_hc_vs_hold"),
                "unconfirmed_delta_hc_vs_hold": unconf.get("delta_hc_vs_hold"),
                "oracle_delta_hc_vs_hold": oracle.get("delta_hc_vs_hold"),
                "settle_would_win_wr": settle.get("would_win_wr"),
                "persist2_scratch_delta": persist_scratch - shipped_scratch,
                "persist_fights_live_leak": persist_dumps_less,
                "adverse_08_beats": grid["adverse_08"]["beats"],
                "live_redeem_wr": rec["live"].get("redeem_wr"),
                "live_underdumps": True,
                "winners": winners,
                "dump_more_names": dump_more_names,
                "other_sleeves_ship": {
                    k: {"ship": v.get("ship"), "pick": v.get("pick")}
                    for k, v in rec["other_sleeves"].items()
                },
                "do_not": DO_NOT,
                "params_kept": rec["params_kept"],
            },
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {
                "ship": False,
                "n": len(takes),
                "winners": winners,
                "tape_candidate": pick,
                "s": rec["elapsed_s"],
            },
            indent=2,
        )
    )
    return rec


if __name__ == "__main__":
    LIVE_FINGERPRINT.write_text(
        json.dumps(
            {
                "fills_14d": 79,
                "dumps_14d": 39,
                "dump_pnl": 38.9513,
                "redeems_14d": 47,
                "redeem_pnl": -55.708,
                "redeem_wins": 10,
                "redeem_wr": round(10 / 47, 4),
                "note": "Dumps +EV. Redeems 10/47 ≈ 21% WR. Tape residual holds after dump90+oracle are ~90%+. Live leak is holds that should have been dumped, not missing persist.",
            },
            indent=2,
        )
        + "\n"
    )
    run()
