#!/usr/bin/env python3
"""Add two more 5m coins to the BTC+ETH pin — research only.

Live hunt is Telegram assets ∩ twap_assets, pinned ["btc","eth"]. Owner asked
whether opening two more coins (same 6bps / 45–55 / 120–280 / first-cross /
dump90+oracle / independent clocks) is +EV.

Top logic (same bars as freq_params / rev59 / trend_side):
  * Settlement = this window's Chainlink 60s TWAP vs T0, not a 15m chart.
    Proxy tape = Binance 1s TWAP vs Binance T0 + CLOB first 45–55 6bps print.
    Live is Chainlink vs Chainlink; do not mix Binance−Gamma PTB.
  * Overlay = BM scratch already in _takes.json, then unconfirmed dump if
    never 62¢ by left=90, then oracle fair<0.60. Not dump_mid90.
  * Independent clocks (Rev 55): same 5m unix may hold BTC and SOL together.
  * Hold out newest 7d. Recommend a pair only if EACH alt is robust, holdout
    take WR ≥0.85, and core+pair beats core by ≥$5 holdout with train extra>0.
  * Live-only costs that print tape cannot pay: CLOB 14-token cap (4 coins
    current+prewarm needs 16), FOK leftover kill, historical live alt bleed.
  * 3 coins fit 12/14. Opening one extra is the WS-safe alternative.

Do not: 15m, 1H Binance candles, HYPE (no CLOB prints here), ZEC (no month
tape), reverse, 4bps, 40–60, leftover chase, pin twap_assets in this script.
Ship is always false — owner decides.
"""

from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from itertools import combinations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import cheaper_than_first  # noqa: E402
import reverse_predict as rp  # noqa: E402
from full_coin_month import SYMBOL  # noqa: E402
from high_wr import path_features  # noqa: E402
from rev59_oracle import dump_row, oracle, ship_base  # noqa: E402
from rev60_fok import CHEAP_EPS, buys_side  # noqa: E402
from tape_pull import (  # noqa: E402
    CORE,
    HOLDOUT_DAYS,
    MON,
    REV,
    TAKES,
    TWAP60,
    buys,
    fill_ts,
    load_raw,
    pack,
    split_holdout,
)

OUT = Path(__file__).resolve().parent / "two_alts.json"
SHIP = Path(__file__).resolve().parent / "two_alts_ship.json"
CANDIDATES = ("sol", "xrp", "doge", "bnb")
ORACLE_FAIR = 0.60
WS_MAX_TOKENS = 14
CHEAP_SLACK = 0.15
LIVE_ALT_BLEED = {
    "source": "research/rev48_live.json",
    "note": "Post-Rev47 closed alts before dump90+oracle+first-cross pin. Not this sleeve.",
    "alts_n": 13,
    "alts_net_usd": -8.87,
    "alt_hold_hit": 0.14,
    "sol_n": 4,
    "sol_net_usd": -8.49,
    "alt_120_180_hold_hit": 0.0,
}


def leftover_1s(raw: list, row: dict) -> dict:
    t0 = fill_ts(row)
    px0 = float(row["px"])
    side = str(row["side"])
    after = buys_side(raw, int(row["start"]), int(row["end"]), side)
    after = [p for p in after if p["ts"] >= t0]
    cheap = None
    up1 = None
    for p in after:
        ts, px = int(p["ts"]), float(p["px"])
        if cheaper_than_first(px, px0, CHEAP_EPS) and cheap is None:
            cheap = ts
        if px + 1e-12 >= px0 + 0.01 - 1e-12 and up1 is None:
            up1 = ts
        if cheap is not None and up1 is not None:
            break
    return {
        "cheap_1s": bool(cheap is not None and cheap - t0 <= 1),
        "up1_before_cheap": bool(up1 is not None and (cheap is None or cheap > up1) and up1 - t0 <= 1),
    }


