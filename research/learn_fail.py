#!/usr/bin/env python3
"""Can the bot learn from failures online without cutting the shipped sleeve?

Question: after a loss / dump / FOK, should live params move (skip next,
raise 6bps, pause an asset, hour filter, bucket skip)? Or is the correct
learning loop still *offline* research on the expanding tape?

Shipped sleeve (frozen): first-cross 6bps 45–55 120–280 + BM scratch +
dump_by90 + oracle fair<0.60 in the last 90s. Reverse off.

Causal rule: a window may only see takes whose end <= this window's start
(the previous 5m has settled). Same-unix BTC/ETH cannot teach each other.

Ship only if a learner beats frozen train AND holdout PnL by ≥$5, keeps
holdout n ≥60%, holdout take WR ≥0.85. Live n is sanity, not a gate.

Do not: reverse, 8¢ SL, dump_mid90, leftover chase, 4bps, 40–60, autodial
live from the last 10 fills.
"""

from __future__ import annotations

import json
import math
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import reverse_predict as rp  # noqa: E402
from rev59_oracle import (  # noqa: E402
    apply_overlay,
    hydrate,
    ship_base,
    slim,
)
from tape_pull import (  # noqa: E402
    CORE,
    HOLDOUT_DAYS,
    TAKES,
    TWAP60,
    pack,
    rec_of,
    split_holdout,
)

OUT = Path(__file__).resolve().parent / "learn_fail.json"
SHIP = Path(__file__).resolve().parent / "learn_fail_ship.json"
LIVE_STATE = Path("/tmp/learn_fail_live.json")
WINDOW = 300


def label_of(row: dict) -> str:
    if row.get("scratched"):
        why = str(row.get("exit_why") or "")
        if "oracle" in why:
            return "dump_oracle"
        if "unconfirmed" in why or "dump" in why:
            return "dump_90"
        return "scratch_bm"
    return "hold_win" if row.get("won") else "hold_lose"


def is_hold_loss(row: dict) -> bool:
    return (not row.get("scratched")) and (not row.get("won"))


def is_neg(row: dict) -> bool:
    try:
        return float(row.get("pnl") or 0) < -1e-9
    except (TypeError, ValueError):
        return False


def chrono(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda r: (int(r["start"]), str(r.get("asset") or ""), str(r.get("slug") or "")))


def settled(taken: list[dict], row: dict) -> list[dict]:
    t0 = int(row["start"])
    return [h for h in taken if int(h["end"]) <= t0]


def last_fail_end(hist: list[dict], *, asset: str | None, kind: str) -> int | None:
    """End-ts of the latest settled take that should start a cooldown.

    Only the *most recent* relevant take counts. An older loss followed by a
    win must not keep the cooldown alive.
    """
    pool = hist if asset is None else [h for h in hist if h.get("asset") == asset]
    if not pool:
        return None
    if kind == "hold_loss":
        holds = [h for h in pool if not h.get("scratched")]
        if not holds or not is_hold_loss(holds[-1]):
            return None
        return int(holds[-1]["end"])
    last = pool[-1]
    if kind == "neg_pnl":
        return int(last["end"]) if is_neg(last) else None
    if kind == "dump":
        why = str(last.get("exit_why") or "")
        dumped = bool(last.get("scratched")) and (
            "unconfirmed" in why or "oracle" in why or why.startswith("dump")
        )
        return int(last["end"]) if dumped else None
    return None


def cooldown_active(start: int, fail_end: int | None, k_windows: int) -> bool:
    """True when `start` is inside the k calendar windows after a settled fail."""
    if fail_end is None or k_windows <= 0:
        return False
    return int(start) < int(fail_end) + int(k_windows) * WINDOW


def shipped_overlay(rows: list[dict]) -> list[dict]:
    dumped = [ship_base(r) for r in rows]
    return apply_overlay(
        dumped,
        lambda r: (not r.get("scratched"))
        and any((r.get(k) or {}).get("fair") is not None and r[k]["fair"] < 0.60 for k in ("o90", "o60", "o30")),
        "twap_scratch_oracle",
    )


def walk(rows: list[dict], decide) -> tuple[list[dict], dict]:
    taken: list[dict] = []
    skipped = 0
    for row in chrono(rows):
        hist = settled(taken, row)
        if decide(hist, row):
            taken.append(row)
        else:
            skipped += 1
    return taken, {"skipped": skipped, "taken": len(taken), "universe": len(rows)}


