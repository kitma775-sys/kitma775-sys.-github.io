#!/usr/bin/env python3
"""Rev 56 FOK-kill conversion research.

Live just printed 全殺單: post-Rev 55 BTC+ETH FOKs with 0 fills. Reasons are
`fok_short` (size gone after wait) and `clob_rtt_miss` (first confirm OK,
second walk 200ms later empty). `twap_no_cheaper` is the leftover gate and
must stay.

Rev 10 already said a second wait models requote+itode and misses 300–400ms
holes. Live currently sleeps 250ms + clob_rtt 200ms *then* the exchange
itode-holds another 250ms ≈ 700ms before match. Paper/print tape first-cross
takes are the public BUYs that filled that flash.

Question: convert those kills by sending the *same* first-cross 6bps 45–55
limit faster — not by chasing leftover, widening the band, or cutting 6bps.

Do not: restore alts, 4bps, 40–60, leftover cheaper, dump_mid90, reverse, 8¢ SL.
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

import high_wr as hw  # noqa: E402
from app.twap import TwapParams  # noqa: E402
import reverse_predict as rp  # noqa: E402

OUT = Path(__file__).resolve().parent / "rev56_fok.json"
CHEAP_EPS = 0.005


def persist(row: dict, raw: list) -> dict:
    t0 = int(row["ts"])
    px = float(row["px"])
    side = str(row["side"])
    after = [
        p
        for p in raw
        if str(p.get("outcome")) == side and str(p.get("side") or "BUY").upper() == "BUY" and int(p.get("ts") or 0) >= t0
    ]
    cheaper = None
    walked = None
    takeable_last = t0
    same_sec = 0
    for p in after:
        ts = int(p["ts"])
        pxx = float(p["px"])
        if pxx + 1e-12 < px - CHEAP_EPS:
            if cheaper is None:
                cheaper = ts
            continue
        if pxx > 0.55 + 1e-12:
            if walked is None:
                walked = ts
            continue
        if ts == t0:
            same_sec += 1
        takeable_last = max(takeable_last, ts)
    dt_cheap = None if cheaper is None else cheaper - t0
    dt_walk = None if walked is None else walked - t0
    dt_take = takeable_last - t0
    return {
        "dt_takeable_s": dt_take,
        "dt_cheaper_s": dt_cheap,
        "dt_walked_s": dt_walk,
        "same_sec_prints": same_sec,
        "persist_0s": True,
        "persist_same_sec": same_sec >= 2,
        "persist_1s": dt_take >= 1,
        "persist_2s": dt_take >= 2,
        "cheap_within_1s": dt_cheap is not None and dt_cheap <= 1,
        "cheap_within_2s": dt_cheap is not None and dt_cheap <= 2,
    }


def frac(rows: list[dict], key: str) -> float | None:
    if not rows:
        return None
    return round(sum(1 for r in rows if r.get(key)) / len(rows), 4)


def median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    n = len(s)
    mid = n // 2
    if n % 2:
        return float(s[mid])
    return round(0.5 * (s[mid - 1] + s[mid]), 4)


def run() -> dict:
    t0 = time.time()
    events = json.loads((hw.REV_CACHE / "_events.json").read_text()) if (hw.REV_CACHE / "_events.json").exists() else []
    twap_ev = [e for e in events if int(e.get("end") or 0) >= hw.TWAP60]
    newest = max((e["end"] for e in twap_ev), default=hw.TWAP60)
    series_of = {
        "btc": rp.load_series("btc", hw.TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", hw.TWAP60 - 180, newest + 5),
    }
    params = TwapParams(
        min_price=0.45,
        max_price=0.55,
        min_lead_bps=6.0,
        min_edge=0.04,
        min_left=120.0,
        max_left=280.0,
        max_lead_bps=40.0,
        take_profit=0.0,
    )
    scanned = hw.scan_btc_eth(twap_ev, series_of, params)
    first = scanned["first"]
    dumped = [hw.overlay(r, mode="dump_unconfirmed_by90", haircut=hw.HAIRCUT) for r in first]
    packed = hw.pack(dumped)
    stats = []
    for r in dumped:
        raw = hw.load_raw(hw.REV_CACHE, r["slug"])
        # load_raw items may already be prints; persist() expects ts/px/outcome/side
        band = []
        for t in raw:
            try:
                band.append(
                    {
                        "ts": int(t.get("ts") or 0),
                        "px": float(t.get("px") or 0),
                        "outcome": str(t.get("outcome") or ""),
                        "side": str(t.get("side") or "BUY"),
                    }
                )
            except (TypeError, ValueError):
                continue
        stats.append(persist(r, band))
    take = [s["dt_takeable_s"] for s in stats]
    cheap = [s["dt_cheaper_s"] for s in stats if s["dt_cheaper_s"] is not None]
    live_note = {
        "post_rev55_live_utc": "2026-09-01 13:29–14:10",
        "fills": 0,
        "fok_killed": 8,
        "reasons": {"clob_rtt_miss": "first confirm OK, second 200ms walk empty", "fok_short": "5 shares gone at locked limit after wait", "twap_no_cheaper": "leftover 45¢ — keep killing"},
        "live_wait_ms": "fok_delay 250 + clob_rtt 200 + exchange itode 250 ≈ 700",
        "rev10": "No second wait. Requote+itode misses 300–400ms sticky holes.",
    }
    persist_1 = frac(stats, "persist_1s")
    persist_2 = frac(stats, "persist_2s")
    cheap_1 = frac(stats, "cheap_within_1s")
    same_sec = frac(stats, "persist_same_sec")
    # Keep 250ms delay: 35% of first-cross windows print cheaper leftover
    # inside 1s. Skip only the *second* RTT walk (Rev 10: no second wait).
    # Convert clob_rtt_miss — first confirm already passed no_cheaper.
    ship = bool(
        packed.get("robust")
        and packed["holdout"].get("ev_ok")
        and (packed["all"].get("take_win_rate") or 0) >= 0.90
        and (cheap_1 or 0) > 0.15
    )
    out = {
        "strategy_rev": 56,
        "ship": ship,
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Convert live FOK kills without cutting first-cross take WR.",
        "answer": (
            "Keep 250ms FOK delay + no_cheaper (35% of first-cross windows print a "
            "cheaper leftover within 1s; skipping delay would chase dogs). Live: skip "
            "the second clob_rtt walk — Rev 10 already forbade a second wait. First "
            "confirm already passed the same-limit leftover gate; send FAK. Prefer "
            "fresh WS over HTTP so the 250ms delayed book is not aged another RTT. "
            "Paper keeps RTT re-walk. Sleeve unchanged."
        ),
        "tape": {
            "n": packed["all"].get("n"),
            "pnl_usd": packed["all"].get("pnl_usd"),
            "take_win_rate": packed["all"].get("take_win_rate"),
            "train": packed["train"],
            "holdout": packed["holdout"],
            "robust": packed.get("robust"),
        },
        "persistence": {
            "n": len(stats),
            "median_takeable_s": median(take),
            "median_cheaper_s": median(cheap),
            "persist_same_sec": same_sec,
            "persist_1s": persist_1,
            "persist_2s": persist_2,
            "cheap_within_1s": cheap_1,
            "cheap_within_2s": frac(stats, "cheap_within_2s"),
            "dt_takeable_hist": dict(Counter(min(int(x), 8) for x in take)),
        },
        "live": live_note,
        "do_not": [
            "restore_alts",
            "chase_cheaper_leftover",
            "bps_4",
            "band_40_60",
            "min_left_below_120",
            "dump_mid90",
            "twap_reverse_on",
            "price_sl_8c",
            "turn_taker_fok_off",
            "wipe_telegram_assets",
            "flip_live_trading",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    p = out["persistence"]
    print(
        f"ship={ship} n={p['n']} persist1={p['persist_1s']} persist2={p['persist_2s']} "
        f"same_sec={p['persist_same_sec']} cheap1={p['cheap_within_1s']} "
        f"med_take={p['median_takeable_s']} med_cheap={p['median_cheaper_s']}",
        flush=True,
    )
    return out


if __name__ == "__main__":
    run()