def apply_shipped(row: dict) -> dict:
    x = ship_base(row)
    if not x.get("scratched"):
        o90 = row.get("o90")
        if o90 is not None and float(o90["fair"]) + 1e-12 < ORACLE_FAIR:
            x = dump_row(x, "twap_scratch_oracle", row.get("px90") or row.get("px"))
    x["asset"] = row["asset"]
    x["end"] = row["end"]
    x["start"] = row["start"]
    x["slug"] = row["slug"]
    x["cheap_1s"] = bool(row.get("cheap_1s"))
    x["up1_before_cheap"] = bool(row.get("up1_before_cheap"))
    x["orig_won"] = bool(row.get("orig_won"))
    x["orig_scratch"] = bool(row.get("orig_scratch"))
    x["orig_pnl"] = float(row.get("orig_pnl") or 0)
    x["ever_62_by90"] = bool(row.get("ever_62_by90"))
    x["px90"] = row.get("px90")
    x["exit_why"] = x.get("exit_why") or (row.get("exit_why") if x.get("scratched") else None)
    return x


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
    }


def orig_hold_wr(rows: list[dict]) -> tuple[float | None, int]:
    held = [r for r in rows if not r.get("orig_scratch")]
    if not held:
        return None, 0
    return round(sum(1 for r in held if r.get("orig_won")) / len(held), 4), len(held)


def pack_slim(rows: list[dict]) -> dict:
    p = pack(rows)
    s = slim(p)
    s["robust"] = p["robust"]
    cheap = [r for r in rows if r.get("cheap_1s")]
    s["cheap_1s"] = round(len(cheap) / len(rows), 4) if rows else None
    s["up1_before_cheap"] = round(sum(1 for r in rows if r.get("up1_before_cheap")) / len(rows), 4) if rows else None
    wr, n = orig_hold_wr(rows)
    s["orig_hold_wr"] = wr
    s["orig_hold_n"] = n
    _train, hold = split_holdout(rows)
    h_wr, h_n = orig_hold_wr(hold)
    s["orig_hold_wr_holdout"] = h_wr
    s["orig_hold_n_holdout"] = h_n
    s["confirm_62_by90"] = round(sum(1 for r in rows if r.get("ever_62_by90")) / len(rows), 4) if rows else None
    s["dump_share"] = round(s["scratch_n"] / s["n"], 4) if s["n"] else None
    why = Counter(str(r.get("exit_why") or "hold") for r in rows)
    s["exit_why"] = dict(why)
    return s


def alt_ok(g: dict) -> bool:
    return bool(
        g.get("robust")
        and g["train"]["ev_ok"]
        and g["holdout"]["ev_ok"]
        and (g["holdout"]["take_wr"] or 0) >= 0.85
        and g["holdout"]["n"] >= 25
        and g["train"]["n"] >= 25
    )


def combo_beats(core: dict, combo: dict) -> bool:
    d_tr = round(combo["train"]["pnl5"] - core["train"]["pnl5"], 2)
    d_ho = round(combo["holdout"]["pnl5"] - core["holdout"]["pnl5"], 2)
    return bool(
        combo.get("robust")
        and d_tr > 0
        and d_ho >= 5.0
        and (combo["holdout"]["take_wr"] or 0) >= 0.85
        and combo["holdout"]["n"] >= core["holdout"]["n"]
    )


def pack_orig(rows: list[dict]) -> dict:
    """BM-scratch only (no dump90 / oracle). Diagnostic — not a live candidate."""
    xs = []
    for r in rows:
        x = dict(r)
        x["pnl"] = float(r.get("orig_pnl") or 0)
        x["scratched"] = bool(r.get("orig_scratch"))
        x["exit_why"] = "bm" if r.get("orig_scratch") else "hold"
        xs.append(x)
    return pack_slim(xs)


def leftover_ok(core: dict, per_asset: dict, pair: list[str]) -> bool:
    cheap_c = float(core.get("cheap_1s") or 0)
    return all(float(per_asset[a].get("cheap_1s") or 0) <= cheap_c + CHEAP_SLACK for a in pair)


def tokens_needed(n_coins: int) -> dict:
    current = n_coins * 2
    prewarm = n_coins * 2
    need = current + prewarm
    return {
        "coins": n_coins,
        "tokens_current": current,
        "tokens_prewarm": prewarm,
        "tokens_needed": need,
        "ws_max_tokens": WS_MAX_TOKENS,
        "fits": need <= WS_MAX_TOKENS,
        "overflow": max(0, need - WS_MAX_TOKENS),
        "note": (
            "14 tokens on two sockets. Inventory always. T-45 prewarm next 5m. "
            "4 coins need 16 tokens so one next-window pair is cold at T0."
            if n_coins >= 4
            else "3 coins fit 12/14. BTC+ETH only uses 8."
        ),
    }


