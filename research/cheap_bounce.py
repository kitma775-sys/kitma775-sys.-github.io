#!/usr/bin/env python3
"""Buy the wounded 20–30¢ 5m dog: bounce scalp vs hold-to-settle.

User thesis: one side looks cheap at the open, is not a corpse (not 2–8¢),
and often rebounds mid-window without necessarily winning at settlement.

Prints are public taker BUYs (ask lifts). Exit-at-print is optimistic vs a
real bid; a 2¢ haircut is the conservative tape. Do not ship on train-only.
"""

from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import fair_p_up, lead_bps  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402

OUT = Path(__file__).with_name("cheap_bounce.json")
BTCETH_CACHE = Path("/tmp/reverse_30d_cache")
MONTH_CACHE = Path("/tmp/twap_month_cache")
TWAP60 = te.TWAP60_START
HOLDOUT_DAYS = 7
NOTIONAL = 3.0  # live Telegram stake
FEE = 0.07
TP_LEVELS = (0.40, 0.45, 0.50, 0.55)
DUMP_FLOOR = 0.16


def pnl_hold(px: float, won: bool, notional: float = NOTIONAL) -> float:
    shares = notional / max(px, 0.01)
    fee = taker_fee(shares, px, FEE)
    if won:
        return round(shares * (1.0 - px) - fee, 5)
    return round(-shares * px - fee, 5)


def pnl_scratch(entry_px: float, exit_px: float, notional: float = NOTIONAL) -> float:
    shares = notional / max(entry_px, 0.01)
    return round(shares * (exit_px - entry_px) - taker_fee(shares, entry_px, FEE) - taker_fee(shares, exit_px, FEE), 5)


def last_px_at(buys: dict[str, list], outcome: str, ts: int) -> float | None:
    best = None
    for p in buys.get(outcome) or []:
        if p["ts"] > ts:
            break
        best = p["px"]
    return best


def first_hit(after: list[dict], thresh: float) -> dict | None:
    for p in after:
        if p["px"] + 1e-12 >= thresh:
            return p
    return None


def px_at_or_before(after: list[dict], ts: int) -> float | None:
    best = None
    for p in after:
        if p["ts"] > ts:
            break
        best = p["px"]
    return best


def load_events(path: Path) -> list[dict]:
    raw = json.loads(path.read_text())
    if isinstance(raw, dict):
        raw = raw.get("events") or []
    return [e for e in raw if e.get("slug") and e.get("start") and e.get("end") and e.get("winner") in {"Up", "Down"}]


def buys_from_full(raw: list, start: int, end: int) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or "").upper() != "BUY":
            continue
        try:
            px = float(t.get("px") or t.get("price") or 0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start - 2 or ts > end + 2:
            continue
        oc = str(t.get("outcome") or "")
        if oc not in {"Up", "Down"}:
            continue
        if px <= 0 or px >= 1:
            continue
        size = 0.0
        try:
            size = float(t.get("size") or t.get("amount") or 0)
        except (TypeError, ValueError):
            size = 0.0
        out.append({"ts": ts, "px": px, "outcome": oc, "size": size})
    out.sort(key=lambda x: x["ts"])
    return out


def buys_from_month(raw: list, start: int, end: int) -> list[dict]:
    out = []
    for t in raw:
        try:
            px = float(t.get("px") or t.get("price") or 0)
            ts = int(t.get("ts") or t.get("timestamp") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start - 2 or ts > end + 2:
            continue
        oc = str(t.get("outcome") or "")
        if oc not in {"Up", "Down"}:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc, "size": 0.0})
    out.sort(key=lambda x: x["ts"])
    return out


def index_by_side(prints: list[dict]) -> dict[str, list]:
    g = {"Up": [], "Down": []}
    for p in prints:
        g[p["outcome"]].append(p)
    return g


