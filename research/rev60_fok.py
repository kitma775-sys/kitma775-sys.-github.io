#!/usr/bin/env python3
"""Rev 60: convert FOK 殺單 without leftover chase or WR cut.

Live post-Rev 56 BTC+ETH still dies on:
  fok_short          size gone at the locked limit after 250ms
  twap_no_up_requote delayed book is 1¢ richer, still 45–55 — we kill
  unmatched FAK      confirm passed, CLOB itode "no orders found"
  twap_no_cheaper    leftover 45¢ — keep killing

Tape first-cross already *assumes* the 6bps 45–55 print fills. Live FOK is
why realized n << tape n. Filling at first or first+1 tick is the same
side / same window, so held WR does not change; win PnL shaves ~1¢.

Do not: skip 250ms delay, chase cheaper, 4bps, 40–60, min_left<120, reverse,
dump_mid90, 8¢ SL, 2+ tick walk (can leave the 45–55 band from 0.54).
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import cheaper_than_first  # noqa: E402
import high_wr as hw  # noqa: E402
import twap_engine as te  # noqa: E402
from tape_pull import CORE, HOLDOUT_DAYS, TAKES, TWAP60  # noqa: E402

OUT = Path(__file__).resolve().parent / "rev60_fok.json"
TICK = 0.01
CHEAP_EPS = 0.005
LIVE = Path("/tmp/live_fok3d.json")


def pnl_hold(px: float, won: bool, notional: float = 5.0) -> float:
    shares = notional / max(px, 0.01)
    fee = taker_fee(shares, px, 0.07)
    if won:
        return round(shares * (1.0 - px) - fee, 5)
    return round(-shares * px - fee, 5)


def buys_side(raw: list, start: int, end: int, side: str) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or "BUY").upper() != "BUY":
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc != side:
            continue
        try:
            px = float(t.get("px") or t.get("price") or 0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start - 2 or ts > end + 2:
            continue
        if px < 0.05 or px > 0.99:
            continue
        out.append({"ts": ts, "px": px})
    out.sort(key=lambda x: x["ts"])
    return out


def delayed(row: dict, raw: list) -> dict:
    t0 = int(row["ts"]) if row.get("ts") else int(row["end"]) - int(row["left"])
    px0 = float(row["px"])
    side = str(row["side"])
    after = [p for p in raw if p["ts"] >= t0]
    cheap = None
    up1 = None
    up2 = None
    same = None
    walked = None
    for p in after:
        ts, px = int(p["ts"]), float(p["px"])
        if cheaper_than_first(px, px0, CHEAP_EPS):
            if cheap is None:
                cheap = ts
            continue
        if px > 0.55 + 1e-12:
            if walked is None:
                walked = ts
            continue
        if abs(px - px0) <= CHEAP_EPS + 1e-12:
            if same is None and ts > t0:
                same = ts
            continue
        if px0 + 1e-12 < px <= px0 + TICK + 1e-12:
            if up1 is None:
                up1 = ts
            continue
        if px0 + TICK + 1e-12 < px <= px0 + 2 * TICK + 1e-12 and px <= 0.55 + 1e-12:
            if up2 is None:
                up2 = ts
    return {
        "t0": t0,
        "px0": px0,
        "dt_same": None if same is None else same - t0,
        "dt_up1": None if up1 is None else up1 - t0,
        "dt_up2": None if up2 is None else up2 - t0,
        "dt_cheap": None if cheap is None else cheap - t0,
        "dt_walk": None if walked is None else walked - t0,
        "up1_1s": up1 is not None and up1 - t0 <= 1 and (cheap is None or cheap >= up1),
        "up1_before_cheap": up1 is not None and (cheap is None or cheap > up1),
        "same_1s": same is not None and same - t0 <= 1,
        "cheap_1s": cheap is not None and cheap - t0 <= 1,
        "walk_1s": walked is not None and walked - t0 <= 1,
        "up2_first": up2 is not None and (up1 is None or up2 < up1) and (cheap is None or cheap > up2),
    }


def frac(rows: list[dict], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get(key)) / len(rows), 4)


def live_stats() -> dict:
    if not LIVE.exists():
        return {"n": 0}
    rows = json.loads(LIVE.read_text())
    rev56 = datetime(2026, 9, 1, 14, 14, tzinfo=timezone.utc).timestamp()
    core = [
        r
        for r in rows
        if str(r.get("slug") or "").split("-")[0] in CORE and float(r.get("ts") or 0) >= rev56
    ]
    fok = Counter(r.get("fok") for r in core if r.get("st") == "fok_killed")
    err = Counter()
    for r in core:
        if r.get("st") != "error":
            continue
        d = str(r.get("detail") or "").lower()
        if "no orders found" in d:
            err["unmatched"] += 1
        elif "trading is disabled" in d:
            err["disabled"] += 1
        else:
            err["other"] += 1
    by: dict[str, list] = {}
    for r in core:
        by.setdefault(r["slug"], []).append(r)
    dead_short = dead_up = dead_cheap = dead_unm = 0
    fill_short = fill_up = fill_unm = 0
    for seq in by.values():
        foks = [x for x in seq if x.get("st") == "fok_killed"]
        fills = [x for x in seq if x.get("st") == "filled"]
        reasons = [x.get("fok") for x in foks]
        unmatched = [
            x
            for x in seq
            if x.get("st") == "error" and "no orders found" in str(x.get("detail") or "").lower()
        ]
        if "fok_short" in reasons:
            if fills:
                fill_short += 1
            else:
                dead_short += 1
        if "twap_no_up_requote" in reasons:
            if fills:
                fill_up += 1
            else:
                dead_up += 1
        if "twap_no_cheaper" in reasons and not fills:
            dead_cheap += 1
        if unmatched:
            if fills:
                fill_unm += 1
            else:
                dead_unm += 1
    return {
        "n": len(core),
        "slugs": len(by),
        "filled": sum(1 for r in core if r.get("st") == "filled"),
        "fok_killed": sum(1 for r in core if r.get("st") == "fok_killed"),
        "fok_reasons": dict(fok),
        "error_kinds": dict(err),
        "slugs_fok_short_dead": dead_short,
        "slugs_fok_short_later_fill": fill_short,
        "slugs_up_requote_dead": dead_up,
        "slugs_up_requote_later_fill": fill_up,
        "slugs_unmatched_dead": dead_unm,
        "slugs_unmatched_later_fill": fill_unm,
        "slugs_no_cheaper_dead": dead_cheap,
        "note": "1-tick up + same-limit reconfirm targets short/up/unmatched. no_cheaper stays dead.",
    }


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    stats = []
    rows = []
    for r in first:
        row = dict(r)
        row["ts"] = int(r["ts"]) if r.get("ts") else int(r["end"]) - int(r["left"])
        rows.append(row)
        raw = hw.load_raw(hw.REV_CACHE, r["slug"]) or hw.load_raw(hw.MONTH_CACHE, r["slug"])
        side = str(r["side"])
        band = buys_side(raw, int(r["start"]), int(r["end"]), side)
        feat = delayed(row, band)
        px0 = float(r["px"])
        px1 = min(0.55, round(px0 + TICK, 4))
        won = bool(r["won"])
        feat["pnl0"] = pnl_hold(px0, won)
        feat["pnl1"] = pnl_hold(px1, won)
        feat["won"] = won
        feat["scratched"] = bool(r.get("scratched"))
        feat["end"] = int(r["end"])
        feat["orig_pnl"] = float(r.get("pnl") or 0)
        stats.append(feat)
    packed = hw.pack(rows)

    def held_wr(xs):
        h = [s for s in xs if not s.get("scratched")]
        if not h:
            return None
        return round(sum(1 for s in h if s["won"]) / len(h), 4)

    pnl0 = round(sum(s["pnl0"] for s in stats), 2)
    pnl1 = round(sum(s["pnl1"] for s in stats), 2)
    newest = max(s["end"] for s in stats)
    cut = newest - HOLDOUT_DAYS * 86400
    live = live_stats()
    up1 = frac(stats, "up1_1s")
    cheap1 = frac(stats, "cheap_1s")
    # Convertible: delayed book is +1 tick before leftover, still in band.
    # These are tape takes we already count; live FOK often misses them.
    conv = [s for s in stats if s.get("up1_1s")]
    ship = bool(
        packed.get("robust")
        and packed["holdout"].get("ev_ok")
        and (cheap1 or 0) > 0.15
        and (up1 or 0) >= 0.08
        and pnl1 > 0
        and (held_wr(stats) or 0) >= 0.80
        and (live.get("slugs_fok_short_dead") or 0) + (live.get("slugs_unmatched_dead") or 0) >= 5
    )
    rec = {
        "strategy_rev": 60,
        "ship": ship,
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Convert FOK 殺單 without cutting first-cross take WR.",
        "answer": (
            "Allow 1-tick UP requote and size-walk (still ≤55¢, never cheaper than first-cross). "
            "On live unmatched FAK, re-confirm the book with delay=0 and send one more same-sleeve FAK. "
            f"Tape: {up1:.0%} of first-cross prints reprint 1¢ richer within 1s before leftover; "
            f"{cheap1:.0%} print leftover within 1s so keep 250ms + no_cheaper. "
            "Held WR is the side, not the cent — +1 tick does not flip Up/Down. "
            "Worst-case fill every take 1¢ worse still +EV. Do not 2-tick (0.54→0.56 leaves the band)."
        ),
        "tape": {
            "n": packed["all"].get("n"),
            "take_win_rate": packed["all"].get("take_win_rate"),
            "held_wr_side": held_wr(stats),
            "train_wr": packed["train"].get("take_win_rate"),
            "holdout_wr": packed["holdout"].get("take_win_rate"),
            "robust": packed.get("robust"),
            "pnl_fill_at_first": pnl0,
            "pnl_fill_at_plus_1tick": pnl1,
            "delta_pnl_all_plus1": round(pnl1 - pnl0, 2),
            "holdout_n": packed["holdout"].get("n"),
        },
        "delay_path": {
            "n": len(stats),
            "up1_within_1s_before_cheap": up1,
            "same_px_within_1s": frac(stats, "same_1s"),
            "cheap_within_1s": cheap1,
            "walk_out_within_1s": frac(stats, "walk_1s"),
            "up2_first": frac(stats, "up2_first"),
            "convertible_n": len(conv),
            "convertible_train": sum(1 for s in conv if s["end"] < cut),
            "convertible_holdout": sum(1 for s in conv if s["end"] >= cut),
            "dt_up1_hist": dict(Counter(min(int(s["dt_up1"]), 8) for s in stats if s.get("dt_up1") is not None)),
        },
        "live_btc_eth_since_rev56": live,
        "do_not": [
            "chase_cheaper_leftover",
            "skip_fok_delay",
            "up_requote_2ticks",
            "bps_4",
            "band_40_60",
            "min_left_below_120",
            "dump_mid90",
            "twap_reverse_on",
            "price_sl_8c",
            "turn_taker_fok_off",
            "flip_live_trading",
        ],
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str) + "\n")
    print(
        f"ship={ship} n={len(stats)} up1_1s={up1} cheap1={cheap1} same1={frac(stats,'same_1s')} "
        f"pnl0={pnl0} pnl1={pnl1} wr={held_wr(stats)} live_short_dead={live.get('slugs_fok_short_dead')} "
        f"unm_dead={live.get('slugs_unmatched_dead')}",
        flush=True,
    )
    return rec


if __name__ == "__main__":
    run()