def occupancy(rows: list[dict], assets: tuple[str, ...] | list[str]) -> dict:
    want = set(assets)
    by_unix: dict[int, set[str]] = defaultdict(set)
    for r in rows:
        a = str(r.get("asset") or "")
        if a in want:
            by_unix[int(r["start"])].add(a)
    hist = Counter(len(v) for v in by_unix.values())
    n_unix = len(by_unix)
    ge2 = sum(c for k, c in hist.items() if k >= 2)
    ge3 = sum(c for k, c in hist.items() if k >= 3)
    eq4 = hist.get(4, 0)
    return {
        "assets": list(assets),
        "unix_with_take": n_unix,
        "hist": {str(k): int(hist[k]) for k in sorted(hist)},
        "share_ge2": None if not n_unix else round(ge2 / n_unix, 4),
        "share_ge3": None if not n_unix else round(ge3 / n_unix, 4),
        "n_eq4": eq4,
        "max_concurrent": max(hist) if hist else 0,
        "stake_if_all_print": round(len(assets) * 3.0, 2),
    }


def overlap_unix(core_rows: list[dict], extra: list[dict]) -> dict:
    core_u = {int(r["start"]) for r in core_rows}
    alt_u = {int(r["start"]) for r in extra}
    both = core_u & alt_u
    return {
        "core_unix": len(core_u),
        "alt_unix": len(alt_u),
        "both": len(both),
        "share_of_alts": None if not alt_u else round(len(both) / len(alt_u), 4),
        "note": "Independent clocks: both>0 means extra stake on the same 5m unix, not extra independent windows.",
    }


def hydrate(series_of: dict, want: tuple[str, ...]) -> list[dict]:
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if int(r["end"]) >= TWAP60]
    rows = []
    for r in first:
        asset = str(r.get("asset") or "")
        if asset not in want:
            continue
        start, end = int(r["start"]), int(r["end"])
        t_fill = fill_ts(r)
        raw = load_raw(REV, r["slug"]) or load_raw(MON, r["slug"])
        full = buys(raw, start, end, lo=0.05, hi=0.99) if raw else []
        feat = path_features(full, r["side"], t_fill, end) if full else {}
        series = series_of.get(asset)
        o90 = oracle(series, start, end - 90, r["side"], 90.0) if series else None
        left = leftover_1s(raw or [], r) if raw else {"cheap_1s": False, "up1_before_cheap": False}
        row = dict(r)
        row["ts"] = t_fill
        row["orig_pnl"] = float(r["pnl"])
        row["orig_scratch"] = bool(r.get("scratched"))
        row["orig_won"] = bool(r.get("won"))
        row.update(feat)
        row["o90"] = o90
        row.update(left)
        rows.append(row)
    return rows


def score_combo(core_rows: list[dict], extra: list[dict], core: dict) -> dict:
    combo_rows = core_rows + extra
    g = pack_slim(combo_rows)
    extra_g = pack_slim(extra)
    d_tr = round(g["train"]["pnl5"] - core["train"]["pnl5"], 2)
    d_ho = round(g["holdout"]["pnl5"] - core["holdout"]["pnl5"], 2)
    fillable_extra = [r for r in extra if not r.get("cheap_1s")]
    fillable_core = [r for r in core_rows if not r.get("cheap_1s")]
    fill_g = pack_slim(fillable_core + fillable_extra)
    fill_core = pack_slim(fillable_core)
    d_ho_fill = round(fill_g["holdout"]["pnl5"] - fill_core["holdout"]["pnl5"], 2)
    d_tr_fill = round(fill_g["train"]["pnl5"] - fill_core["train"]["pnl5"], 2)
    return {
        "beats_core": combo_beats(core, g),
        "d_train": d_tr,
        "d_holdout": d_ho,
        "combo": g,
        "alts_only": extra_g,
        "overlap": overlap_unix(core_rows, extra),
        "occupancy": occupancy(combo_rows, tuple(sorted({r["asset"] for r in combo_rows}))),
        "fillable_no_cheap_1s": {
            "d_train": d_tr_fill,
            "d_holdout": d_ho_fill,
            "beats_core": combo_beats(fill_core, fill_g),
            "combo_holdout": fill_g["holdout"],
            "note": "Drop prints that walked cheaper within 1s — live leftover kill proxy, not a fill model.",
        },
    }