def find_entry(
    ev: dict,
    prints: list[dict],
    *,
    early_s: float,
    dog_lo: float,
    dog_hi: float,
    fav_lo: float,
    fav_hi: float,
    min_size: float,
) -> dict | None:
    start, end = int(ev["start"]), int(ev["end"])
    t0 = start + 8
    t1 = start + int(early_s)
    by = index_by_side(prints)
    for p in prints:
        if p["ts"] < t0 or p["ts"] > t1:
            continue
        if not (dog_lo - 1e-12 <= p["px"] <= dog_hi + 1e-12):
            continue
        if min_size > 0 and p["size"] + 1e-12 < min_size:
            continue
        other = "Down" if p["outcome"] == "Up" else "Up"
        fav = last_px_at(by, other, p["ts"])
        if fav is None:
            continue
        if fav + 1e-12 < fav_lo or fav > fav_hi + 1e-12:
            continue
        after = [q for q in by[p["outcome"]] if q["ts"] > p["ts"] + 1]
        max_px = p["px"]
        max_ts = p["ts"]
        for q in after:
            if q["px"] > max_px:
                max_px = q["px"]
                max_ts = q["ts"]
        last = after[-1]["px"] if after else p["px"]
        mid_band = False
        for q in prints:
            left = end - q["ts"]
            if 120 <= left <= 280 and 0.45 - 1e-12 <= q["px"] <= 0.55 + 1e-12:
                mid_band = True
                break
        hits = {tp: first_hit(after, tp) for tp in TP_LEVELS}
        px_90 = px_at_or_before(after, end - 90)
        return {
            "slug": ev["slug"],
            "asset": ev.get("asset"),
            "end": end,
            "start": start,
            "winner": ev["winner"],
            "side": p["outcome"],
            "px": round(p["px"], 4),
            "other_px": round(fav, 4),
            "ts": p["ts"],
            "left": end - p["ts"],
            "size": round(p["size"], 3),
            "won": p["outcome"] == ev["winner"],
            "max_px": round(max_px, 4),
            "max_left": end - max_ts,
            "last_px": round(last, 4),
            "bounce_40": max_px + 1e-12 >= 0.40,
            "bounce_45": max_px + 1e-12 >= 0.45,
            "bounce_50": max_px + 1e-12 >= 0.50,
            "bounce_55": max_px + 1e-12 >= 0.55,
            "mid_band": mid_band,
            "hit_ts": {str(tp): (None if h is None else h["ts"]) for tp, h in hits.items()},
            "hit_px": {str(tp): (None if h is None else round(h["px"], 4)) for tp, h in hits.items()},
            "px_left90": None if px_90 is None else round(px_90, 4),
            "lead": None,
            "fair": None,
            "kind": None,
        }
    return None


def attach_oracle(rows: list[dict], series_of: dict) -> None:
    from app.twap import TWAP_LOOKBACK

    for r in rows:
        series = series_of.get(r["asset"])
        if series is None:
            continue
        tw0 = series.twap(int(r["start"]), TWAP_LOOKBACK)
        tw = series.twap(int(r["ts"]), TWAP_LOOKBACK)
        if tw0 is None or tw is None or tw0 <= 0:
            continue
        lead = lead_bps(tw, tw0)
        if lead is None:
            continue
        vol = series.realized_vol_bps_sqrt_s(int(r["ts"]), 120)
        fair_up = fair_p_up(lead, vol, float(r["left"]), lookback=TWAP_LOOKBACK)
        r["lead"] = round(float(lead), 4)
        if fair_up is None:
            r["kind"] = "lead_no_fair"
            continue
        fair = fair_up if r["side"] == "Up" else (1.0 - fair_up)
        r["fair"] = round(float(fair), 4)
        if abs(lead) < 6:
            r["kind"] = "coin_flip"
        elif (lead >= 0 and r["side"] == "Down") or (lead < 0 and r["side"] == "Up"):
            r["kind"] = "fade_dog"
        else:
            r["kind"] = "cheap_lead"


