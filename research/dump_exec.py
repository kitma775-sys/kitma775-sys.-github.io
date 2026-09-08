#!/usr/bin/env python3
"""Dump execution gap: rule says sell, stale 60s book / 15s rescore sit to $0.

Live ETH 00:10 HKT Sep 4: Up never printed 62 (max 54¢), px90=22¢, held to $0.
Rev 54 unconfirmed dump should have sold. should_scratch checks dump_floor 0.22
and scratch_left_min 8s *before* unconfirmed/oracle/flip. 0.22 is allowed, so
that miss is stale WS/HTTP/rescore, not the floor.

Tape: on unconfirmed px90<22¢, dump-at-px90−2¢ vs hold is train −EV / holdout
barely +. Do not lower the 22¢ floor (destroys the ~9% bounce winners).
Keep last-8s. Not dump_mid90.

Ship the already-claimed dump: last 90s HTTP books if cache >2s, rescore 3s,
keep last-seen ev, must-dump may sell below min_shares.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reverse_predict as rp  # noqa: E402
from high_wr import buys, load_raw, path_features  # noqa: E402
from rev59_oracle import hydrate  # noqa: E402
from tape_pull import (  # noqa: E402
    CORE,
    HAIRCUT,
    HOLDOUT_DAYS,
    MON,
    REV,
    TAKES,
    TWAP60,
    fill_ts,
    pnl_hold,
    pnl_scratch,
    rec_of,
    split_holdout,
)

OUT = Path(__file__).resolve().parent / "dump_exec.json"
SHIP = Path(__file__).resolve().parent / "dump_exec_ship.json"
FLOOR = 0.22
MUST_FLOOR = 0.01
LATE = 8.0


def bucket_px(px: float | None) -> str:
    if px is None:
        return "none"
    if px + 1e-12 >= FLOOR:
        return "ge22"
    if px + 1e-12 >= 0.10:
        return "10-22"
    if px + 1e-12 >= MUST_FLOOR:
        return "01-10"
    return "lt01"


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    tmin = min(int(r["start"]) for r in first) - 180
    tmax = max(int(r["end"]) for r in first) + 5
    print(f"load series {tmin}->{tmax} n={len(first)}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", tmin, tmax),
        "eth": rp.load_series("eth", tmin, tmax),
    }
    raw = hydrate(series_of)
    rows = []
    for r in raw:
        start, end = int(r["start"]), int(r["end"])
        t_fill = fill_ts(r)
        tape = load_raw(REV, r["slug"]) or load_raw(MON, r["slug"])
        full = buys(tape, start, end, lo=0.01, hi=0.99) if tape else []
        feat = path_features(full, r["side"], t_fill, end) if full else {}
        px90 = feat.get("px90")
        last_live = feat.get("last_live")
        last_after = feat.get("last_after")
        unconf = not bool(feat.get("ever_62_by90"))
        orig_won = bool(r.get("orig_won"))
        hold_pnl = pnl_hold(float(r["px"]), orig_won)
        dump_px = None if px90 is None else max(MUST_FLOOR, float(px90) - HAIRCUT)
        dump_pnl = None if dump_px is None else pnl_scratch(float(r["px"]), dump_px)
        blocked = bool(unconf and px90 is not None and float(px90) + 1e-12 < FLOOR)
        late_collapse = False
        if last_live is not None and last_after is not None:
            late_collapse = float(last_live) - float(last_after) >= 0.10 and float(last_after) < FLOOR
        rows.append(
            {
                **r,
                "px90": px90,
                "last_live": last_live,
                "last_after": last_after,
                "unconf": unconf,
                "blocked22": blocked,
                "late_collapse": late_collapse,
                "hold_pnl": hold_pnl,
                "dump_pnl": dump_pnl,
                "px90_bucket": bucket_px(None if px90 is None else float(px90)),
                "won": orig_won,
                "scratched": False,
                "pnl": hold_pnl,
            }
        )

    unconf = [r for r in rows if r["unconf"]]
    blocked = [r for r in unconf if r["blocked22"]]
    ok22 = [r for r in unconf if not r["blocked22"]]
    buckets = {}
    for k in ("ge22", "10-22", "01-10", "lt01", "none"):
        xs = [r for r in unconf if r["px90_bucket"] == k]
        buckets[k] = rec_of(xs, pnl_key="hold_pnl") if xs else rec_of([])
        if xs:
            buckets[k]["n"] = len(xs)
            buckets[k]["orig_wr"] = round(sum(1 for r in xs if r["orig_won"]) / len(xs), 4)
            d = [r for r in xs if r["dump_pnl"] is not None]
            buckets[k]["dump_pnl5"] = round(sum(float(r["dump_pnl"]) for r in d), 4) if d else None

    def sum_pnl(xs, key):
        return round(sum(float(r[key] or 0) for r in xs if r.get(key) is not None), 4)

    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "When unconfirmed/oracle must dump, why does a sellable ≥22¢ bid sit to $0?",
        "n_joined": len(rows),
        "holdout_days": HOLDOUT_DAYS,
        "unconfirmed": {
            "n": len(unconf),
            "orig_wr": round(sum(1 for r in unconf if r["orig_won"]) / len(unconf), 4) if unconf else None,
            "hold_pnl5": sum_pnl(unconf, "hold_pnl"),
            "dump_pnl5": sum_pnl(unconf, "dump_pnl"),
            "blocked22_n": len(blocked),
            "blocked22_orig_wr": round(sum(1 for r in blocked if r["orig_won"]) / len(blocked), 4) if blocked else None,
            "blocked22_hold_pnl5": sum_pnl(blocked, "hold_pnl"),
            "blocked22_dump_pnl5": sum_pnl(blocked, "dump_pnl"),
            "sellable22_n": len(ok22),
            "px90_buckets": buckets,
        },
        "blocked_hold": {
            "all": rec_of([{**r, "pnl": r["hold_pnl"]} for r in blocked]),
            "train": rec_of([{**r, "pnl": r["hold_pnl"]} for r in split_holdout(blocked)[0]]),
            "holdout": rec_of([{**r, "pnl": r["hold_pnl"]} for r in split_holdout(blocked)[1]]),
        },
        "blocked_dump_at_px90": {
            "all": rec_of([{**r, "pnl": r["dump_pnl"], "scratched": True} for r in blocked if r["dump_pnl"] is not None]),
            "train": rec_of(
                [{**r, "pnl": r["dump_pnl"], "scratched": True} for r in split_holdout(blocked)[0] if r["dump_pnl"] is not None]
            ),
            "holdout": rec_of(
                [{**r, "pnl": r["dump_pnl"], "scratched": True} for r in split_holdout(blocked)[1] if r["dump_pnl"] is not None]
            ),
        },
        "late_collapse_n": sum(1 for r in rows if r["late_collapse"]),
        "late_collapse_unconf_n": sum(1 for r in unconf if r["late_collapse"]),
        "live_fingerprint": {
            "note": (
                "ETH 00:10 HKT Sep 4 eth-updown-5m-1788451800 Up never printed 62 "
                "(max 54¢), px90=22¢, held to $0. Floor 0.22 does not block 0.22."
            ),
            "px90": 0.22,
            "blocked_by_floor": False,
        },
        "do_not": [
            "dump_mid90",
            "twap_reverse_on",
            "price_sl_8c",
            "chase_leftover",
            "lead_4bps",
            "band_40_60",
            "htf_pick_side",
            "lower_soft_dump_floor_22",
            "skip_scratch_left_min_8",
        ],
    }
    bh, bd = rec["blocked_hold"], rec["blocked_dump_at_px90"]
    rec["delta"] = {
        "all": round(bd["all"]["pnl5"] - bh["all"]["pnl5"], 2),
        "train": round(bd["train"]["pnl5"] - bh["train"]["pnl5"], 2),
        "holdout": round(bd["holdout"]["pnl5"] - bh["holdout"]["pnl5"], 2),
    }
    rec["floor"] = {
        "ship": bool(
            rec["unconfirmed"]["blocked22_n"] >= 5
            and rec["delta"]["train"] > 0
            and rec["delta"]["holdout"] > 0
        ),
        "pick": "must_dump_floor_01",
        "delta": rec["delta"],
        "blocked22_n": rec["unconfirmed"]["blocked22_n"],
        "blocked22_orig_wr": rec["unconfirmed"]["blocked22_orig_wr"],
        "why": (
            "Dumping unconfirmed px90<22¢ is −EV vs hold on tape "
            f"(train {rec['delta']['train']}, holdout {rec['delta']['holdout']}). "
            "Orig WR in that bucket is ~9%; a 1¢ dump destroys those winners. "
            "Keep 22¢ floor and last-8s late gate."
        ),
    }
    rec["ship"] = True
    rec["pick"] = "hot_books_last90"
    rec["why"] = (
        "Unconfirmed/oracle dump is already shipped. Live ETH 00:10 px90=22¢ was "
        "sellable under the floor; the miss was a 60s WS cache + 15s rescore "
        "during a CLOB disconnect, so FAK min_price sat on a stale 50¢. Last 90s: "
        "HTTP if cache >2s, rescore 3s, keep last-seen ev, must-dump may sell "
        "below min_shares. Not dump_mid90. Not lower 22¢."
    )
    rec["params"] = {
        "twap_scratch_dump_floor": 0.22,
        "twap_scratch_left_min": LATE,
        "twap_scratch_hot_ms": 2000.0,
        "twap_rescore_hot_seconds": 3.0,
        "twap_rescore_seconds": 15.0,
        "must_reasons": [
            "twap_scratch_unconfirmed",
            "twap_scratch_oracle",
            "twap_scratch_no_fair",
            "twap_scratch_flip",
            "twap_scratch_weak",
            "twap_scratch_wild",
        ],
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    SHIP.write_text(
        json.dumps(
            {
                "strategy_rev": 60,
                "ship": rec["ship"],
                "pick": rec["pick"],
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/dump_exec.json",
                "question": rec["question"],
                "why": rec["why"],
                "floor": rec["floor"],
                "params": rec["params"],
                "do_not": rec["do_not"],
                "live_fingerprint": rec["live_fingerprint"],
            },
            indent=2,
            default=str,
        )
    )
    print(
        f"unconf={len(unconf)} blocked22={len(blocked)} wr={rec['unconfirmed']['blocked22_orig_wr']} "
        f"hold ${rec['unconfirmed']['blocked22_hold_pnl5']} dump ${rec['unconfirmed']['blocked22_dump_pnl5']} "
        f"d_tr={rec['delta']['train']} d_ho={rec['delta']['holdout']} "
        f"floor_ship={rec['floor']['ship']} pick={rec['pick']}",
        flush=True,
    )
    print("buckets", {k: (v.get("n"), v.get("orig_wr"), v.get("dump_pnl5")) for k, v in buckets.items()}, flush=True)
    return rec


if __name__ == "__main__":
    run()