def pick_pair(pairs: list[dict], per_asset: dict, core: dict) -> list[str] | None:
    for p in pairs:
        if p["each_ok"] and p["beats_core"] and leftover_ok(core, per_asset, p["pair"]):
            return list(p["pair"])
    for p in pairs:
        if p["each_ok"] and p["beats_core"]:
            return list(p["pair"])
    return None


def pick_plus_one(singles: list[dict], per_asset: dict, core: dict) -> str | None:
    for s in singles:
        a = s["asset"]
        if s["each_ok"] and s["beats_core"] and leftover_ok(core, per_asset, [a]):
            return a
        if s["each_ok"] and s["beats_core"]:
            return a
    return None


def hydrate_hype_n() -> int:
    takes = json.loads(TAKES.read_text())
    first = takes.get("first") or []
    return sum(1 for r in first if str(r.get("asset") or "") == "hype")


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if int(r["end"]) >= TWAP60]
    tmin = min(int(r["start"]) for r in first) - 180
    tmax = max(int(r["end"]) for r in first) + 5
    rp.SYMBOL.update(SYMBOL)
    want = tuple(CORE) + CANDIDATES
    print(f"load series {tmin}->{tmax} assets={want}", flush=True)
    series_of = {}
    for asset in want:
        print(f"  series {asset}", flush=True)
        series_of[asset] = rp.load_series(asset, tmin, tmax)
    print("hydrate", flush=True)
    raw_rows = hydrate(series_of, want)
    shipped = [apply_shipped(r) for r in raw_rows]
    by_asset: dict[str, list[dict]] = {a: [] for a in want}
    for r in shipped:
        by_asset.setdefault(r["asset"], []).append(r)
    per_asset = {a: pack_slim(by_asset.get(a) or []) for a in want}
    raw_by: dict[str, list[dict]] = {a: [] for a in want}
    for r in raw_rows:
        raw_by.setdefault(r["asset"], []).append(r)
    overlay_cf = {}
    for a in want:
        xs = raw_by.get(a) or []
        dump90 = []
        for r in xs:
            y = ship_base(r)
            y["asset"] = r["asset"]
            y["end"] = r["end"]
            y["start"] = r["start"]
            y["cheap_1s"] = r.get("cheap_1s")
            y["up1_before_cheap"] = r.get("up1_before_cheap")
            y["orig_won"] = r.get("orig_won")
            y["orig_scratch"] = r.get("orig_scratch")
            y["orig_pnl"] = r.get("orig_pnl")
            y["ever_62_by90"] = r.get("ever_62_by90")
            dump90.append(y)
        overlay_cf[a] = {
            "bm_only": pack_orig(xs),
            "dump90": pack_slim(dump90),
            "dump90_oracle": per_asset[a],
            "note": "bm_only is print-tape BM scratch without dump90. Not a live candidate: live alt holds hit 14%.",
        }
    core_rows = [r for r in shipped if r["asset"] in CORE]
    core = pack_slim(core_rows)
    passing = [a for a in CANDIDATES if alt_ok(per_asset[a])]
    fail_reasons = {}
    for a in CANDIDATES:
        g = per_asset[a]
        reasons = []
        if not g["train"]["ev_ok"]:
            reasons.append("train_-EV")
        if not g["holdout"]["ev_ok"]:
            reasons.append("holdout_-EV")
        wr = g["holdout"]["take_wr"]
        if wr is None or wr < 0.85:
            reasons.append("holdout_wr_below_85_or_no_holds")
        if g["holdout"]["n"] < 25:
            reasons.append("holdout_n_thin")
        if (g.get("dump_share") or 0) >= 0.90:
            reasons.append("dump_share_ge_90")
        if (g.get("confirm_62_by90") or 0) < 0.40:
            reasons.append("confirm_62_rare")
        fail_reasons[a] = reasons
    pairs = []
    for a, b in combinations(CANDIDATES, 2):
        extra = (by_asset.get(a) or []) + (by_asset.get(b) or [])
        scored = score_combo(core_rows, extra, core)
        scored.update(
            {
                "pair": [a, b],
                "each_ok": bool(alt_ok(per_asset[a]) and alt_ok(per_asset[b])),
                "cheap_1s_alts": scored["alts_only"].get("cheap_1s"),
                "cheap_1s_core": core.get("cheap_1s"),
                "leftover_ok": leftover_ok(core, per_asset, [a, b]),
            }
        )
        pairs.append(scored)
    pairs.sort(key=lambda p: (p["each_ok"], p["beats_core"], p["d_holdout"], p["d_train"]), reverse=True)
    singles = []
    for a in CANDIDATES:
        extra = by_asset.get(a) or []
        scored = score_combo(core_rows, extra, core)
        scored.update(
            {
                "asset": a,
                "each_ok": alt_ok(per_asset[a]),
                "leftover_ok": leftover_ok(core, per_asset, [a]),
            }
        )
        singles.append(scored)
    singles.sort(key=lambda s: (s["each_ok"], s["beats_core"], s["d_holdout"], s["d_train"]), reverse=True)
    recommend = pick_pair(pairs, per_asset, core)
    recommend_one = pick_plus_one(singles, per_asset, core)
    occ_core = occupancy(core_rows, CORE)
    occ_four = occupancy(shipped, want)
    hype_n = hydrate_hype_n()
    why = (
        "Shipped first-cross + dump90 + oracle, independent clocks, 7d holdout. "
        "BTC+ETH stay +EV. SOL/XRP/DOGE/BNB are train and holdout −EV on this sleeve: "
        "they rarely print 62¢ so dump90 fires on ~90%+ of fills and donates spread "
        "instead of holding. BM-only print-tape orig_hold_wr still looks 75–95% "
        "(SOL 94.6%) but that is NOT live — Rev 47/48 alt holds hit 14%, SOL −$8.49. "
        "Do not skip dump90 on alts to chase that tape. 4 coins need 16 WS tokens "
        "(overflow 2). 3 coins fit 12/14 but no single alt passes. HYPE 0 CLOB prints. "
        "BNB n=79 is too thin. Owner decides; do not pin twap_assets."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Open two more 5m coins on the frozen BTC+ETH TWAP sleeve?",
        "holdout_days": HOLDOUT_DAYS,
        "n_joined": len(shipped),
        "hype_prints": hype_n,
        "core": core,
        "per_asset": per_asset,
        "overlay_cf": overlay_cf,
        "fail_reasons": fail_reasons,
        "passing_alts": passing,
        "occupancy": {"btc_eth": occ_core, "all_six": occ_four},
        "plus_one": [
            {
                "asset": s["asset"],
                "each_ok": s["each_ok"],
                "beats_core": s["beats_core"],
                "leftover_ok": s["leftover_ok"],
                "d_train": s["d_train"],
                "d_holdout": s["d_holdout"],
                "combo": s["combo"],
                "alts_only": s["alts_only"],
                "overlap": s["overlap"],
                "fillable_no_cheap_1s": s["fillable_no_cheap_1s"],
            }
            for s in singles
        ],
        "pairs": [
            {
                "pair": p["pair"],
                "each_ok": p["each_ok"],
                "beats_core": p["beats_core"],
                "leftover_ok": p["leftover_ok"],
                "d_train": p["d_train"],
                "d_holdout": p["d_holdout"],
                "combo": p["combo"],
                "alts_only": p["alts_only"],
                "overlap": p["overlap"],
                "fillable_no_cheap_1s": p["fillable_no_cheap_1s"],
            }
            for p in pairs
        ],
        "recommend": recommend,
        "recommend_plus_one": recommend_one,
        "ship": False,
        "pick": None if recommend is None else "+".join(recommend),
        "why": why,
        "ws_slots": {
            "btc_eth": tokens_needed(2),
            "plus_one": tokens_needed(3),
            "plus_two": tokens_needed(4),
        },
        "live_fingerprint": LIVE_ALT_BLEED,
        "do_not": [
            "pin_twap_assets_without_owner",
            "15m",
            "1h_binance",
            "hype_no_prints",
            "bnb_thin",
            "twap_reverse_on",
            "dump_mid90",
            "lead_4bps",
            "band_40_60",
            "chase_leftover",
            "clock_lock_one_coin",
            "raise_btc_eth_min_left_180",
            "skip_dump90_on_alts_to_chase_print_hold_wr",
        ],
        "params_kept": {
            "twap_min_lead_bps": 6.0,
            "band": [0.45, 0.55],
            "twap_min_left": 120.0,
            "twap_max_left": 280.0,
            "twap_confirm_px": 0.62,
            "twap_confirm_fair": 0.60,
            "independent_clocks": True,
        },
    }
    ws_two = rec["ws_slots"]["plus_two"]
    if recommend is None:
        headline = "Do not open two more coins yet."
        if recommend_one:
            headline += f" Tape would allow ONE extra ({recommend_one.upper()}) which fits 12/14 WS. Still owner decides."
        rec["findings"] = {
            "headline": headline,
            "best_pair": None if not pairs else pairs[0]["pair"],
            "best_d_holdout_pair": None if not pairs else max(pairs, key=lambda p: p["d_holdout"])["pair"],
            "recommend_plus_one": recommend_one,
            "fail_reasons": fail_reasons,
            "mechanism": (
                "Dump90+oracle that saves BTC/ETH steamrollers fires on almost every "
                "alt fill because alts rarely print 62¢. Residual hold WR looks 100% "
                "because almost nothing is held. BM-only tape looks +EV; live alt holds did not."
            ),
        }
    else:
        rec["findings"] = {
            "headline": (
                f"Tape prefers {recommend[0].upper()}+{recommend[1].upper()} if you open two; "
                f"still do not auto-pin. 4 coins need {ws_two['tokens_needed']} tokens "
                f"(overflow {ws_two['overflow']}). 3 coins fit 12/14."
            ),
            "best_pair": recommend,
            "recommend_plus_one": recommend_one,
            "ws_plus_two_fits": ws_two["fits"],
        }
    rec["elapsed_s"] = round(time.time() - t0, 2)
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    SHIP.write_text(
        json.dumps(
            {
                "strategy_rev": 60,
                "ship": False,
                "pick": rec["pick"],
                "recommend": recommend,
                "recommend_plus_one": recommend_one,
                "passing_alts": passing,
                "fail_reasons": fail_reasons,
                "researched_at_utc": rec["researched_at_utc"],
                "source": "research/two_alts.json",
                "question": rec["question"],
                "why": rec["why"],
                "core_holdout": core["holdout"],
                "ws_plus_two_fits": rec["ws_slots"]["plus_two"]["fits"],
                "ws_plus_one_fits": rec["ws_slots"]["plus_one"]["fits"],
                "hype_prints": hype_n,
                "live_alt_bleed_net": LIVE_ALT_BLEED["alts_net_usd"],
                "fail_reasons": fail_reasons,
                "closest_plus_one": None if not singles else {
                    "asset": singles[0]["asset"],
                    "d_holdout": singles[0]["d_holdout"],
                    "each_ok": singles[0]["each_ok"],
                },
                "closest_pair": None if not pairs else {
                    "pair": pairs[0]["pair"],
                    "d_holdout": pairs[0]["d_holdout"],
                    "each_ok": pairs[0]["each_ok"],
                },
                "alt_dump_share": {a: per_asset[a].get("dump_share") for a in CANDIDATES},
                "alt_confirm_62": {a: per_asset[a].get("confirm_62_by90") for a in CANDIDATES},
                "do_not": rec["do_not"],
                "params_kept": rec["params_kept"],
            },
            indent=2,
            default=str,
        )
    )
    print(
        "core", core["n"], core["holdout"]["pnl5"], "wr", core["holdout"]["take_wr"],
        "passing", passing, "pick", rec["pick"], "plus_one", recommend_one,
        flush=True,
    )
    for a in want:
        g = per_asset[a]
        print(
            f"  {a} n={g['n']} ho={g['holdout']['pnl5']} wr={g['holdout']['take_wr']} "
            f"ok={alt_ok(g)} cheap1s={g.get('cheap_1s')} dump={g.get('dump_share')} "
            f"c62={g.get('confirm_62_by90')} orig_hold_wr={g.get('orig_hold_wr')} "
            f"orig_ho={g.get('orig_hold_wr_holdout')}",
            flush=True,
        )
    if pairs:
        p = pairs[0]
        print(
            "top pair", p["pair"], "d_ho", p["d_holdout"], "each_ok", p["each_ok"],
            "beats", p["beats_core"], "leftover_ok", p["leftover_ok"],
            flush=True,
        )
    if singles:
        s = singles[0]
        print(
            "top plus_one", s["asset"], "d_ho", s["d_holdout"], "ok", s["each_ok"],
            "beats", s["beats_core"],
            flush=True,
        )
    return rec


if __name__ == "__main__":
    run()