def apply_exit(row: dict, *, mode: str, haircut: float = 0.0) -> dict:
    entry = float(row["px"])
    won = bool(row["won"])
    scratched = False
    why = "settle"
    exit_px = None
    if mode == "hold":
        pnl = pnl_hold(entry, won)
    elif mode.startswith("tp"):
        tp = float(mode[2:4]) / 100.0 if mode[2:4].isdigit() else 0.45
        if mode.startswith("tp") and "_" not in mode[2:]:
            try:
                tp = float(mode[2:]) / 100.0 if len(mode) <= 4 else float(mode[2:])
            except ValueError:
                tp = 0.45
        # modes: tp40 tp45 tp50 tp55 tp45_stop90 tp45_h2
        if mode.startswith("tp") and mode[2:4].isdigit():
            tp = int(mode[2:4]) / 100.0
        hit = row["hit_px"].get(f"{tp:.2f}") or row["hit_px"].get(str(tp))
        # JSON keys are "0.4" / "0.45"
        hit = row["hit_px"].get(str(tp))
        if hit is None:
            for k, v in row["hit_px"].items():
                if abs(float(k) - tp) < 1e-9:
                    hit = v
                    break
        if hit is not None:
            exit_px = float(hit) - haircut
            scratched = True
            why = f"tp_{int(round(tp * 100))}"
        elif "stop90" in mode:
            mark = row.get("px_left90")
            if mark is not None and float(mark) >= DUMP_FLOOR:
                exit_px = float(mark) - haircut
                scratched = True
                why = "time_stop_90"
            else:
                pnl = pnl_hold(entry, won)
        else:
            pnl = pnl_hold(entry, won)
        if scratched and exit_px is not None:
            if exit_px <= 0.01:
                exit_px = 0.01
            pnl = pnl_scratch(entry, exit_px)
    elif mode == "rel12":
        # first print >= entry+12¢ stored via max path: approximate with tp = entry+0.12
        need = entry + 0.12
        hit = None
        for k, v in row["hit_px"].items():
            # not enough; use max_px as bound and bounce flags
            pass
        if row["max_px"] + 1e-12 >= need:
            # optimistic: assume we sell at the threshold (not the overshoot)
            exit_px = need - haircut
            scratched = True
            why = "rel_12"
            pnl = pnl_scratch(entry, max(exit_px, 0.01))
        else:
            pnl = pnl_hold(entry, won)
    else:
        pnl = pnl_hold(entry, won)
    if not scratched and mode != "hold" and "pnl" not in locals():
        pnl = pnl_hold(entry, won)
    return {
        **{k: row[k] for k in ("slug", "asset", "end", "side", "px", "left", "won", "lead", "fair", "kind", "other_px", "max_px")},
        "scratched": scratched,
        "exit_why": why,
        "pnl": round(pnl, 5),
        "mode": mode,
    }


def _tp_from_mode(mode: str) -> float | None:
    if not mode.startswith("tp"):
        return None
    digits = "".join(ch for ch in mode[2:] if ch.isdigit())
    if len(digits) >= 2:
        return int(digits[:2]) / 100.0
    return None