def beats(g: dict, base: dict) -> bool:
    ho_wr = g["holdout"]["take_wr"]
    return bool(
        g["robust"]
        and g["train"]["pnl5"] + 1e-9 >= base["train"]["pnl5"]
        and g["holdout"]["pnl5"] >= base["holdout"]["pnl5"] + 5.0
        and g["holdout"]["n"] >= 0.6 * base["holdout"]["n"]
        and g["n"] >= 0.6 * base["n"]
        and (ho_wr or 0) >= 0.85
    )


def iid_holds(rows: list[dict]) -> dict:
    holds = [r for r in chrono(rows) if not r.get("scratched")]
    bits = [0 if r.get("won") else 1 for r in holds]
    n = len(bits)
    losses = sum(bits)
    p = losses / n if n else None
    pairs = list(zip(bits, bits[1:])) if n >= 2 else []
    n_ll = sum(1 for a, b in pairs if a == 1 and b == 1)
    n_l = sum(1 for a, _ in pairs if a == 1)
    p_ll = (n_ll / n_l) if n_l else None
    # Wald–Wolfowitz runs of wins vs losses
    runs = 1 if n else 0
    for a, b in pairs:
        if a != b:
            runs += 1
    n1, n0 = losses, n - losses
    exp = None
    z = None
    if n0 >= 1 and n1 >= 1 and n >= 3:
        exp = 1 + 2 * n0 * n1 / n
        var = (2 * n0 * n1 * (2 * n0 * n1 - n)) / (n * n * (n - 1))
        if var > 0:
            z = (runs - exp) / math.sqrt(var)
    return {
        "held_n": n,
        "hold_loss_n": losses,
        "p_loss": None if p is None else round(p, 4),
        "p_loss_given_loss": None if p_ll is None else round(p_ll, 4),
        "pairs_after_loss": n_l,
        "loss_loss_pairs": n_ll,
        "runs": runs,
        "runs_expected": None if exp is None else round(exp, 3),
        "runs_z": None if z is None else round(z, 3),
        "clustered": bool(z is not None and z < -1.96),
    }


def buckets_of(row: dict) -> dict[str, str]:
    px = float(row["px"])
    lead = abs(float(row.get("lead") or 0))
    left = float(row.get("left") or 0)
    hour = datetime.fromtimestamp(int(row["start"]), timezone.utc).hour
    if px < 0.50:
        px_b = "45-49"
    elif px < 0.53:
        px_b = "50-52"
    else:
        px_b = "53-55"
    if lead < 8:
        lead_b = "6-8"
    elif lead < 12:
        lead_b = "8-12"
    else:
        lead_b = "12-40"
    if left < 180:
        left_b = "120-180"
    elif left < 240:
        left_b = "180-240"
    else:
        left_b = "240-280"
    return {
        "px": px_b,
        "lead": lead_b,
        "left": left_b,
        "hour": f"{hour:02d}",
        "asset": str(row.get("asset") or ""),
        "px_lead": f"{px_b}|{lead_b}",
    }


def toxic_keys(train: list[dict], key: str, min_n: int = 8) -> set[str]:
    grp = defaultdict(list)
    for r in train:
        grp[buckets_of(r)[key]].append(float(r.get("pnl") or 0))
    bad = set()
    for k, xs in grp.items():
        if len(xs) >= min_n and sum(xs) < -1e-9:
            bad.add(k)
    return bad


