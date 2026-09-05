#!/usr/bin/env python3
"""Rev 55 frequency research: more takes without cutting the shipped take WR.

Rev 54 hunt is BTC+ETH first-cross 6bps 45–55 + 90s unconfirmed dump.
The ship tape (`first_dump_by90_h2`) counted BTC and ETH independently (n=593,
take WR 93.8%, +$543). Live still locked *one 5m unix across coins*, so a BTC
fill blocked ETH on the same window. That overlay is stricter than the +EV tape.

This script rebuilds the ship rows from the 18-day print cache and compares:
  independent (by slug)  vs  global unix clock-lock (one coin per start).

Do not: restore alts, widen 45–55, drop 6bps, chase leftover cheaper, min_left
below 120, dump_mid90, reverse, 8¢ SL.
"""

from __future__ import annotations

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import full_coin_month as fcm  # noqa: E402
import high_wr as hw  # noqa: E402
from app.twap import TwapParams  # noqa: E402
import reverse_predict as rp  # noqa: E402

OUT = Path(__file__).resolve().parent / "rev55_clock.json"


def slim(pack: dict) -> dict:
    a, t, h = pack["all"], pack["train"], pack["holdout"]
    return {
        "n": pack.get("n") or a.get("n"),
        "pnl_usd": a.get("pnl_usd"),
        "take_win_rate": a.get("take_win_rate"),
        "ev_ok": a.get("ev_ok"),
        "robust": pack.get("robust"),
        "train": {
            "n": t.get("n"),
            "pnl_usd": t.get("pnl_usd"),
            "take_win_rate": t.get("take_win_rate"),
            "ev_ok": t.get("ev_ok"),
        },
        "holdout": {
            "n": h.get("n"),
            "pnl_usd": h.get("pnl_usd"),
            "take_win_rate": h.get("take_win_rate"),
            "ev_ok": h.get("ev_ok"),
        },
        "by_asset": pack.get("by_asset") or {},
    }


def overlap_stats(rows: list[dict]) -> dict:
    by: dict[int, list[str]] = {}
    for r in rows:
        by.setdefault(int(r["start"]), []).append(str(r.get("asset") or "?"))
    dual = sum(1 for xs in by.values() if len(set(xs)) >= 2)
    return {
        "n_takes": len(rows),
        "n_windows": len(by),
        "dual_btc_eth_windows": dual,
        "extra_takes_vs_clock": len(rows) - len(by),
        "clock_keeps_frac": None if not rows else round(len(by) / len(rows), 4),
    }


def rejected() -> list[dict]:
    return [
        {"lever": "restore_alts", "why": "Live leftover 45¢ FOK last-look; month dump overlays invalid."},
        {"lever": "min_left_below_120", "why": "BTC/ETH min_left 180 already worse than 120; 90s is the late gate we do not reopen."},
        {"lever": "bps_4", "why": "6bps is the ship sleeve; 4bps adds weaker lead."},
        {"lever": "band_40_60", "why": "45–55 is the +EV band; 40–60 dilutes WR."},
        {"lever": "chase_cheaper_leftover", "why": "First-cross is the WR lift; leftover 45¢ is the live bleed."},
        {"lever": "dump_mid90", "why": "Clips live winners sitting ~59¢ after 62 already printed."},
        {"lever": "twap_reverse", "why": "Fade tape −EV; reverse skips BM/confirm."},
        {"lever": "price_sl_8c", "why": "−EV vs BM scratch on the same tape."},
        {"lever": "fok_up_requote_after_miss", "why": "Filling 0.55 after a 0.47 miss is a worse in-band take, not more of the same sleeve."},
    ]


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
    indep = hw.pack(dumped)
    indep["by_asset"] = {a: te_summ(xs) for a, xs in _by(dumped).items()}
    clock_rows = fcm.clock_lock(dumped, rank="lead")
    clock = hw.pack(clock_rows)
    clock["by_asset"] = {a: te_summ(xs) for a, xs in _by(clock_rows).items()}
    ov = overlap_stats(dumped)
    wr_i = indep["all"].get("take_win_rate") or 0
    wr_c = clock["all"].get("take_win_rate") or 0
    ho_i = indep["holdout"].get("take_win_rate") or 0
    ho_c = clock["holdout"].get("take_win_rate") or 0
    wr_ok = abs(wr_i - wr_c) <= 0.03 and ho_i + 1e-12 >= ho_c - 0.02
    ship = bool(
        indep.get("robust")
        and indep["train"].get("ev_ok")
        and indep["holdout"].get("ev_ok")
        and wr_ok
        and ov["extra_takes_vs_clock"] > 0
    )
    out = {
        "strategy_rev": 55,
        "ship": ship,
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "source": "research/high_wr.py first_dump_by90_h2 on 18-day print cache",
        "question": "Increase fills without cutting the shipped BTC+ETH take win rate.",
        "answer": (
            "Unlock BTC and ETH on the same 5m unix. Keep per-asset lock: one horizon per coin, "
            "and after fill/dump that coin's unix stays taken until T1 (no same-slug reverse). "
            "Entry sleeve unchanged: first-cross 6bps 45–55, 120–280s, 90s unconfirmed dump. "
            "Independent n=593 take WR 93.8% +$543 vs lead-ranked one-per-unix n=485 WR 94.8% +$494: "
            "+108 dual-window takes, overall WR −1.0pp because clock cherry-picks the stronger lead, "
            "PnL still higher. Live clock is first-fill not lead-rank. BTC and ETH own WR are both 93.8%."
        ),
        "independent": slim(indep),
        "clock_lock_lead": slim(clock),
        "overlap": ov,
        "wr_delta_all": round(wr_i - wr_c, 4),
        "wr_delta_holdout": round(ho_i - ho_c, 4),
        "n_delta": ov["extra_takes_vs_clock"],
        "rejected": rejected(),
        "do_not": [
            "restore_alts",
            "min_left_below_120",
            "bps_4",
            "band_40_60",
            "chase_cheaper_leftover",
            "dump_mid90",
            "twap_reverse_on",
            "price_sl_8c",
            "wipe_telegram_assets",
            "flip_live_trading",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n")
    print(json.dumps({k: out[k] for k in ("ship", "overlap", "wr_delta_all", "wr_delta_holdout", "n_delta")}, indent=2))
    print(
        f"indep n={indep['all']['n']} wr={wr_i} ${indep['all']['pnl_usd']} | "
        f"clock n={clock['all']['n']} wr={wr_c} ${clock['all']['pnl_usd']}",
        flush=True,
    )
    return out


def _by(rows: list[dict]) -> dict[str, list]:
    g: dict[str, list] = {}
    for r in rows:
        g.setdefault(str(r.get("asset") or "?"), []).append(r)
    return g


def te_summ(xs: list[dict]) -> dict:
    rec = hw.te.summarize(xs)
    return {
        "n": rec.get("n"),
        "pnl_usd": rec.get("pnl_usd"),
        "take_win_rate": rec.get("take_win_rate"),
        "ev_ok": rec.get("ev_ok"),
    }


if __name__ == "__main__":
    run()