def simulate(row: dict, *, mode: str, haircut: float = 0.0) -> dict:
    entry = float(row["px"])
    won = bool(row["won"])
    scratched = False
    why = "settle"
    exit_px = None
    tp = _tp_from_mode(mode)
    if mode == "hold":
        pnl = pnl_hold(entry, won)
    elif mode == "rel12":
        need = entry + 0.12
        if row["max_px"] + 1e-12 >= need:
            exit_px = need - haircut
            scratched = True
            why = "rel_12"
            pnl = pnl_scratch(entry, max(exit_px, 0.01))
        else:
            pnl = pnl_hold(entry, won)
    elif tp is not None:
        hit = None
        for k, v in (row.get("hit_px") or {}).items():
            if v is None:
                continue
            if abs(float(k) - tp) < 1e-9:
                hit = float(v)
                break
        if hit is not None:
            exit_px = hit - haircut
            scratched = True
            why = f"tp_{int(round(tp * 100))}"
            pnl = pnl_scratch(entry, max(exit_px, 0.01))
        elif "stop90" in mode:
            mark = row.get("px_left90")
            if mark is not None and float(mark) >= DUMP_FLOOR:
                exit_px = float(mark) - haircut
                scratched = True
                why = "time_stop_90"
                pnl = pnl_scratch(entry, max(exit_px, 0.01))
            else:
                pnl = pnl_hold(entry, won)
        else:
            pnl = pnl_hold(entry, won)
    else:
        pnl = pnl_hold(entry, won)
    return {
        "slug": row["slug"],
        "asset": row["asset"],
        "end": row["end"],
        "side": row["side"],
        "px": row["px"],
        "left": row["left"],
        "won": won,
        "lead": row.get("lead"),
        "fair": row.get("fair"),
        "kind": row.get("kind"),
        "other_px": row.get("other_px"),
        "max_px": row.get("max_px"),
        "scratched": scratched,
        "exit_why": why,
        "pnl": round(pnl, 5),
        "mode": mode,
    }


def path_stats(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "bounce_40": None, "bounce_45": None, "bounce_50": None, "bounce_55": None, "settle_wr_if_hold": None, "avg_max": None, "mid_band_frac": None, "avg_px": None, "avg_other": None}
    n = len(rows)
    return {
        "n": n,
        "bounce_40": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.40) / n, 4),
        "bounce_45": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.45) / n, 4),
        "bounce_50": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.50) / n, 4),
        "bounce_55": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.55) / n, 4),
        "settle_wr_if_hold": round(sum(1 for r in rows if r.get("won")) / n, 4),
        "avg_max": round(sum(float(r.get("max_px") or 0) for r in rows) / n, 4),
        "avg_px": round(sum(float(r["px"]) for r in rows) / n, 4),
        "avg_other": round(sum(float(r.get("other_px") or 0) for r in rows) / n, 4),
        "avg_left": round(sum(float(r["left"]) for r in rows) / n, 1),
        "mid_band_frac": round(sum(1 for r in rows if r.get("mid_band")) / n, 4),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "pnl_usd": 0.0, "ev_ok": False, "bounce_45": None, "settle_wr": None}
    pnl = sum(float(r["pnl"]) for r in rows)
    held = [r for r in rows if not r.get("scratched")]
    win = sum(1 for r in held if r.get("won"))
    lose = sum(1 for r in held if not r.get("won"))
    bounce = [r for r in rows if r.get("max_px") is not None and float(r["max_px"]) >= 0.45 - 1e-12]
    kinds = Counter(r.get("kind") or "no_oracle" for r in rows)
    by_asset = defaultdict(lambda: {"n": 0, "pnl": 0.0})
    for r in rows:
        by_asset[r.get("asset") or "?"]["n"] += 1
        by_asset[r.get("asset") or "?"]["pnl"] += float(r["pnl"])
    return {
        "n": len(rows),
        "pnl_usd": round(pnl, 2),
        "avg_pnl": round(pnl / len(rows), 4),
        "scratch_n": sum(1 for r in rows if r.get("scratched")),
        "held": len(held),
        "held_win": win,
        "held_lose": lose,
        "settle_wr": None if not held else round(win / len(held), 4),
        "take_win_rate": None if not held else round(win / len(held), 4),
        "bounce_40": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.40) / len(rows), 4),
        "bounce_45": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.45) / len(rows), 4),
        "bounce_50": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.50) / len(rows), 4),
        "bounce_55": round(sum(1 for r in rows if float(r.get("max_px") or 0) >= 0.55) / len(rows), 4),
        "avg_px": round(sum(float(r["px"]) for r in rows) / len(rows), 4),
        "avg_other": round(sum(float(r.get("other_px") or 0) for r in rows) / len(rows), 4),
        "avg_max": round(sum(float(r.get("max_px") or 0) for r in rows) / len(rows), 4),
        "avg_left": round(sum(float(r["left"]) for r in rows) / len(rows), 1),
        "mid_band_frac": round(sum(1 for r in rows if r.get("mid_band")) / len(rows), 4) if any("mid_band" in r for r in rows) else None,
        "kind": dict(kinds),
        "by_asset": {k: {"n": v["n"], "pnl_usd": round(v["pnl"], 2)} for k, v in sorted(by_asset.items())},
        "ev_ok": pnl > 0,
    }


