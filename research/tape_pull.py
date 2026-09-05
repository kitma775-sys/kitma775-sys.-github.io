#!/usr/bin/env python3
"""拉盤 / direction stress-test. Research only. Does not patch live.

Questions (extreme opposite bets, same first-cross sleeve):
  H_follow     buy TWAP side (current)
  H_fade       buy the other 5m leg, hold to settle, no BM scratch
  H_fade_hold  fade only residual holds (selection-biased, not a live rule)
  H_dump_fast  after fill, if same-side prints 62¢ within T seconds, dump
  H_fade_fast  if 62¢ within T seconds, dump follow and hold the other leg
  H_skip_2way  skip/dump if opposite also prints 45–55 after fill (choppy tape)
  H_btc_only / H_eth_only

拉盤 = CLOB same-side BUY prints racing toward 62/87 after a 6bps first-cross.
Causal overlays fire at the first 62¢ print (or at opposite-band print), not
with lookahead. Month prints are slimmed ≤62¢; BTC/ETH reverse_30d cache is
full 5–99¢ and is the primary path tape.
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
import twap_engine as te  # noqa: E402

OUT = Path(__file__).resolve().parent / "tape_pull.json"
TAKES = Path("/tmp/twap_month_cache/_takes.json")
REV = Path("/tmp/reverse_30d_cache")
MON = Path("/tmp/twap_month_cache")
HOLDOUT_DAYS = 7
NOTIONAL = 5.0
LIVE_STAKE = 3.0
HAIRCUT = 0.02
CONFIRM = 0.62
CORE = ("btc", "eth")
TWAP60 = te.TWAP60_START


def buys(raw: list, start: int, end: int, *, lo: float, hi: float) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or t.get("Side") or "BUY").upper() != "BUY":
            continue
        try:
            px = float(t.get("px") or t.get("price") or 0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start - 2 or ts > end + 2:
            continue
        if px < lo or px > hi:
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc not in {"Up", "Down"}:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc})
    out.sort(key=lambda x: x["ts"])
    return out


def load_raw(cache: Path, slug: str) -> list:
    path = cache / f"{slug}.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def fill_ts(row: dict) -> int:
    if row.get("ts"):
        return int(row["ts"])
    return int(row["end"]) - int(row["left"])


def pnl_hold(px: float, won: bool, notional: float = NOTIONAL) -> float:
    shares = notional / max(px, 0.01)
    fee = taker_fee(shares, px, 0.07)
    if won:
        return round(shares * (1.0 - px) - fee, 5)
    return round(-shares * px - fee, 5)


def pnl_scratch(entry_px: float, exit_px: float, notional: float = NOTIONAL) -> float:
    shares = notional / max(entry_px, 0.01)
    buy_fee = taker_fee(shares, entry_px, 0.07)
    sell_fee = taker_fee(shares, exit_px, 0.07)
    return round(shares * (exit_px - entry_px) - buy_fee - sell_fee, 5)


def scale3(pnl5: float) -> float:
    return round(pnl5 * (LIVE_STAKE / NOTIONAL), 5)


def other_px(px: float) -> float:
    return min(max(1.0 - float(px), 0.20), 0.80)


def path_of(full: list[dict], side: str, t0: int, end: int) -> dict:
    opp = "Down" if side == "Up" else "Up"
    after = [p for p in full if p["outcome"] == side and p["ts"] >= t0]
    opp_after = [p for p in full if p["outcome"] == opp and p["ts"] >= t0]
    band_opp = [p for p in opp_after if 0.45 - 1e-12 <= p["px"] <= 0.55 + 1e-12]
    t62 = None
    t70 = None
    t85 = None
    max_px = 0.0
    for p in after:
        if p["px"] > max_px:
            max_px = p["px"]
        if t62 is None and p["px"] + 1e-12 >= CONFIRM:
            t62 = p["ts"]
        if t70 is None and p["px"] + 1e-12 >= 0.70:
            t70 = p["ts"]
        if t85 is None and p["px"] + 1e-12 >= 0.85:
            t85 = p["ts"]
    t_opp_band = band_opp[0]["ts"] if band_opp else None
    by90 = end - 90
    ever_62_by90 = t62 is not None and t62 <= by90
    n_same_10 = sum(1 for p in after if p["ts"] - t0 <= 10)
    n_opp_10 = sum(1 for p in opp_after if p["ts"] - t0 <= 10)
    return {
        "dt62": None if t62 is None else int(t62 - t0),
        "dt70": None if t70 is None else int(t70 - t0),
        "dt85": None if t85 is None else int(t85 - t0),
        "max_after": round(max_px, 4),
        "ever_62": t62 is not None,
        "ever_62_by90": ever_62_by90,
        "ever_70": t70 is not None,
        "ever_85": t85 is not None,
        "t62": t62,
        "two_way": t_opp_band is not None and (t_opp_band - t0) <= 20,
        "dt_opp_band": None if t_opp_band is None else int(t_opp_band - t0),
        "n_same_10": n_same_10,
        "n_opp_10": n_opp_10,
        "opp_more": n_opp_10 > n_same_10,
        "n_after": len(after),
        "full_tape": max_px > 0.621,
    }


def split_holdout(rows: list[dict], days: int = HOLDOUT_DAYS):
    if not rows:
        return [], []
    newest = max(int(r["end"]) for r in rows)
    cut = newest - days * 86400
    train = [r for r in rows if int(r["end"]) < cut]
    hold = [r for r in rows if int(r["end"]) >= cut]
    return train, hold


def rec_of(rows: list[dict], *, pnl_key: str = "pnl") -> dict:
    if not rows:
        return {"n": 0, "pnl5": 0.0, "pnl3": 0.0, "win": 0, "lose": 0, "held": 0, "scratch_n": 0, "take_wr": None, "ev_ok": False}
    pnl5 = round(sum(float(r[pnl_key] or 0) for r in rows), 4)
    held = [r for r in rows if not r.get("scratched")]
    win = sum(1 for r in held if r.get("won"))
    lose = sum(1 for r in held if not r.get("won"))
    n_h = win + lose
    wr = None if n_h == 0 else round(win / n_h, 4)
    return {
        "n": len(rows),
        "pnl5": pnl5,
        "pnl3": scale3(pnl5),
        "win": win,
        "lose": lose,
        "held": n_h,
        "scratch_n": sum(1 for r in rows if r.get("scratched")),
        "take_wr": wr,
        "ev_ok": pnl5 > 0,
    }


def pack(rows: list[dict], *, pnl_key: str = "pnl") -> dict:
    train, hold = split_holdout(rows)
    out = {"all": rec_of(rows, pnl_key=pnl_key), "train": rec_of(train, pnl_key=pnl_key), "holdout": rec_of(hold, pnl_key=pnl_key)}
    out["robust"] = bool(
        out["train"]["ev_ok"]
        and out["holdout"]["ev_ok"]
        and out["train"]["n"] >= 25
        and out["holdout"]["n"] >= 25
    )
    return out


def apply_fast_dump(row: dict, *, max_dt: int) -> dict:
    out = dict(row)
    if row.get("scratched"):
        return out
    dt = row.get("dt62")
    if dt is None or dt > max_dt:
        return out
    exit_px = max(0.01, CONFIRM - HAIRCUT)
    out["scratched"] = True
    out["exit_why"] = f"fast62_{max_dt}"
    out["pnl"] = pnl_scratch(row["px"], exit_px)
    return out


def apply_fast_fade(row: dict, *, max_dt: int) -> dict:
    """At first 62¢ print: dump follow (haircut) AND hold the other leg from ~38¢.

    Conservative: other fill = 1-0.62 + 2¢ = 0.40. Fade wins iff original lost.
    """
    out = dict(row)
    if row.get("scratched"):
        return out
    dt = row.get("dt62")
    if dt is None or dt > max_dt:
        return out
    dump_pnl = pnl_scratch(row["px"], max(0.01, CONFIRM - HAIRCUT))
    fade_px = min(max(1.0 - CONFIRM + HAIRCUT, 0.20), 0.80)
    fade_won = not row["won"]
    fade_pnl = pnl_hold(fade_px, fade_won)
    out["scratched"] = False
    out["won"] = fade_won
    out["exit_why"] = f"fade62_{max_dt}"
    out["pnl"] = round(dump_pnl + fade_pnl, 5)
    out["px"] = fade_px
    return out


def apply_two_way_dump(row: dict) -> dict:
    out = dict(row)
    if row.get("scratched"):
        return out
    if not row.get("two_way"):
        return out
    exit_px = max(0.01, float(row["px"]) - HAIRCUT)
    out["scratched"] = True
    out["exit_why"] = "two_way_tape"
    out["pnl"] = pnl_scratch(row["px"], exit_px)
    return out


def fade_hold_settle(row: dict) -> dict:
    """Ignore original scratch; buy other at 1-entry, hold to $1/$0."""
    out = dict(row)
    px = other_px(row["px"])
    won = not row["orig_won"]
    out["scratched"] = False
    out["won"] = won
    out["px"] = px
    out["pnl"] = pnl_hold(px, won)
    out["exit_why"] = "fade_settle"
    return out


def bucket(dt):
    if dt is None:
        return "never_62"
    if dt <= 5:
        return "0-5s"
    if dt <= 15:
        return "6-15s"
    if dt <= 30:
        return "16-30s"
    if dt <= 60:
        return "31-60s"
    if dt <= 120:
        return "61-120s"
    return "120s+"


def wr_pnl(xs: list[dict]) -> dict:
    if not xs:
        return {"n": 0, "win": 0, "lose": 0, "wr": None, "pnl5": 0.0}
    win = sum(1 for r in xs if r.get("won"))
    lose = sum(1 for r in xs if not r.get("won"))
    n = win + lose
    pnl5 = round(sum(float(r["hold_pnl"] if r.get("hold_pnl") is not None else r["pnl"]) for r in xs), 4)
    return {"n": n, "win": win, "lose": lose, "wr": None if n == 0 else round(win / n, 4), "pnl5": pnl5}


def run() -> dict:
    t0 = time.time()
    takes = json.loads(TAKES.read_text())
    first = [r for r in takes.get("first") or [] if r.get("asset") in CORE and int(r["end"]) >= TWAP60]
    rows = []
    n_full = 0
    n_slim = 0
    n_miss = 0
    for r in first:
        slug = r["slug"]
        start, end = int(r["start"]), int(r["end"])
        t_fill = fill_ts(r)
        raw = load_raw(REV, slug)
        src = "rev"
        if not raw:
            raw = load_raw(MON, slug)
            src = "mon"
        if not raw:
            n_miss += 1
            continue
        full = buys(raw, start, end, lo=0.05, hi=0.99)
        feat = path_of(full, r["side"], t_fill, end)
        if src == "rev":
            n_full += 1
        else:
            n_slim += 1
        row = dict(r)
        row["ts"] = t_fill
        row["orig_won"] = bool(r["won"])
        row["orig_pnl"] = float(r["pnl"])
        row["hold_pnl"] = pnl_hold(r["px"], bool(r["won"]))
        row.update(feat)
        row["tape"] = src
        # dump_unconfirmed_by90 overlay on residual holds (shipped)
        if (not r.get("scratched")) and (not feat["ever_62_by90"]):
            exit_px = max(0.01, float(r["px"]) - HAIRCUT)
            row["ship_pnl"] = pnl_scratch(r["px"], exit_px)
            row["ship_scratched"] = True
            row["ship_why"] = "unconfirmed_by90"
        else:
            row["ship_pnl"] = float(r["pnl"])
            row["ship_scratched"] = bool(r.get("scratched"))
            row["ship_why"] = r.get("exit_why")
        rows.append(row)

    full_rows = [r for r in rows if r["tape"] == "rev"]

    # Confirmed-hold anatomy (no scratch): hold-to-settle PnL by pull speed
    holds = [r for r in full_rows if not r.get("scratched")]
    by_dt = defaultdict(list)
    for r in holds:
        by_dt[bucket(r.get("dt62"))].append(r)
    confirm_holds = [r for r in holds if r.get("ever_62")]
    never = [r for r in holds if not r.get("ever_62")]
    fast15 = [r for r in confirm_holds if r.get("dt62") is not None and int(r["dt62"]) <= 15]
    slow15 = [r for r in confirm_holds if r.get("dt62") is not None and int(r["dt62"]) > 15]
    to70 = [r for r in confirm_holds if r.get("ever_70")]
    to85 = [r for r in confirm_holds if r.get("ever_85")]
    two_way_h = [r for r in holds if r.get("two_way")]
    one_way_h = [r for r in holds if not r.get("two_way")]

    # Hypotheses
    shipped = []
    for r in rows:
        x = dict(r)
        x["pnl"] = r["ship_pnl"]
        x["scratched"] = r["ship_scratched"]
        shipped.append(x)

    fade_all = [fade_hold_settle(r) for r in rows]
    fade_full = [fade_hold_settle(r) for r in full_rows]
    follow_hold = []
    for r in rows:
        x = dict(r)
        x["scratched"] = False
        x["won"] = r["orig_won"]
        x["pnl"] = r["hold_pnl"]
        follow_hold.append(x)

    fade_residual = []
    for r in rows:
        if r.get("scratched"):
            x = dict(r)
            x["pnl"] = r["orig_pnl"]
            fade_residual.append(x)
        else:
            fade_residual.append(fade_hold_settle(r))

    grid = {
        "follow_scratch_shipped_dump90": pack(shipped),
        "follow_hold_to_settle_no_scratch": pack(follow_hold),
        "fade_every_entry_hold_settle": pack(fade_all),
        "fade_residual_holds_only": pack(fade_residual),
        "btc_only_shipped": pack([r for r in shipped if r["asset"] == "btc"]),
        "eth_only_shipped": pack([r for r in shipped if r["asset"] == "eth"]),
        "dump_fast62_5s": pack([apply_fast_dump(r, max_dt=5) for r in shipped]),
        "dump_fast62_15s": pack([apply_fast_dump(r, max_dt=15) for r in shipped]),
        "dump_fast62_30s": pack([apply_fast_dump(r, max_dt=30) for r in shipped]),
        "fade_fast62_15s": pack([apply_fast_fade(r, max_dt=15) for r in shipped]),
        "fade_fast62_30s": pack([apply_fast_fade(r, max_dt=30) for r in shipped]),
        "dump_two_way_20s": pack([apply_two_way_dump(r) for r in shipped]),
        "skip_two_way_entries": pack([r for r in shipped if not r.get("two_way")]),
    }

    # Full-tape-only (uncapped) sensitivity
    shipped_full = []
    for r in full_rows:
        x = dict(r)
        x["pnl"] = r["ship_pnl"]
        x["scratched"] = r["ship_scratched"]
        shipped_full.append(x)
    grid_full = {
        "follow_shipped": pack(shipped_full),
        "fade_every": pack(fade_full),
        "dump_fast62_15s": pack([apply_fast_dump(r, max_dt=15) for r in shipped_full]),
        "fade_fast62_15s": pack([apply_fast_fade(r, max_dt=15) for r in shipped_full]),
        "dump_two_way": pack([apply_two_way_dump(r) for r in shipped_full]),
        "follow_hold": pack([{**r, "scratched": False, "won": r["orig_won"], "pnl": r["hold_pnl"]} for r in full_rows]),
    }

    by_asset_confirm = {}
    for a in CORE:
        xs = [r for r in confirm_holds if r["asset"] == a]
        by_asset_confirm[a] = wr_pnl(xs)

    # Recommendation lock: fade must beat follow on train AND holdout, else no.
    rec = {
        "change_direction": False,
        "keep": "follow first-cross (TWAP side) + BM scratch + dump unconfirmed by 90s",
        "why": [],
    }
    f_ship = grid["follow_scratch_shipped_dump90"]
    f_fade = grid["fade_every_entry_hold_settle"]
    rec["why"].append(
        f"fade_every holdout pnl5={f_fade['holdout']['pnl5']} wr={f_fade['holdout']['take_wr']} vs follow_shipped holdout pnl5={f_ship['holdout']['pnl5']} wr={f_ship['holdout']['take_wr']}"
    )
    if f_fade["robust"] and f_fade["holdout"]["pnl5"] > f_ship["holdout"]["pnl5"] and f_fade["train"]["pnl5"] > f_ship["train"]["pnl5"]:
        rec["change_direction"] = True
        rec["keep"] = "fade every first-cross"
    rec["fast62_beats"] = False
    d15 = grid["dump_fast62_15s"]
    if d15["robust"] and d15["holdout"]["pnl5"] > f_ship["holdout"]["pnl5"] + 20 and d15["train"]["pnl5"] > f_ship["train"]["pnl5"] + 10:
        rec["fast62_beats"] = True
    rec["fade_fast_beats"] = bool(
        grid["fade_fast62_15s"]["robust"]
        and grid["fade_fast62_15s"]["holdout"]["pnl5"] > f_ship["holdout"]["pnl5"]
        and grid["fade_fast62_15s"]["train"]["pnl5"] > f_ship["train"]["pnl5"]
    )

    out = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Does CLOB 拉盤 after a 6bps first-cross justify flipping direction?",
        "n_first_btc_eth": len(first),
        "n_joined": len(rows),
        "n_full_tape": n_full,
        "n_slim_tape": n_slim,
        "n_miss": n_miss,
        "range": {
            "oldest": datetime.fromtimestamp(min(r["end"] for r in rows), timezone.utc).isoformat() if rows else None,
            "newest": datetime.fromtimestamp(max(r["end"] for r in rows), timezone.utc).isoformat() if rows else None,
        },
        "anatomy_full_tape_holds": {
            "all_holds": wr_pnl(holds),
            "confirmed_62": wr_pnl(confirm_holds),
            "never_62": wr_pnl(never),
            "fast62_le15s": wr_pnl(fast15),
            "slow62_gt15s": wr_pnl(slow15),
            "ever_70": wr_pnl(to70),
            "ever_85": wr_pnl(to85),
            "two_way_20s": wr_pnl(two_way_h),
            "one_way": wr_pnl(one_way_h),
            "by_dt62": {k: wr_pnl(v) for k, v in sorted(by_dt.items())},
            "by_asset_confirmed": by_asset_confirm,
            "note": "Hold-to-settle on residual BM holds, full 5–99¢ tape only. Fast 62 = CLOB 拉盤.",
        },
        "hypotheses_all_joined": grid,
        "hypotheses_full_tape_only": grid_full,
        "recommendation": rec,
        "do_not": [
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
            "scratch_adverse_0.08",
            "favorite_97_98",
            "complement",
            "chase_leftover",
        ],
        "ship": False,
        "findings": {
            "headline": "",
            "live_caveat": "Live residual holds are BM-scratch leftovers; fading them is not the same as fading first-cross. Overnight n is too small to flip a 500+ window tape.",
        },
    }
    # fill headline after numbers exist
    c = out["anatomy_full_tape_holds"]["confirmed_62"]
    f = out["anatomy_full_tape_holds"]["fast62_le15s"]
    s = out["anatomy_full_tape_holds"]["slow62_gt15s"]
    out["findings"]["headline"] = (
        f"Full-tape confirmed holds WR {c['wr']} n={c['n']}; "
        f"fast 拉盤≤15s WR {f['wr']} n={f['n']}; slow confirm WR {s['wr']} n={s['n']}. "
        f"Fade-every robust={f_fade['robust']} holdout {f_fade['holdout']['pnl5']} vs follow {f_ship['holdout']['pnl5']}."
    )
    OUT.write_text(json.dumps(out, indent=2))
    print(out["findings"]["headline"], flush=True)
    print("joined", len(rows), "full", n_full, "slim", n_slim, "miss", n_miss, "elapsed", out["elapsed_s"], flush=True)
    print("change_direction", rec["change_direction"], "fast62_beats", rec["fast62_beats"], "fade_fast_beats", rec["fade_fast_beats"], flush=True)
    return out


if __name__ == "__main__":
    run()
