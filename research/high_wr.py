#!/usr/bin/env python3
"""High-WR R&D without touching live.

Live held-to-settle is ~8W/38L (17%). Research first/last 6bps on the BTC+ETH
CLOB print tape is ~90%. The gap is *which* 6bps quote we hold, not 6bps itself.

Levers (not 8¢ SL, not 20–30¢ bounce, not dump-more-blindly):
  1. First-cross / refuse cheaper leftover after the first 6bps print.
  2. Book-confirm: if the CLOB never prints ≥62¢ (or is still mid-band at
     90s left), dump the residual hold BM scratch left behind.

User said 暫時不動. ship is always false here. A variant is a *candidate*
only if train and holdout are both +EV, hold take WR ≥70%, and holdout PnL
does not collapse vs last-take + BM scratch.

Prints are public taker BUYs (asks lifted). Confirm dumps that sell at a
print are optimistic; a 2¢ haircut is the conservative tape.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import (  # noqa: E402
    TwapParams,
    entry_edge,
    fair_p_up,
    lead_bps,
    should_scratch,
)
import full_coin_month as fcm  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402

OUT = Path(__file__).resolve().parent / "high_wr.json"
REV_CACHE = Path("/tmp/reverse_30d_cache")
MONTH_CACHE = Path("/tmp/twap_month_cache")
LIVE_HOLDS = Path("/tmp/live_holds.json")
LIVE_HOLDS_FULL = Path("/tmp/live_holds_full.json")
LIVE_PRINTS = Path("/tmp/live_hold_prints")
TWAP60 = te.TWAP60_START
NOTIONAL = te.NOTIONAL
RESCORE = te.RESCORE
HOLDOUT_DAYS = 7
CONFIRM_PX = 0.62
MID_STILL = 0.60
CONFIRM_LEFT = 90
LATE_LEFT = 150
HAIRCUT = 0.02
UA = {
    "User-Agent": "surf-arb-research/high-wr (read-only; no trading)",
    "Accept": "application/json",
}
GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
DO_NOT_SHIP = {
    "scratch_adverse_0.08",
    "btc_eth_min_left_180",
    "cut_telegram_coins",
    "price_sl_8c",
    "cheap_bounce_20_30",
    "complement",
    "favorite_97_98",
    "min_fair_gate",
    "vol_scale",
    "pick_by_edge",
    "twap_reverse_on",
}


def http_json(url: str, timeout: float = 25.0, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                return None
            if exc.code in {429, 500, 502, 503, 422}:
                time.sleep(0.35 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.25 * (2**i))
    if last:
        raise last
    return None


def buys(raw: list, start: int, end: int, *, lo: float, hi: float) -> list[dict]:
    out = []
    for t in raw:
        if str(t.get("side") or "BUY").upper() != "BUY":
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


def path_features(full: list[dict], side: str, t_fill: int, end: int) -> dict:
    after = [p for p in full if p["outcome"] == side and p["ts"] >= t_fill]
    max_px = max((p["px"] for p in after), default=0.0)
    last_px = after[-1]["px"] if after else None
    last_live = None
    for p in after:
        if p["ts"] <= end - 8:
            last_live = p["px"]
    by90 = end - CONFIRM_LEFT
    ever_62_by90 = any(p["px"] + 1e-12 >= CONFIRM_PX and p["ts"] <= by90 for p in after)
    px90 = None
    for p in after:
        if p["ts"] <= by90:
            px90 = p["px"]
        else:
            if px90 is None:
                px90 = p["px"]
            break
    ever = {}
    for thr in (0.58, 0.62, 0.70, 0.85):
        ever[f"ever_{int(round(thr * 100))}"] = any(p["px"] + 1e-12 >= thr for p in after)
    still_mid = px90 is not None and px90 < MID_STILL
    still_band = px90 is not None and px90 < 0.55
    return {
        "max_after": round(max_px, 4),
        "last_after": None if last_px is None else round(last_px, 4),
        "last_live": None if last_live is None else round(last_live, 4),
        "px90": None if px90 is None else round(px90, 4),
        "still_mid90": still_mid,
        "still_in_band90": still_band,
        "ever_62_by90": ever_62_by90,
        **ever,
        "n_after": len(after),
    }


def pick_candidates(ev, series, band: list[dict], params: TwapParams) -> dict:
    start, end = int(ev["start"]), int(ev["end"])
    tw_open = series.twap(start, params.lookback)
    if tw_open is None or tw_open <= 0:
        return {"first": None, "last": None}
    t0 = start + 15
    t1 = end - int(params.min_left)
    first = last = None
    for ts in range(t0, t1 + 1, 5):
        left = end - ts
        if left > params.max_left or left < params.min_left:
            continue
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open)
        if lead is None or abs(lead) < params.min_lead_bps:
            continue
        if abs(lead) > params.max_lead_bps + 1e-12:
            continue
        side = "Up" if lead >= 0 else "Down"
        pr = te.last_print(band, ts, side, slack=25)
        if pr is None:
            continue
        if not (params.min_price - 1e-12 <= pr["px"] <= params.max_price + 1e-12):
            continue
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) < params.min_edge:
            continue
        cand = {
            "ts": ts,
            "left": left,
            "side": side,
            "px": pr["px"],
            "lead": lead,
            "fair": fair,
        }
        if first is None:
            first = cand
        last = cand
    return {"first": first, "last": last}


def bm_exit(ev, series, band: list[dict], picked: dict, params: TwapParams) -> dict:
    start, end = int(ev["start"]), int(ev["end"])
    tw_open = series.twap(start, params.lookback)
    shares = NOTIONAL / max(picked["px"], 0.01)
    exit_px = None
    exit_why = "settle"
    for ts in range(picked["ts"] + RESCORE, end - 3, RESCORE):
        left = end - ts
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open) or 0.0
        signed = lead if picked["side"] == "Up" else -lead
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
        mark = te.last_print(band, ts, picked["side"], slack=30)
        bid = None if mark is None else mark["px"]
        go, why = should_scratch(
            fair_p=fair,
            lead_bps_signed=signed,
            bid=bid,
            shares=shares,
            fee_rate=0.07,
            left=float(left),
            params=params,
        )
        if not go:
            continue
        nxt = te.next_print(band, ts, picked["side"], slack=8) or mark
        if nxt is None:
            continue
        exit_px = nxt["px"]
        exit_why = why
        break
    won = picked["side"] == ev["winner"]
    if exit_px is not None:
        pnl = te.pnl_scratch(picked["px"], exit_px)
        scratched = True
    else:
        pnl = te.pnl_hold(picked["px"], won)
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
        "fair": round(picked["fair"], 4),
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "exit_px": None if exit_px is None else round(exit_px, 4),
        "pnl": round(pnl, 5),
        "pick": None,
    }


def attach_path(row: dict, full: list[dict]) -> dict:
    feat = path_features(full, row["side"], int(row["ts"]), int(row["end"]))
    out = dict(row)
    out.update(feat)
    return out


def overlay(row: dict, *, mode: str, haircut: float = 0.0) -> dict:
    """Apply a confirm dump on residual BM holds. Scratches stay as-is."""
    out = dict(row)
    out["overlay"] = mode
    out["haircut"] = haircut
    if row.get("scratched"):
        return out
    dump = False
    why = row.get("exit_why") or "settle"
    raw_exit = None
    if mode == "none":
        return out
    if mode == "dump_never_62" and not row.get("ever_62"):
        dump = True
        why = "never_confirmed_62"
        raw_exit = row.get("last_live") or row.get("px90") or row.get("last_after")
    elif mode == "dump_mid90":
        if row.get("still_mid90"):
            dump = True
            why = "late_still_mid"
            raw_exit = row.get("px90") or row.get("last_live")
        elif not row.get("ever_62"):
            dump = True
            why = "never_confirmed_62"
            raw_exit = row.get("last_live") or row.get("px90") or row.get("last_after")
    elif mode == "dump_unconfirmed_by90" and not row.get("ever_62_by90"):
        dump = True
        why = "unconfirmed_by90"
        raw_exit = row.get("px90") or row.get("last_live") or row.get("last_after")
    elif mode == "skip_left150":
        return out
    if not dump:
        return out
    if raw_exit is None:
        raw_exit = row["px"]
    exit_px = max(0.01, float(raw_exit) - haircut)
    out["scratched"] = True
    out["exit_why"] = why
    out["exit_px"] = round(exit_px, 4)
    out["pnl"] = round(te.pnl_scratch(row["px"], exit_px), 5)
    return out


def summarize(rows: list[dict]) -> dict:
    rec = te.summarize(rows)
    if not rows:
        rec.update({"confirmed_frac": None, "late_frac": None, "still_mid90_frac": None, "overlay_n": 0})
        return rec
    holds = [r for r in rows if not r.get("scratched")]
    rec["confirmed_frac"] = round(sum(1 for r in rows if r.get("ever_62")) / len(rows), 4)
    rec["late_frac"] = round(sum(1 for r in rows if float(r.get("left") or 0) <= LATE_LEFT) / len(rows), 4)
    rec["still_mid90_frac"] = round(sum(1 for r in rows if r.get("still_mid90")) / len(rows), 4)
    rec["overlay_n"] = sum(
        1 for r in rows if str(r.get("exit_why") or "").startswith(("never_confirmed", "late_still", "unconfirmed_by90"))
    )
    rec["hold_confirmed"] = te.summarize([r for r in holds if r.get("ever_62")])
    rec["hold_never_62"] = te.summarize([r for r in holds if not r.get("ever_62")])
    rec["hold_late"] = te.summarize([r for r in holds if float(r.get("left") or 0) <= LATE_LEFT])
    rec["hold_early"] = te.summarize([r for r in holds if float(r.get("left") or 0) > LATE_LEFT])
    rec["hold_still_mid90"] = te.summarize([r for r in holds if r.get("still_mid90")])
    rec["avg_px"] = rec.get("avg_px")
    return rec


def pack(rows: list[dict]) -> dict:
    train, hold = te.split_holdout(rows, days=HOLDOUT_DAYS)
    rec = {"all": summarize(rows), "train": summarize(train), "holdout": summarize(hold), "n": len(rows)}
    rec["robust"] = bool(
        rec["train"].get("ev_ok")
        and rec["holdout"].get("ev_ok")
        and rec["train"].get("n", 0) >= 25
        and rec["holdout"].get("n", 0) >= 25
        and (rec["holdout"].get("take_win_rate") or 0) >= 0.70
    )
    rec["hit70"] = bool((rec["holdout"].get("take_win_rate") or 0) >= 0.70 and rec["holdout"].get("ev_ok") and rec["train"].get("ev_ok"))
    rec["hit80"] = bool((rec["holdout"].get("take_win_rate") or 0) >= 0.80 and rec["holdout"].get("ev_ok") and rec["train"].get("ev_ok"))
    return rec


def by_asset(rows: list[dict]) -> dict:
    g = defaultdict(list)
    for r in rows:
        g[str(r.get("asset") or "?")].append(r)
    return {a: te.summarize(xs) for a, xs in sorted(g.items())}


def _wr(xs: list[dict]) -> dict:
    nn = len(xs)
    ww = sum(1 for r in xs if r.get("won"))
    return {
        "n": nn,
        "wins": ww,
        "wr": None if nn == 0 else round(ww / nn, 4),
        "pnl": round(sum(float(r.get("net") or r.get("pnl") or 0) for r in xs), 2),
    }


def _px_bucket(p) -> str:
    if p is None:
        return "na"
    p = float(p)
    if p < 0.47:
        return "45-46"
    if p < 0.50:
        return "47-49"
    if p < 0.53:
        return "50-52"
    return "53-55"


def _left_bucket(left) -> str:
    if left is None:
        return "na"
    left = float(left)
    if left < 150:
        return "120-150"
    if left < 180:
        return "150-180"
    if left < 240:
        return "180-240"
    return "240+"


def _fair_bucket(fair) -> str:
    if fair is None:
        return "na"
    f = float(fair)
    if f < 0.55:
        return "<55"
    if f < 0.65:
        return "55-65"
    if f < 0.75:
        return "65-75"
    if f < 0.85:
        return "75-85"
    return "ge85"


def live_autopsy(raw: dict | None) -> dict:
    if not raw:
        return {"error": "no live holds dump"}
    holds = list(raw.get("holds") or [])
    n = int(raw.get("n_hold") or len(holds))
    w = int(raw.get("hold_w") or sum(1 for r in holds if r.get("won")))
    def buckets(fn, keys):
        return [{**_wr([r for r in holds if fn(r) == k]), "k": k} for k in keys]
    out = {
        "holds": {
            "n": n,
            "wins": w,
            "wr": None if n == 0 else round(w / n, 4),
            "pnl": raw.get("hold_net"),
        },
        "scratches": {"n": raw.get("n_scratch"), "pnl": raw.get("scratch_net")},
        "hold_by_px": raw.get("hold_by_px")
        or buckets(lambda r: _px_bucket(r.get("px")), ("45-46", "47-49", "50-52", "53-55")),
        "hold_by_left": raw.get("hold_by_left")
        or buckets(lambda r: _left_bucket(r.get("left")), ("120-150", "150-180", "180-240", "240+")),
        "hold_by_asset": raw.get("hold_by_asset")
        or buckets(lambda r: r.get("asset"), sorted({a for a in (r.get("asset") for r in holds) if a})),
        "hold_by_fair": raw.get("hold_by_fair")
        or buckets(lambda r: _fair_bucket(r.get("fair")), ("<55", "55-65", "65-75", "75-85", "ge85", "na")),
        "note": (
            "Scratch +EV. Holds bleed in leftover 120–150s and on XRP/SOL. "
            "53–55¢ holds are not safer than 45–46¢."
        ),
    }
    late = [r for r in holds if float(r.get("left") or 0) < 150]
    rest = [r for r in holds if float(r.get("left") or 0) >= 150]
    out["counterfactual_drop_left_lt_150"] = {
        "dropped": _wr(late),
        "kept_holds": _wr(rest),
        "note": "If live never took leftover 120–150s. Research only; min_left 180 for BTC/ETH is separately -EV on holdout.",
    }
    keep = [r for r in holds if r.get("asset") not in ("xrp", "sol")]
    out["counterfactual_drop_xrp_sol_holds"] = {
        "kept": _wr(keep),
        "note": "Do not ship. User forbade cutting Telegram coins.",
    }
    fade = [r for r in holds if r.get("fair") is not None and float(r["fair"]) < 0.55]
    out["fade_leftover_holds"] = _wr(fade)
    return out


def live_scratch_pnl(entry: float, exit_px: float, notional: float = 3.0) -> float:
    shares = notional / max(float(entry), 0.01)
    return round(
        shares * (float(exit_px) - float(entry))
        - taker_fee(shares, float(entry), 0.07)
        - taker_fee(shares, float(exit_px), 0.07),
        5,
    )


def fetch_live_prints(slug: str, cid: str, end: int) -> list[dict]:
    LIVE_PRINTS.mkdir(parents=True, exist_ok=True)
    dest = LIVE_PRINTS / f"{slug}.json"
    if dest.exists() and dest.stat().st_size > 8:
        try:
            rows = json.loads(dest.read_text())
            if isinstance(rows, list) and rows:
                return rows
        except (json.JSONDecodeError, OSError):
            pass
    raw: list[dict] = []
    for page in range(6):
        chunk = http_json(f"{DATA}/trades?market={cid}&limit=1000&offset={page * 1000}&takerOnly=true") or []
        if not isinstance(chunk, list):
            break
        raw.extend(chunk)
        if len(chunk) < 1000:
            break
        time.sleep(0.03)
    rows = buys(raw, end - 300, end, lo=0.01, hi=0.99)
    dest.write_text(json.dumps(rows))
    return rows


def cid_for_slug(slug: str) -> tuple[str | None, int | None]:
    try:
        t0 = int(str(slug).rsplit("-", 1)[-1])
    except ValueError:
        t0 = None
    evs = http_json(f"{GAMMA}/events?slug={slug}") or []
    if isinstance(evs, dict):
        evs = [evs]
    if not evs:
        return None, t0
    market = (evs[0].get("markets") or [{}])[0]
    cid = str(market.get("conditionId") or "") or None
    return cid, t0


def live_path_join(holds: list[dict]) -> dict:
    """Join live residual holds to public CLOB BUY prints after fill."""
    if not holds:
        return {"n": 0}
    rows = []
    errors = 0

    def one(h):
        slug = h.get("slug") or ""
        cid, t0 = cid_for_slug(slug)
        end = (t0 + 300) if t0 else None
        if not cid or not end:
            return None
        # fill ts from iso if present
        ts = None
        iso = h.get("iso")
        if iso:
            try:
                ts = int(datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp())
            except ValueError:
                ts = None
        if ts is None and h.get("left") is not None and end:
            ts = int(end - float(h["left"]))
        token = str(h.get("token") or "").lower()
        side = "Up" if token in {"up", "yes"} else "Down"
        prints = fetch_live_prints(slug, cid, end)
        feat = path_features(prints, side, int(ts or end - 150), end)
        feat.update(
            {
                "slug": slug,
                "asset": h.get("asset"),
                "px": h.get("px"),
                "left": h.get("left"),
                "won": h.get("won"),
                "net": h.get("net"),
                "side": side,
                "n_prints": len(prints),
            }
        )
        return feat

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = [pool.submit(one, h) for h in holds]
        for fut in as_completed(futs):
            try:
                row = fut.result()
            except Exception:
                errors += 1
                continue
            if row:
                rows.append(row)
    wins = [r for r in rows if r.get("won")]
    loses = [r for r in rows if r.get("won") is False]

    def frac(xs, key):
        if not xs:
            return None
        return round(sum(1 for r in xs if r.get(key)) / len(xs), 4)

    def cf(mode: str) -> dict:
        """Replace settlement net with a 2¢-haircut scratch when the overlay fires."""
        pnl = 0.0
        dumped_w = dumped_l = kept_w = kept_l = 0
        for r in rows:
            entry = float(r.get("px") or 0.5)
            actual = float(r.get("net") or 0)
            dump = False
            raw_exit = None
            if mode == "dump_never_62" and not r.get("ever_62"):
                dump = True
                raw_exit = r.get("last_live") if r.get("last_live") is not None else r.get("px90")
            elif mode == "dump_mid90" and r.get("still_mid90"):
                dump = True
                raw_exit = r.get("px90") if r.get("px90") is not None else r.get("last_after")
            elif mode == "dump_unconfirmed_by90" and not r.get("ever_62_by90"):
                dump = True
                raw_exit = r.get("px90") if r.get("px90") is not None else r.get("last_live")
            elif mode == "dump_in_band90" and r.get("still_in_band90"):
                dump = True
                raw_exit = r.get("px90") if r.get("px90") is not None else r.get("last_live")
            if dump:
                exit_px = max(0.01, float(raw_exit if raw_exit is not None else entry) - HAIRCUT)
                pnl += live_scratch_pnl(entry, exit_px)
                if r.get("won"):
                    dumped_w += 1
                else:
                    dumped_l += 1
            else:
                pnl += actual
                if r.get("won"):
                    kept_w += 1
                else:
                    kept_l += 1
        kept = kept_w + kept_l
        return {
            "pnl": round(pnl, 2),
            "dumped_winners": dumped_w,
            "dumped_losers": dumped_l,
            "kept_holds": kept,
            "kept_wr": None if kept == 0 else round(kept_w / kept, 4),
        }

    actual = round(sum(float(r.get("net") or 0) for r in rows), 2)
    return {
        "n": len(rows),
        "errors": errors,
        "actual_net": actual,
        "all_ever_62": frac(rows, "ever_62"),
        "win_ever_62": frac(wins, "ever_62"),
        "lose_ever_62": frac(loses, "ever_62"),
        "all_still_mid90": frac(rows, "still_mid90"),
        "win_still_mid90": frac(wins, "still_mid90"),
        "lose_still_mid90": frac(loses, "still_mid90"),
        "all_still_in_band90": frac(rows, "still_in_band90"),
        "win_still_in_band90": frac(wins, "still_in_band90"),
        "lose_still_in_band90": frac(loses, "still_in_band90"),
        "lose_n": len(loses),
        "win_n": len(wins),
        "all_ever_62_by90": frac(rows, "ever_62_by90"),
        "win_ever_62_by90": frac(wins, "ever_62_by90"),
        "lose_ever_62_by90": frac(loses, "ever_62_by90"),
        "cf_dump_never_62_h2": cf("dump_never_62"),
        "cf_dump_by90_h2": cf("dump_unconfirmed_by90"),
        "cf_dump_mid90_h2": cf("dump_mid90"),
        "cf_dump_in_band90_h2": cf("dump_in_band90"),
        "note": (
            "Residual live holds only (BM scratch already missed them). "
            "dump_never_62 keeps every winner that printed ≥62¢. dump_mid90 "
            "uses <60¢ at 90s left and can clip a winner sitting at 59¢."
        ),
        "rows_sample": sorted(rows, key=lambda r: str(r.get("slug")))[:8],
    }


def scan_btc_eth(events: list[dict], series_of: dict, params: TwapParams) -> dict:
    first_rows = []
    last_rows = []
    paired = {"same": 0, "cheaper": 0, "richer": 0, "only_one": 0}
    cheaper_last = []
    richer_last = []
    n_win = 0
    for i, ev in enumerate(events, 1):
        asset = ev.get("asset")
        if asset not in series_of:
            continue
        if int(ev["end"]) < TWAP60:
            continue
        raw = load_raw(REV_CACHE, ev["slug"])
        if not raw:
            continue
        full = buys(raw, ev["start"], ev["end"], lo=0.05, hi=0.99)
        band = [p for p in full if 0.40 - 1e-12 <= p["px"] <= 0.60 + 1e-12]
        if len(band) < 4:
            continue
        n_win += 1
        got = pick_candidates(ev, series_of[asset], band, params)
        first, last = got["first"], got["last"]
        if first is None and last is None:
            continue
        if first is not None:
            row = attach_path(bm_exit(ev, series_of[asset], band, first, params), full)
            row["pick"] = "first"
            first_rows.append(row)
        if last is not None:
            row = attach_path(bm_exit(ev, series_of[asset], band, last, params), full)
            row["pick"] = "last"
            last_rows.append(row)
        if first is not None and last is not None:
            if last["ts"] == first["ts"]:
                paired["same"] += 1
            elif last["px"] + 1e-12 < first["px"] - 0.01:
                paired["cheaper"] += 1
                cheaper_last.append(last_rows[-1])
            elif last["px"] > first["px"] + 0.01:
                paired["richer"] += 1
                richer_last.append(last_rows[-1])
            else:
                paired["same"] += 1
        else:
            paired["only_one"] += 1
        if i % 800 == 0:
            print(f"  btceth {i} windows_with_prints={n_win} first={len(first_rows)} last={len(last_rows)}", flush=True)
    return {
        "first": first_rows,
        "last": last_rows,
        "paired": paired,
        "cheaper_last": cheaper_last,
        "richer_last": richer_last,
        "windows_scanned": n_win,
    }


def last_no_cheaper(first_rows: list[dict], last_rows: list[dict], *, by: str = "slug") -> list[dict]:
    """Take last only when it is not cheaper than first; else keep first.

    `by=slug` for independent windows. `by=start` for clock-lock (one coin per
    5m unix): only rewrite when first/last are the same slug.
    """
    key = (lambda r: r["slug"]) if by == "slug" else (lambda r: int(r["start"]))
    by_first = {key(r): r for r in first_rows}
    out = []
    for last in last_rows:
        first = by_first.get(key(last))
        if first is None:
            out.append(last)
            continue
        if by == "start" and first.get("slug") != last.get("slug"):
            out.append(last)
            continue
        if last["px"] + 1e-12 < first["px"] - 0.005:
            out.append(first)
        else:
            out.append(last)
    return out


def skip_late(rows: list[dict], min_left: float) -> list[dict]:
    return [r for r in rows if float(r.get("left") or 0) >= min_left]


def variant_grid(base: dict) -> list[dict]:
    first, last = base["first"], base["last"]
    grid = []

    def add(name, rows, **meta):
        rec = pack(rows)
        rec["name"] = name
        rec["by_asset"] = by_asset(rows)
        rec.update(meta)
        grid.append(rec)
        h = rec["holdout"]
        print(
            f"{name:28s} n={rec['all']['n']:4d} all ${rec['all']['pnl_usd']:+7.1f} "
            f"hit={rec['all'].get('take_win_rate')} hold n={h['n']:3d} "
            f"hit={h.get('take_win_rate')} pnl={h['pnl_usd']:+.1f} robust={rec['robust']}",
            flush=True,
        )

    add("first_bm", first, pick="first", overlay="none")
    add("last_bm", last, pick="last", overlay="none")
    add("last_no_cheaper", last_no_cheaper(first, last), pick="last_else_first", overlay="none")
    add("last_skip_left150", skip_late(last, 150), pick="last", overlay="skip_left150")
    add("first_skip_left150", skip_late(first, 150), pick="first", overlay="skip_left150")
    add("last_skip_left180", skip_late(last, 180), pick="last", overlay="skip_left180",
        note="Do not ship as BTC/ETH default. Holdout historically worse than 120.")
    for pick, rows in (("first", first), ("last", last)):
        for mode in ("dump_never_62", "dump_mid90"):
            for hair in (0.0, HAIRCUT):
                tagged = [overlay(r, mode=mode, haircut=hair) for r in rows]
                add(f"{pick}_{mode}_h{int(hair * 100)}", tagged, pick=pick, overlay=mode, haircut=hair)
        tagged = [overlay(r, mode="dump_unconfirmed_by90", haircut=HAIRCUT) for r in rows]
        add(f"{pick}_dump_by90_h2", tagged, pick=pick, overlay="dump_unconfirmed_by90", haircut=HAIRCUT)
    combo2 = [overlay(r, mode="dump_never_62", haircut=HAIRCUT) for r in last_no_cheaper(first, last)]
    add("no_cheaper_dump62_h2", combo2, pick="last_else_first", overlay="dump_never_62", haircut=HAIRCUT)
    return grid


def month_overlay() -> dict:
    takes_path = MONTH_CACHE / "_takes.json"
    if not takes_path.exists():
        return {"error": "no _takes.json"}
    cached = json.loads(takes_path.read_text())
    last_rows = list(cached.get("last") or [])
    first_rows = list(cached.get("first") or [])
    print(f"month takes last={len(last_rows)} first={len(first_rows)}", flush=True)

    def hydrate(rows: list[dict]) -> list[dict]:
        out = []
        for r in rows:
            if int(r.get("end") or 0) < TWAP60:
                continue
            ts = int(r["end"]) - int(r["left"])
            r = dict(r)
            r["ts"] = ts
            raw = load_raw(MONTH_CACHE, r["slug"])
            if not raw:
                raw = load_raw(REV_CACHE, r["slug"])
            full = buys(raw, int(r["start"]), int(r["end"]), lo=0.05, hi=0.99) if raw else []
            if full:
                r = attach_path(r, full)
            else:
                r.setdefault("ever_62", False)
                r.setdefault("still_mid90", False)
                r.setdefault("max_after", 0.0)
                r.setdefault("last_after", r.get("px"))
                r.setdefault("px90", None)
            out.append(r)
        return out

    last_h = hydrate(last_rows)
    first_h = hydrate(first_rows)
    clock_last = fcm.clock_lock(last_h, rank="lead")
    clock_first = fcm.clock_lock(first_h, rank="lead")
    variants = []

    def add(name, rows, **meta):
        rec = pack(rows)
        rec["name"] = name
        rec["by_asset"] = by_asset(rows)
        rec.update(meta)
        variants.append(rec)
        print(
            f"month {name:28s} n={rec['all']['n']:4d} all ${rec['all']['pnl_usd']:+7.1f} "
            f"hit={rec['all'].get('take_win_rate')} holdout ${rec['holdout']['pnl_usd']:+.1f} "
            f"hit={rec['holdout'].get('take_win_rate')} robust={rec['robust']}",
            flush=True,
        )

    add("indep_last_bm", last_h, clock=False, pick="last")
    add("indep_first_bm", first_h, clock=False, pick="first")
    add("clock_last_bm", clock_last, clock=True, pick="last")
    add("clock_first_bm", clock_first, clock=True, pick="first")
    add("clock_last_no_cheaper", last_no_cheaper(clock_first, clock_last, by="start"), clock=True, pick="last_else_first")
    add(
        "clock_last_dump_mid90_h2",
        [overlay(r, mode="dump_mid90", haircut=HAIRCUT) for r in clock_last],
        clock=True,
        pick="last",
        overlay="dump_mid90",
        haircut=HAIRCUT,
    )
    add(
        "clock_first_dump_mid90_h2",
        [overlay(r, mode="dump_mid90", haircut=HAIRCUT) for r in clock_first],
        clock=True,
        pick="first",
        overlay="dump_mid90",
        haircut=HAIRCUT,
    )
    add(
        "clock_last_dump62_h2",
        [overlay(r, mode="dump_never_62", haircut=HAIRCUT) for r in clock_last],
        clock=True,
        pick="last",
        overlay="dump_never_62",
        haircut=HAIRCUT,
        note="Month prints are slimmed ≤62¢ so ever_62 is a cap hit, not a true 70¢ print.",
    )
    return {
        "n_last": len(last_h),
        "n_first": len(first_h),
        "print_cap": "month cache slimmed 0.20–0.62; confirm@62 is a cap, mid90 is the reliable overlay",
        "variants": variants,
    }


def pick_candidate(grid: list[dict], baseline_name: str) -> dict:
    base = next((g for g in grid if g["name"] == baseline_name), None)
    base_ho = (base or {}).get("holdout", {}).get("pnl_usd") or 0.0
    ranked = sorted(grid, key=lambda g: (g["holdout"].get("pnl_usd") or -1e9, g["holdout"].get("take_win_rate") or 0), reverse=True)
    ok = []
    for g in grid:
        if g["name"] in {"last_skip_left180"}:
            continue
        if not g.get("robust"):
            continue
        ho = g["holdout"].get("pnl_usd") or 0
        if ho < 0.80 * base_ho and base_ho > 0:
            continue
        ok.append(g)
    ok.sort(key=lambda g: (g["holdout"].get("take_win_rate") or 0, g["holdout"].get("pnl_usd") or 0), reverse=True)
    best_wr = ok[0] if ok else None
    # dump_mid90 maxes tape WR but clips live winners sitting ~59¢ at 90s left.
    prefer = [
        g
        for g in ranked
        if g.get("robust")
        and "dump_mid90" not in g["name"]
        and "skip_left180" not in g["name"]
    ]
    prefer_causal = [g for g in prefer if "dump_by90" in g["name"]]
    later = (prefer_causal[0] if prefer_causal else None) or (prefer[0] if prefer else None)
    return {
        "ranking_holdout_pnl": [
            {
                "name": g["name"],
                "holdout_pnl": g["holdout"].get("pnl_usd"),
                "holdout_hit": g["holdout"].get("take_win_rate"),
                "train_pnl": g["train"].get("pnl_usd"),
                "all_pnl": g["all"].get("pnl_usd"),
                "all_hit": g["all"].get("take_win_rate"),
                "robust": g.get("robust"),
            }
            for g in ranked
        ],
        "best_robust_wr": None
        if best_wr is None
        else {
            "name": best_wr["name"],
            "holdout_pnl": best_wr["holdout"].get("pnl_usd"),
            "holdout_hit": best_wr["holdout"].get("take_win_rate"),
            "train_pnl": best_wr["train"].get("pnl_usd"),
            "all_hit": best_wr["all"].get("take_win_rate"),
        },
        "baseline_last_bm_holdout": base_ho,
        "if_later": None
        if later is None
        else {
            "name": later["name"],
            "holdout_pnl": later["holdout"].get("pnl_usd"),
            "holdout_hit": later["holdout"].get("take_win_rate"),
            "train_pnl": later["train"].get("pnl_usd"),
            "all_hit": later["all"].get("take_win_rate"),
            "why": "Causal confirm: dump at 90s left if 62¢ has not printed yet. Best holdout among dump_by90.",
        },
    }


def run() -> dict:
    t0 = time.time()
    live_src = LIVE_HOLDS_FULL if LIVE_HOLDS_FULL.exists() else LIVE_HOLDS
    live_raw = json.loads(live_src.read_text()) if live_src.exists() else None
    live = live_autopsy(live_raw)
    print("live autopsy", live.get("holds"), flush=True)

    events = json.loads((REV_CACHE / "_events.json").read_text()) if (REV_CACHE / "_events.json").exists() else []
    twap_ev = [e for e in events if int(e.get("end") or 0) >= TWAP60]
    newest = max((e["end"] for e in twap_ev), default=TWAP60)
    print(f"load series btc/eth {TWAP60-180}->{newest+5} n_ev={len(twap_ev)}", flush=True)
    series_of = {
        "btc": rp.load_series("btc", TWAP60 - 180, newest + 5),
        "eth": rp.load_series("eth", TWAP60 - 180, newest + 5),
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
    print("scan btc+eth windows", flush=True)
    scanned = scan_btc_eth(twap_ev, series_of, params)
    print("grid btc+eth", flush=True)
    grid = variant_grid(scanned)
    picked = pick_candidate(grid, "last_bm")

    print("month overlay", flush=True)
    month = month_overlay()

    print("live CLOB path join", flush=True)
    live_path = {"n": 0, "skipped": "no holds list"}
    try:
        live_path = live_path_join(list((live_raw or {}).get("holds") or []))
    except Exception as exc:
        live_path = {"n": 0, "error": f"{type(exc).__name__}: {exc}"}
    print("live path", {k: live_path.get(k) for k in ("n", "all_ever_62", "lose_ever_62", "win_ever_62", "lose_still_mid90")}, flush=True)

    first_s = summarize(scanned["first"])
    last_s = summarize(scanned["last"])
    cheap_s = te.summarize(scanned["cheaper_last"])
    rich_s = te.summarize(scanned["richer_last"])

    findings = {
        "headline": (
            "高勝率 bot = first-cross（唔追平 leftover）+ 從未印 62¢ 就 dump。"
            "唔係 8¢ 止蝕，唔係 20–30¢ 炒底，唔係再 dump 多啲盲 scratch。"
        ),
        "live_gap": (
            f"Live holds {live.get('holds')}. Research last_bm take WR "
            f"{last_s.get('take_win_rate')} / first_bm {first_s.get('take_win_rate')}. "
            "Binance-proxy last-take still ~90% because the print *is* a filled 45–55 BUY. "
            "Live FOK hunts a leftover ask after that print is gone. "
            "XRP/SOL live ~0–8% vs proxy clock-lock ~90% — proxy does not model last-look."
        ),
        "cheaper_chase": {
            "paired": scanned["paired"],
            "last_when_cheaper": cheap_s,
            "last_when_richer": rich_s,
            "note": (
                "On the print tape, last-cheaper leftover still ~82% WR. "
                "Live 17% is last-look / FOK, not the same sleeve."
            ),
        },
        "book_confirm": {
            "research_last_hold_never_62": last_s.get("hold_never_62"),
            "research_last_hold_confirmed": last_s.get("hold_confirmed"),
            "live_path": {
                "n": live_path.get("n"),
                "win_ever_62": live_path.get("win_ever_62"),
                "win_ever_62_by90": live_path.get("win_ever_62_by90"),
                "lose_ever_62": live_path.get("lose_ever_62"),
                "lose_ever_62_by90": live_path.get("lose_ever_62_by90"),
                "win_still_mid90": live_path.get("win_still_mid90"),
                "cf_dump_never_62_h2": live_path.get("cf_dump_never_62_h2"),
                "cf_dump_by90_h2": live_path.get("cf_dump_by90_h2"),
                "cf_dump_mid90_h2": live_path.get("cf_dump_mid90_h2"),
            },
            "note": (
                "Causal confirm = at 90s left, dump if 62¢ has not printed yet. "
                "On BTC+ETH this matches lookahead never-62 (same WR, holdout within $2). "
                "Live residual holds: every winner had printed 62 by 90s left; "
                "dump_by90 clips 0 winners and ~3/4 of the $0 losers. "
                "dump_mid90 (last print still <60¢) clips live winners at 59¢. "
                "Month 7-coin dump overlays are invalid: prints slimmed ≤62¢."
            ),
        },
        "do_not": sorted(DO_NOT_SHIP),
        "ship": False,
        "why_not_now": "User 暫時不動. Live FOK last-look still differs from the print tape.",
        "top_strategy": (
            "No live patch. If later: first-cross + dump_unconfirmed_by90 "
            "(at 90s left, same-side book has not printed 62¢). Keep BM scratch. "
            "2¢ haircut tape stays +EV on BTC+ETH train/holdout."
        ),
    }

    rec = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_s": round(time.time() - t0, 2),
        "proxy": "Binance 1s TWAP vs Binance T0 + CLOB BUY prints. Live = Chainlink vs Chainlink T0 + FOK.",
        "notional_research": NOTIONAL,
        "notional_live": 3.0,
        "holdout_days": HOLDOUT_DAYS,
        "ship": False,
        "do_not_default_on": True,
        "user": "暫時不動 — research only, no Zeabur patch",
        "confirm_px": CONFIRM_PX,
        "confirm_left": CONFIRM_LEFT,
        "haircut_conservative": HAIRCUT,
        "live": live,
        "live_clob_path": live_path,
        "btc_eth": {
            "windows_scanned": scanned["windows_scanned"],
            "paired": scanned["paired"],
            "first_bm": first_s,
            "last_bm": last_s,
            "cheaper_last": cheap_s,
            "richer_last": rich_s,
            "variants": grid,
            "pick": picked,
        },
        "seven_coin_month": month,
        "findings": findings,
        "recommendation": {
            "do_now": "nothing on live",
            "if_later": (
                "1) First-cross: after the first valid 6bps 45–55 setup, do not fill a "
                "cheaper leftover (retry the same limit or skip the window on FOK miss). "
                "2) Causal book-confirm: at 90s left, if the same-side book has not "
                "yet printed ≥62¢, dump at the then bid (keep BM weak/flip/better). "
                "Do not dump just because the last print is 59¢ if 62 already printed. "
                "Re-score on a live FOK tape before Zeabur. Never 8¢ SL, never 20–30¢ bounce, "
                "never min_left 180 on BTC/ETH, never cut coins."
            ),
            "ship_candidate": (picked.get("if_later") or {}).get("name"),
            "max_wr_but_clips_live_winners": (picked.get("best_robust_wr") or {}).get("name"),
        },
    }
    OUT.write_text(json.dumps(rec, indent=2, default=str) + "\n")
    print("wrote", OUT, "elapsed", rec["elapsed_s"], flush=True)
    return rec


if __name__ == "__main__":
    run()