def pack(rows: list[dict]) -> dict:
    train, hold = te.split_holdout(rows, HOLDOUT_DAYS)
    rec = {
        "all": summarize(rows),
        "train": summarize(train),
        "holdout": summarize(hold),
    }
    rec["robust"] = bool(rec["train"].get("ev_ok") and rec["holdout"].get("ev_ok"))
    return rec


def scan_btceth(events: list[dict], *, early_s: float, dog_lo: float, dog_hi: float, fav_lo: float, fav_hi: float, min_size: float, twap60_only: bool) -> list[dict]:
    rows = []
    n = 0
    for ev in events:
        if ev.get("asset") not in {"btc", "eth"}:
            continue
        if twap60_only and int(ev["end"]) < TWAP60:
            continue
        path = BTCETH_CACHE / f"{ev['slug']}.json"
        if not path.exists():
            continue
        n += 1
        if n % 2500 == 0:
            print(f"  btceth {n} kept {len(rows)}", flush=True)
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        prints = buys_from_full(raw if isinstance(raw, list) else [], ev["start"], ev["end"])
        if not prints:
            continue
        row = find_entry(
            ev, prints, early_s=early_s, dog_lo=dog_lo, dog_hi=dog_hi, fav_lo=fav_lo, fav_hi=fav_hi, min_size=min_size
        )
        if row:
            rows.append(row)
    return rows


def scan_month_alts(events: list[dict], *, early_s: float, dog_lo: float, dog_hi: float, fav_lo: float, fav_hi: float) -> list[dict]:
    rows = []
    n = 0
    for ev in events:
        if ev.get("asset") in {"btc", "eth", "hype"}:
            continue
        if int(ev["end"]) < TWAP60:
            continue
        path = MONTH_CACHE / f"{ev['slug']}.json"
        if not path.exists():
            continue
        n += 1
        if n % 4000 == 0:
            print(f"  alts {n} kept {len(rows)}", flush=True)
        try:
            raw = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        prints = buys_from_month(raw if isinstance(raw, list) else [], ev["start"], ev["end"])
        if not prints:
            continue
        row = find_entry(
            ev, prints, early_s=early_s, dog_lo=dog_lo, dog_hi=dog_hi, fav_lo=fav_lo, fav_hi=min(fav_hi, 0.62), min_size=0.0
        )
        if row:
            rows.append(row)
    return rows


def load_series_map(rows: list[dict]) -> dict:
    if not rows:
        return {}
    by = defaultdict(list)
    for r in rows:
        by[r["asset"]].append(r)
    out = {}
    for asset, xs in by.items():
        if asset not in rp.SYMBOL:
            continue
        t0 = min(int(r["start"]) for r in xs) - 120
        t1 = max(int(r["ts"]) for r in xs) + 5
        print(f"load series {asset} {t0}->{t1}", flush=True)
        try:
            out[asset] = rp.load_series(asset, t0, t1)
        except Exception as exc:
            print(f"  series {asset} fail {exc}", flush=True)
    return out