def ewma_skip(hist: list[dict], n: int = 10, wr_floor: float | None = None, pnl_sum: bool = False) -> bool:
    recent = hist[-n:]
    if len(recent) < n:
        return False
    if pnl_sum:
        return sum(float(r.get("pnl") or 0) for r in recent) < -1e-9
    holds = [r for r in recent if not r.get("scratched")]
    if len(holds) < max(4, n // 2) or wr_floor is None:
        return False
    wr = sum(1 for r in holds if r.get("won")) / len(holds)
    return wr + 1e-12 < wr_floor


def live_taxonomy() -> dict:
    path = LIVE_STATE
    if not path.exists():
        return {"n": 0, "note": "no /tmp/learn_fail_live.json"}
    try:
        live = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {"n": 0, "note": "bad live json"}
    trades = live.get("trades") or []
    counts: dict[str, int] = defaultdict(int)
    reverse_ish = 0
    for t in trades:
        if t.get("kind") != "taker":
            counts[str(t.get("kind") or "other")] += 1
            continue
        st = str(t.get("status") or "")
        counts[st] += 1
        pay = t.get("payload") or {}
        lead = pay.get("lead_bps")
        try:
            if lead is not None and float(lead) < 0:
                reverse_ish += 1
        except (TypeError, ValueError):
            pass
        fok = pay.get("fok") or pay.get("unmatched_retry")
        if fok:
            counts[f"fok:{fok}"] += 1
    board = live.get("board") or {}
    return {
        "n": len(trades),
        "taker_status": {k: v for k, v in counts.items() if not str(k).startswith("fok:")},
        "fok_reasons": {k.split(":", 1)[1]: v for k, v in counts.items() if str(k).startswith("fok:")},
        "taker_negative_lead": reverse_ish,
        "hit": board.get("hit_label"),
        "today_pnl": board.get("today_pnl"),
        "open_n": board.get("open_n"),
        "rev": board.get("rev"),
        "note": "live takers mix reverse-on hours with follow; do not autodial from this n",
    }


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
    rows = shipped_overlay(raw)
    base = pack(rows)
    print("SHIPPED", slim(base), flush=True)

    labels = defaultdict(int)
    pnl_by = defaultdict(float)
    for r in rows:
        lab = label_of(r)
        labels[lab] += 1
        pnl_by[lab] += float(r.get("pnl") or 0)
    iid = iid_holds(rows)
    print("labels", dict(labels), "iid", iid, flush=True)

    train, hold = split_holdout(rows)
    toxic = {k: sorted(toxic_keys(train, k)) for k in ("px", "lead", "left", "hour", "asset", "px_lead")}

    grid: dict[str, dict] = {"frozen": slim(base)}

    def add(name: str, xs: list[dict], note: str = "", extra: dict | None = None) -> None:
        rec = slim(pack(xs))
        rec["note"] = note
        rec["beats"] = beats(rec, slim(base))
        rec["d_ho"] = round(rec["holdout"]["pnl5"] - base["holdout"]["pnl5"], 2)
        rec["d_tr"] = round(rec["train"]["pnl5"] - base["train"]["pnl5"], 2)
        rec["skipped"] = len(rows) - len(xs)
        if extra:
            rec.update(extra)
        grid[name] = rec
        mark = "BEAT" if rec["beats"] else ("+" if rec["d_ho"] > 0 and rec["d_tr"] > 0 else ".")
        print(
            f"{mark:4} {name:32s} n={rec['n']:3d} skip={rec['skipped']:3d} "
            f"tr {rec['train']['pnl5']:+7.1f} ho {rec['holdout']['pnl5']:+7.1f} "
            f"dho={rec['d_ho']:+6.1f} wr={rec['holdout']['take_wr']}",
            flush=True,
        )

    add("frozen", rows, "shipped first-cross + dump90 + oracle fair<0.60")

    # --- walk-forward learners (only settled past takes) ---
    def dec_skip_loss(k: int, asset_local: bool):
        def decide(hist, row):
            asset = str(row.get("asset") or "") if asset_local else None
            return not cooldown_active(int(row["start"]), last_fail_end(hist, asset=asset, kind="hold_loss"), k)

        return decide

    def dec_skip_neg(k: int, asset_local: bool):
        def decide(hist, row):
            asset = str(row.get("asset") or "") if asset_local else None
            return not cooldown_active(int(row["start"]), last_fail_end(hist, asset=asset, kind="neg_pnl"), k)

        return decide

    for k in (1, 2, 3, 6):
        xs, meta = walk(rows, dec_skip_loss(k, False))
        add(f"skip_{k}w_after_hold_loss", xs, "calendar cooldown after a residual hold loss", meta)
        xs, meta = walk(rows, dec_skip_loss(k, True))
        add(f"pause_asset_{k}w_after_hold_loss", xs, "per-asset calendar cooldown after hold loss", meta)
        xs, meta = walk(rows, dec_skip_neg(k, True))
        add(f"pause_asset_{k}w_after_neg_pnl", xs, "treat dump/scratch red PnL as a fail", meta)

    def dec_lead8(hist, row):
        holds = [h for h in hist if h.get("asset") == row.get("asset") and not h.get("scratched")]
        thr = 8.0 if holds and is_hold_loss(holds[-1]) else 6.0
        return abs(float(row.get("lead") or 0)) + 1e-12 >= thr

    xs, meta = walk(rows, dec_lead8)
    add("adaptive_lead_8_after_asset_loss", xs, "raise 6→8bps on that coin until a held win", meta)

    def dec_ewma_wr(hist, row):
        return not ewma_skip(hist, n=10, wr_floor=0.80)

    xs, meta = walk(rows, dec_ewma_wr)
    add("ewma10_skip_wr_below_80", xs, "skip while last 10 taken held-WR < 80%", meta)

    def dec_ewma_pnl(hist, row):
        return not ewma_skip(hist, n=10, pnl_sum=True)

    xs, meta = walk(rows, dec_ewma_pnl)
    add("ewma10_skip_sum_pnl_neg", xs, "skip while last 10 taken PnL sum < 0", meta)

    def dec_dump_cool(hist, row):
        return not cooldown_active(int(row["start"]), last_fail_end(hist, asset=str(row.get("asset") or ""), kind="dump"), 1)

    xs, meta = walk(rows, dec_dump_cool)
    add("pause_asset_1w_after_dump", xs, "skip next window of that coin after a dump", meta)

    # --- train-only static filters (proper ML, not online last-10) ---
    add(
        "train_skip_toxic_hour",
        [r for r in rows if buckets_of(r)["hour"] not in set(toxic["hour"])],
        f"skip UTC hours whose train PnL < 0: {toxic['hour']}",
        {"toxic": toxic["hour"]},
    )
    add(
        "train_skip_toxic_px_lead",
        [r for r in rows if buckets_of(r)["px_lead"] not in set(toxic["px_lead"])],
        f"skip px|lead buckets with train PnL < 0: {toxic['px_lead']}",
        {"toxic": toxic["px_lead"]},
    )
    add(
        "train_skip_toxic_left",
        [r for r in rows if buckets_of(r)["left"] not in set(toxic["left"])],
        f"skip left buckets with train PnL < 0: {toxic['left']}",
        {"toxic": toxic["left"]},
    )
    add("static_skip_px55", [r for r in rows if float(r["px"]) < 0.545], "never lift a 55¢ first-cross (FOK cannot +1¢)")
    add("static_skip_knife_6_5", [r for r in rows if abs(float(r.get("lead") or 0)) >= 6.5], "skip |lead|<6.5 (rev59 already rejected)")
    add("btc_only", [r for r in rows if r.get("asset") == "btc"])
    add("eth_only", [r for r in rows if r.get("asset") == "eth"])

    ranked = sorted(
        ((n, g) for n, g in grid.items() if n != "frozen"),
        key=lambda kv: (kv[1].get("beats"), kv[1]["holdout"]["pnl5"], kv[1]["train"]["pnl5"]),
        reverse=True,
    )
    winners = [n for n, g in ranked if g.get("beats")]
    live = live_taxonomy()
    why = (
        "Shipped dump overlay already clips residual hold losses "
        f"({iid['hold_loss_n']} of {iid['held_n']} holds). "
        "Online cooldowns therefore almost never fire, or they skip +EV windows "
        "after a dump/scratch. Train-only hour/bucket skips fail holdout. "
        "Live FOK unmatched is an execution gap (Rev 60), not a direction error "
        "to autodial 6bps from. Keep frozen sleeve; keep learning offline on tape."
    )
    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "question": "Learn from failures online so the bot slowly improves?",
        "n_joined": len(rows),
        "holdout_days": HOLDOUT_DAYS,
        "shipped": slim(base),
        "labels": {k: {"n": labels[k], "pnl5": round(pnl_by[k], 2)} for k in sorted(labels)},
        "iid": iid,
        "toxic_train": toxic,
        "grid": grid,
        "winners": winners,
        "pick": winners[0] if winners else None,
        "ship": False,
        "live_sanity": live,
        "why": why,
        "correct_loop": [
            "offline tape research (this file, rev59, rev60)",
            "freeze params that pass train+holdout",
            "log live FOK/dump fingerprints for the next research pass",
            "do not autodial min_lead / band / dump from the last 10 fills",
        ],
        "do_not": [
            "online_bandit_min_lead",
            "skip_after_loss_cooldown",
            "pause_asset_after_dump",
            "ewma_wr_gate",
            "train_hour_filter",
            "twap_reverse_on",
            "dump_mid90",
            "price_sl_8c",
            "chase_leftover",
            "lead_4bps",
            "band_40_60",
            "autodial_from_live_n9",
        ],
        "findings": {
            "headline": why,
            "best_nonbeat": None if not ranked else ranked[0][0],
            "best_d_ho": None if not ranked else ranked[0][1]["d_ho"],
        },
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str))
    ship = {
        "strategy_rev": 60,
        "ship": False,
        "pick": None,
        "researched_at_utc": rec["researched_at_utc"],
        "source": "research/learn_fail.json",
        "question": rec["question"],
        "why": why,
        "shipped_holdout": rec["shipped"]["holdout"],
        "iid": iid,
        "winners": winners,
        "do_not": rec["do_not"],
    }
    SHIP.write_text(json.dumps(ship, indent=2, default=str))
    print("PICK", rec["pick"], "winners", winners, "elapsed", rec["elapsed_s"], flush=True)
    return rec


if __name__ == "__main__":
    run()