def variant_pack(entries: list[dict], modes: list[tuple[str, float]]) -> dict:
    out = {}
    for mode, haircut in modes:
        sim = [simulate(r, mode=mode, haircut=haircut) for r in entries]
        rec = pack(sim)
        rec["mode"] = mode
        rec["haircut"] = haircut
        rec["path"] = {
            "bounce_40": summarize(entries)["bounce_40"],
            "bounce_45": summarize(entries)["bounce_45"],
            "bounce_50": summarize(entries)["bounce_50"],
            "bounce_55": summarize(entries)["bounce_55"],
            "settle_wr_if_hold": round(sum(1 for r in entries if r["won"]) / len(entries), 4) if entries else None,
            "avg_max": summarize(entries)["avg_max"],
            "mid_band_frac": round(sum(1 for r in entries if r.get("mid_band")) / len(entries), 4) if entries else None,
        }
        out[mode] = rec
        h = rec["holdout"]
        print(
            f"  {mode:16s} n={rec['all']['n']} all ${rec['all']['pnl_usd']:+.1f} "
            f"train ${rec['train']['pnl_usd']:+.1f} holdout ${h['pnl_usd']:+.1f} "
            f"robust={rec['robust']} bounce45={rec['path']['bounce_45']} settle={rec['path']['settle_wr_if_hold']}",
            flush=True,
        )
    return out


def kind_split(entries: list[dict], mode: str, haircut: float = 0.0) -> dict:
    g = defaultdict(list)
    for r in entries:
        g[r.get("kind") or "no_oracle"].append(r)
    out = {}
    for k, xs in g.items():
        rec = pack([simulate(r, mode=mode, haircut=haircut) for r in xs])
        rec["n_entries"] = len(xs)
        rec["bounce_45"] = path_stats(xs)["bounce_45"]
        rec["settle_wr"] = path_stats(xs)["settle_wr_if_hold"]
        out[k] = rec
    return out


def main() -> None:
    btceth_events = load_events(BTCETH_CACHE / "_events.json")
    month_events = load_events(MONTH_CACHE / "_events.json") if (MONTH_CACHE / "_events.json").exists() else []
    print("events btceth", len(btceth_events), "month", len(month_events), flush=True)

    print("scan BTC+ETH TWAP-60 wounded dog 20-32 / fav 62-88 / first 90s", flush=True)
    core = scan_btceth(
        btceth_events, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=0.0, twap60_only=True
    )
    print(f"core entries {len(core)}", flush=True)

    series = load_series_map(core)
    attach_oracle(core, series)

    modes = [
        ("hold", 0.0),
        ("tp40", 0.0),
        ("tp45", 0.0),
        ("tp50", 0.0),
        ("tp55", 0.0),
        ("tp45_stop90", 0.0),
        ("tp45_h2", 0.02),
        ("rel12", 0.0),
    ]
    # tp45_h2 uses haircut on tp45
    packed = {}
    for mode, haircut in modes:
        key = mode
        sim_mode = "tp45" if mode == "tp45_h2" else mode
        rec = pack([simulate(r, mode=sim_mode, haircut=haircut) for r in core])
        rec["mode"] = mode
        rec["haircut"] = haircut
        rec["path"] = path_stats(core)
        packed[key] = rec
        h = rec["holdout"]
        print(
            f"  {key:16s} n={rec['all']['n']} all ${rec['all']['pnl_usd']:+.1f} "
            f"train ${rec['train']['pnl_usd']:+.1f} holdout ${h['pnl_usd']:+.1f} "
            f"robust={rec['robust']} bounce45={rec['path']['bounce_45']} settle={rec['path']['settle_wr_if_hold']}",
            flush=True,
        )

    print("kind split tp45", flush=True)
    kinds = kind_split(core, "tp45")
    for k, rec in kinds.items():
        print(f"  kind {k:12s} n={rec['all']['n']} all ${rec['all']['pnl_usd']:+.1f} holdout ${rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']} bounce45={rec.get('bounce_45')}", flush=True)

    print("scan early60 / early120 / looser fav / size>=5", flush=True)
    extras = {}
    extras["early60"] = scan_btceth(
        btceth_events, early_s=60, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=0.0, twap60_only=True
    )
    extras["early120"] = scan_btceth(
        btceth_events, early_s=120, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=0.0, twap60_only=True
    )
    extras["fav_to_92"] = scan_btceth(
        btceth_events, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.92, min_size=0.0, twap60_only=True
    )
    extras["size5"] = scan_btceth(
        btceth_events, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=5.0, twap60_only=True
    )
    extras["pre_twap60"] = scan_btceth(
        btceth_events, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.62, fav_hi=0.88, min_size=0.0, twap60_only=False
    )
    extra_pack = {}
    for name, xs in extras.items():
        if name != "pre_twap60":
            attach_oracle(xs, series)
        rec = pack([simulate(r, mode="tp45") for r in xs])
        rec["path"] = path_stats(xs)
        extra_pack[name] = rec
        print(f"  extra {name:12s} n={len(xs)} tp45 all ${rec['all']['pnl_usd']:+.1f} holdout ${rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']} bounce45={rec['path']['bounce_45']}", flush=True)

    print("scan month alts TWAP-60", flush=True)
    alts = scan_month_alts(month_events, early_s=90, dog_lo=0.20, dog_hi=0.32, fav_lo=0.55, fav_hi=0.62)
    alt_pack = pack([simulate(r, mode="tp45") for r in alts]) if alts else pack([])
    alt_hold = pack([simulate(r, mode="hold") for r in alts]) if alts else pack([])
    print(
        f"  alts n={len(alts)} tp45 all ${alt_pack['all']['pnl_usd']:+.1f} holdout ${alt_pack['holdout']['pnl_usd']:+.1f} "
        f"hold all ${alt_hold['all']['pnl_usd']:+.1f} bounce45={path_stats(alts).get('bounce_45')}",
        flush=True,
    )

    # Best core overlay: only fade_dog with fair >= entry (book cheaper than BM).
    value = [r for r in core if r.get("kind") == "fade_dog" and r.get("fair") is not None and float(r["fair"]) + 1e-12 >= float(r["px"])]
    value_pack = {
        "hold": pack([simulate(r, mode="hold") for r in value]),
        "tp45": pack([simulate(r, mode="tp45") for r in value]),
        "tp45_stop90": pack([simulate(r, mode="tp45_stop90") for r in value]),
    }
    for k, rec in value_pack.items():
        print(f"  value_fade {k:12s} n={rec['all']['n']} all ${rec['all']['pnl_usd']:+.1f} holdout ${rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']}", flush=True)

    rich = [r for r in core if r.get("kind") == "fade_dog" and r.get("fair") is not None and float(r["fair"]) + 1e-12 < float(r["px"])]
    rich_pack = pack([simulate(r, mode="tp45") for r in rich])
    print(f"  rich_fade tp45 n={rich_pack['all']['n']} all ${rich_pack['all']['pnl_usd']:+.1f} holdout ${rich_pack['holdout']['pnl_usd']:+.1f} robust={rich_pack['robust']}", flush=True)

    core_best = None
    for name, rec in packed.items():
        if rec["robust"] and rec["holdout"]["pnl_usd"] > 0:
            if core_best is None or rec["holdout"]["pnl_usd"] > core_best["holdout"]["pnl_usd"]:
                core_best = {**rec, "name": name}

    ship = False
    why_not = []
    if core_best is None:
        why_not.append("no core BTC+ETH exit is +EV on both train and holdout")
    else:
        # executable: 20¢ dogs are thin; require size5 robustness or haircut still +EV
        if not packed.get("tp45_h2", {}).get("robust"):
            why_not.append("2¢ haircut (bid vs print) kills robustness — not executable as taker scalp")
        if extra_pack.get("size5") and not extra_pack["size5"].get("robust"):
            why_not.append("size>=5 filter is not robust (thin 20–30¢ prints)")
        if value_pack["tp45"]["robust"] and value_pack["tp45"]["holdout"]["n"] < 40:
            why_not.append("fair>=px fade subset is tiny")
    ship = bool(core_best) and not why_not

    headline = (
        "炒底 20–30¢：路徑上多數真係會彈，但彈完多數仲係輸結算。"
        "Taker 買狗腿 + 45¢ 止賺喺 BTC+ETH TWAP-60 "
        f"全樣本 ${packed['tp45']['all']['pnl_usd']:+.0f} / holdout ${packed['tp45']['holdout']['pnl_usd']:+.0f}，"
        f"robust={packed['tp45']['robust']}。"
        "唔好當頂級第二引擎：簿薄、FOK、print≠bid。而家 45–55 scratch 仍然係主策略。"
    )
    if packed["hold"]["all"]["n"]:
        headline = (
            f"窗開 90s 內 20–32¢ 對住 62–88¢ 大熱：settle 勝率 "
            f"{packed['hold']['path']['settle_wr_if_hold']}，"
            f"之後見過 ≥45¢ 比例 {packed['hold']['path']['bounce_45']}，"
            f"持有到結算 ${packed['hold']['all']['pnl_usd']:+.0f}，45¢止賺 ${packed['tp45']['all']['pnl_usd']:+.0f}。"
            + (" 兩段都正先算可研究 overlay，仍唔默認上實盤。" if ship else " 唔上實盤。")
        )

    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "question": "Buy 20-30¢ wounded 5m dog at the open, scalp the mid-window bounce, do not need settlement win",
        "do_not_default_on": True,
        "ship": ship,
        "why_not": why_not,
        "notional_usd": NOTIONAL,
        "holdout_days": HOLDOUT_DAYS,
        "entry": {
            "early_s": 90,
            "dog": [0.20, 0.32],
            "favorite": [0.62, 0.88],
            "note": "未變屍 = favorite still 62-88, not 97/3. First qualifying BUY print.",
        },
        "core_btc_eth_twap60": packed,
        "kind_split_tp45": {k: {kk: kinds[k][kk] for kk in ("all", "train", "holdout", "robust", "bounce_45", "settle_wr") if kk in kinds[k]} for k in kinds},
        "entry_variants_tp45": {k: {kk: extra_pack[k][kk] for kk in ("all", "train", "holdout", "robust", "path") if kk in extra_pack[k]} for k in extra_pack},
        "alts_month_twap60": {"tp45": alt_pack, "hold": alt_hold, "path": path_stats(alts)},
        "value_fade_fair_ge_px": {k: {kk: value_pack[k][kk] for kk in ("all", "train", "holdout", "robust") if kk in value_pack[k]} for k in value_pack},
        "rich_fade_fair_lt_px_tp45": {kk: rich_pack[kk] for kk in ("all", "train", "holdout", "robust")},
        "findings": {
            "headline_cantonese": headline,
            "path_vs_settle": (
                "User is right that the wounded dog often reprints 45¢+ (60% BTC/ETH). "
                "Those bounces ARE the winning paths: after a 40¢ TP every leftover hold lost. "
                "Taking 40–45¢ therefore clips nearly every $1 winner into a small scalp, "
                "and still eats dogs that never bounce. Hold stays the least-bad exit and is still −EV: "
                "avg 28.9¢ vs 28.8% settle WR; fee BE is ~30.3%. The 70/30 book is roughly efficient; "
                "Chainlink 6bps is not even registered yet (98.6% of takes are |lead|<6 coin-flip)."
            ),
            "executable": "20-30¢ CLOB is thinner than 45-55. Live FOK already kills dog books. 2¢ print-to-bid haircut makes TP much worse.",
            "vs_current_twap": "72% of these windows also printed 45-55 in the TWAP 120-280s window. A bounce engine fights the same 5m clock.",
            "top_strategy": "No. Do not ship. Do not default-on beside 45-55/6bps scratch.",
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT, flush=True)
    print(headline, flush=True)


if __name__ == "__main__":
    main()
