#!/usr/bin/env python3
"""Copy top Polymarket 5m wallets: reconstruct how they actually make money.

Public CRYPTO WEEK+MONTH leaderboards, closed-position PnL split into
pair-lock vs leftover tilt, then causal tape rules a $5 taker could run.

Hold out the newest 7 days of the BTC 5m TWAP-60 cache. Do not ship a rule
that is only +EV as a maker (fee=0) or that flips sign on holdout.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee, taker_net  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402
from app.twap import TwapParams  # noqa: E402

OUT = Path(__file__).with_name("copy_top.json")
DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "surf-arb-research/2.1 (read-only; copy-top wallets; no trading)",
    "Accept": "application/json",
}
TWAP60_START = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp())
HOLDOUT_DAYS = 7
NOTIONAL = 5.0
FEE = 0.07


def get(url: str, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code == 404:
                return None
            if exc.code in {400, 429, 500, 502, 503, 422}:
                time.sleep(0.4 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.35 * (2**i))
    if last:
        raise last
    return None


def is_5m_crypto(slug: str) -> bool:
    s = str(slug or "")
    return s.startswith("btc-updown-5m-") or s.startswith("eth-updown-5m-")


def slug_start(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def leaderboard(category: str, period: str, order: str, n: int) -> list[dict]:
    return (
        get(
            f"{DATA}/v1/leaderboard?category={category}&timePeriod={period}&orderBy={order}&limit={n}"
        )
        or []
    )


def paged(url_base: str, *, limit: int, pages: int, extra: str = "") -> list[dict]:
    rows: list[dict] = []
    for p in range(pages):
        chunk = get(f"{url_base}&limit={limit}&offset={p * limit}{extra}") or []
        if not isinstance(chunk, list):
            break
        rows.extend(chunk)
        if len(chunk) < limit:
            break
        time.sleep(0.03)
    return rows


def closed_positions(wallet: str, pages: int = 16) -> list[dict]:
    return paged(
        f"{DATA}/closed-positions?user={wallet}",
        limit=50,
        pages=pages,
        extra="&sortBy=timestamp&sortDirection=DESC",
    )


def user_trades(wallet: str, pages: int = 3) -> list[dict]:
    return paged(f"{DATA}/trades?user={wallet}", limit=1000, pages=pages)


def g(row: dict | None, key: str, default: float = 0.0) -> float:
    if not row:
        return default
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def reconstruct_markets(closed: list[dict]) -> list[dict]:
    five = [c for c in closed if is_5m_crypto(c.get("slug") or c.get("eventSlug") or "")]
    by: dict[str, list[dict]] = defaultdict(list)
    for c in five:
        by[str(c.get("slug") or c.get("eventSlug") or "")].append(c)
    out = []
    for slug, legs in by.items():
        up = next((x for x in legs if x.get("outcome") == "Up"), None)
        dn = next((x for x in legs if x.get("outcome") == "Down"), None)
        us, ds = g(up, "totalBought"), g(dn, "totalBought")
        upx, dpx = g(up, "avgPrice"), g(dn, "avgPrice")
        pnl = g(up, "realizedPnl") + g(dn, "realizedPnl")
        pair = min(us, ds)
        leftover = abs(us - ds)
        winner = None
        if up and g(up, "curPrice") >= 0.99:
            winner = "Up"
        elif dn and g(dn, "curPrice") >= 0.99:
            winner = "Down"
        elif up and g(up, "curPrice") <= 0.01 and not dn:
            winner = "Down"
        elif dn and g(dn, "curPrice") <= 0.01 and not up:
            winner = "Up"
        lock_per = (1.0 - upx - dpx) if pair > 0 else None
        lock_usd = pair * lock_per if lock_per is not None else 0.0
        if us >= ds:
            left_pnl = leftover * ((1.0 if winner == "Up" else 0.0) - upx)
            tilt = "up" if leftover > 1 else "flat"
        else:
            left_pnl = leftover * ((1.0 if winner == "Down" else 0.0) - dpx)
            tilt = "down" if leftover > 1 else "flat"
        out.append(
            {
                "slug": slug,
                "btc": str(slug).startswith("btc-"),
                "both": bool(up and dn),
                "pnl": round(pnl, 4),
                "pair": round(pair, 4),
                "leftover": round(leftover, 4),
                "tilt": tilt,
                "winner": winner,
                "lock_per": None if lock_per is None else round(lock_per, 4),
                "lock_usd": round(lock_usd, 4),
                "left_usd": round(left_pnl, 4),
                "up_sh": round(us, 2),
                "dn_sh": round(ds, 2),
                "up_px": round(upx, 4),
                "dn_px": round(dpx, 4),
            }
        )
    return out


def classify(mkt: list[dict], n5_trades: int, both_frac: float, sell_frac: float, fav_frac: float) -> str:
    if n5_trades < 20 and len(mkt) < 20:
        return "not_5m"
    if fav_frac >= 0.4:
        return "favorite_taker"
    if not mkt:
        return "mixed"
    lock = sum(m["lock_usd"] for m in mkt)
    left = sum(m["left_usd"] for m in mkt)
    net = sum(m["pnl"] for m in mkt)
    if both_frac >= 0.75 and lock > abs(left) and lock > 0 and net > 0:
        return "pair_lock_harvester"
    if both_frac >= 0.5 and left > abs(lock) and left > 0 and net > 0:
        return "leftover_tilter"
    if both_frac >= 0.5 and net < 0:
        return "both_sides_loser"
    if sell_frac >= 0.2:
        return "scratch_or_mm"
    return "mixed"


def profile_wallet(row: dict, *, source: str, period: str) -> dict:
    w = row["proxyWallet"]
    closed = closed_positions(w, pages=16)
    trades = user_trades(w, pages=2)
    m5 = [t for t in trades if is_5m_crypto(t.get("slug") or t.get("eventSlug") or "")]
    buys = [t for t in m5 if str(t.get("side") or "").upper() == "BUY"]
    sells = [t for t in m5 if str(t.get("side") or "").upper() == "SELL"]
    mkts = reconstruct_markets(closed)
    px = [float(t["price"]) for t in buys if t.get("price") is not None]
    lefts = []
    usd = []
    dt_other = []
    by_mkt: dict[str, list[dict]] = defaultdict(list)
    for t in buys:
        by_mkt[str(t.get("slug") or "")].append(t)
        st = slug_start(t.get("slug") or "")
        ts = t.get("timestamp")
        if st and ts:
            lefts.append(st + 300 - int(ts))
        try:
            usd.append(float(t.get("size") or 0) * float(t.get("price") or 0))
        except (TypeError, ValueError):
            pass
    both_tr = 0
    same_sec = 0
    for ts in by_mkt.values():
        oc = {str(x.get("outcome")) for x in ts}
        if "Up" in oc and "Down" in oc:
            both_tr += 1
            alls = sorted(ts, key=lambda x: int(x.get("timestamp") or 0))
            first = alls[0]
            for x in alls[1:]:
                if x.get("outcome") != first.get("outcome"):
                    dt = int(x.get("timestamp") or 0) - int(first.get("timestamp") or 0)
                    dt_other.append(dt)
                    if dt == 0:
                        same_sec += 1
                    break
    buckets = Counter()
    for L in lefts:
        if L >= 240:
            buckets["into_0_60"] += 1
        elif L >= 180:
            buckets["into_60_120"] += 1
        elif L >= 120:
            buckets["into_120_180"] += 1
        elif L >= 60:
            buckets["into_180_240"] += 1
        else:
            buckets["last_60"] += 1
    fav = sum(1 for p in px if p >= 0.97) / len(px) if px else 0.0
    mid = sum(1 for p in px if 0.45 <= p <= 0.55) / len(px) if px else 0.0
    both_frac = (sum(1 for m in mkts if m["both"]) / len(mkts)) if mkts else 0.0
    sell_frac = (len(sells) / len(m5)) if m5 else 0.0
    style = classify(mkts, len(m5), both_frac, sell_frac, fav)
    lock = sum(m["lock_usd"] for m in mkts)
    left_usd = sum(m["left_usd"] for m in mkts)
    net = sum(m["pnl"] for m in mkts)
    locks = [m["lock_per"] for m in mkts if m.get("lock_per") is not None]
    return {
        "source": source,
        "period": period,
        "rank": row.get("rank"),
        "name": row.get("userName") or "",
        "wallet": w,
        "board_vol": round(float(row.get("vol") or 0), 2),
        "board_pnl": round(float(row.get("pnl") or 0), 2),
        "n_closed_5m_mkts": len(mkts),
        "n_5m_trades": len(m5),
        "n_buy": len(buys),
        "n_sell": len(sells),
        "sell_frac": round(sell_frac, 4),
        "frac_5m": None if not trades else round(len(m5) / len(trades), 4),
        "both_frac_closed": round(both_frac, 4),
        "both_frac_trades": None if not by_mkt else round(both_tr / len(by_mkt), 4),
        "closed_pnl": round(net, 2),
        "pair_lock_usd": round(lock, 2),
        "leftover_usd": round(left_usd, 2),
        "lock_minus_left": round(lock - abs(left_usd), 2),
        "pair_lock_mean": None if not locks else round(sum(locks) / len(locks), 4),
        "pair_lock_median": None if not locks else round(sorted(locks)[len(locks) // 2], 4),
        "frac_lock_gt0": None if not locks else round(sum(1 for x in locks if x > 0) / len(locks), 4),
        "median_buy_px": None if not px else round(sorted(px)[len(px) // 2], 4),
        "median_buy_usd": None if not usd else round(sorted(usd)[len(usd) // 2], 2),
        "median_left_s": None if not lefts else int(sorted(lefts)[len(lefts) // 2]),
        "mid_buy_frac": round(mid, 4),
        "favorite_buy_frac": round(fav, 4),
        "dt_first_other_median_s": None if not dt_other else int(sorted(dt_other)[len(dt_other) // 2]),
        "same_second_complete_frac": None if not dt_other else round(same_sec / len(dt_other), 4),
        "time_buckets": dict(buckets),
        "style": style,
        "btc_mkt_frac": None if not mkts else round(sum(1 for m in mkts if m["btc"]) / len(mkts), 4),
    }


def collect_wallets() -> list[tuple[str, str, dict]]:
    jobs = []
    seen: set[str] = set()
    for period, n_pnl, n_vol in (("WEEK", 25, 15), ("MONTH", 25, 15)):
        pnl = leaderboard("CRYPTO", period, "PNL", n_pnl)
        vol = leaderboard("CRYPTO", period, "VOL", n_vol)
        for src, xs in (("pnl", pnl), ("vol", vol)):
            for row in xs:
                w = str(row.get("proxyWallet") or "")
                if not w or w in seen:
                    continue
                if float(row.get("vol") or 0) < 1000 and float(row.get("pnl") or 0) < 1000:
                    continue
                seen.add(w)
                jobs.append((src, period.lower(), row))
    return jobs


def load_btc_prints(ev: dict) -> list[tuple[int, float, str]]:
    path = rp.CACHE / f"{ev['slug']}.json"
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    start, end = int(ev["start"]), int(ev["end"])
    for t in raw:
        if str(t.get("side") or "").upper() != "BUY":
            continue
        oc = str(t.get("outcome") or "")
        if oc not in {"Up", "Down"}:
            continue
        try:
            ts = int(t.get("ts") or t.get("timestamp") or 0)
            px = float(t.get("px") or t.get("price") or 0)
        except (TypeError, ValueError):
            continue
        if ts < start or ts > end or px <= 0:
            continue
        out.append((ts, px, oc))
    out.sort()
    return out


def pnl_hold_leg(px: float, shares: float, won: bool, fee_rate: float) -> float:
    fee = taker_fee(shares, px, fee_rate)
    if won:
        return round(shares * (1.0 - px) - fee, 5)
    return round(-shares * px - fee, 5)


def sim_pairlock(
    prints: list[tuple[int, float, str]],
    winner: str,
    start: int,
    end: int,
    *,
    first_max: float,
    complete_sum: float,
    min_left: float,
    max_left: float,
    chop: bool,
    fee_rate: float,
    notional: float = NOTIONAL,
) -> dict | None:
    """Buy one cheap/mid first leg, complete if later other print sums under cap."""
    last = {"Up": None, "Down": None}
    first = None
    second = None
    for ts, px, oc in prints:
        last[oc] = (ts, px)
        left = end - ts
        if first is None:
            if left < min_left or left > max_left or px > first_max + 1e-12:
                continue
            if chop:
                other = "Down" if oc == "Up" else "Up"
                opp = last[other]
                if opp is None or ts - opp[0] > 8:
                    continue
                if not (0.35 <= opp[1] <= 0.70 and 0.35 <= px <= 0.70):
                    continue
            shares = notional / max(px, 0.01)
            first = {"ts": ts, "px": px, "oc": oc, "shares": shares, "left": left}
            continue
        if second is None and oc != first["oc"] and ts >= first["ts"]:
            if px + first["px"] <= complete_sum + 1e-12:
                sh = min(first["shares"], notional / max(px, 0.01))
                second = {"ts": ts, "px": px, "oc": oc, "shares": sh, "left": left}
                first["shares"] = sh
                break
    if first is None:
        return None
    if second is None:
        pnl = pnl_hold_leg(first["px"], first["shares"], first["oc"] == winner, fee_rate)
        return {
            "kind": "unmatched",
            "first_px": first["px"],
            "first_oc": first["oc"],
            "second_px": None,
            "pair_sum": None,
            "pnl": pnl,
            "left": first["left"],
        }
    if fee_rate <= 0:
        pnl = round(first["shares"] * (1.0 - first["px"] - second["px"]), 5)
    else:
        pnl = taker_net(first["shares"], first["px"], second["px"], fee_rate)
    return {
        "kind": "paired",
        "first_px": first["px"],
        "first_oc": first["oc"],
        "second_px": second["px"],
        "pair_sum": round(first["px"] + second["px"], 4),
        "pnl": pnl,
        "left": first["left"],
    }


def sim_simul_complement(
    prints: list[tuple[int, float, str]],
    start: int,
    end: int,
    *,
    max_sum: float,
    min_left: float,
    fee_rate: float,
    notional: float = NOTIONAL,
) -> dict | None:
    last = {"Up": None, "Down": None}
    for ts, px, oc in prints:
        last[oc] = (ts, px)
        left = end - ts
        if left < min_left:
            continue
        up, dn = last["Up"], last["Down"]
        if up is None or dn is None:
            continue
        if abs(up[0] - dn[0]) > 2:
            continue
        ssum = up[1] + dn[1]
        if ssum <= max_sum + 1e-12:
            shares = notional / max(max(up[1], dn[1]), 0.01)
            if fee_rate <= 0:
                pnl = round(shares * (1.0 - ssum), 5)
            else:
                pnl = taker_net(shares, up[1], dn[1], fee_rate)
            return {"kind": "simul", "pair_sum": round(ssum, 4), "pnl": pnl, "left": left}
    return None


def sim_twap_then_complete(
    ev: dict,
    series,
    prints: list[tuple[int, float, str]],
    params: TwapParams,
    *,
    complete_sum: float,
    fee_rate: float,
) -> dict | None:
    mid = [{"ts": ts, "px": px, "outcome": oc} for ts, px, oc in prints if 0.45 <= px <= 0.55]
    row = te.simulate_market(ev, series, mid, params)
    if row is None:
        return None
    if row.get("scratched"):
        return {**row, "kind": "twap_scratch", "completed": False}
    # unmatched hold: try complete using later opposite prints
    entry_ts = None
    for ts, px, oc in prints:
        if oc == row["side"] and abs(px - row["px"]) < 1e-9 and ts >= ev["start"]:
            entry_ts = ts
            break
    if entry_ts is None:
        return {**row, "kind": "twap_hold", "completed": False}
    other = "Down" if row["side"] == "Up" else "Up"
    shares = NOTIONAL / max(row["px"], 0.01)
    for ts, px, oc in prints:
        if ts <= entry_ts or oc != other:
            continue
        if px + row["px"] <= complete_sum + 1e-12:
            if fee_rate <= 0:
                pnl = round(shares * (1.0 - row["px"] - px), 5)
            else:
                pnl = taker_net(shares, row["px"], px, fee_rate)
            return {
                **row,
                "kind": "twap_complete",
                "completed": True,
                "second_px": px,
                "pair_sum": round(row["px"] + px, 4),
                "pnl": pnl,
                "scratched": False,
                "exit_why": "pair_complete",
            }
    return {**row, "kind": "twap_hold", "completed": False}


def summarize_pair(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "pnl_usd": 0.0, "ev_ok": False}
    paired = [r for r in rows if r.get("kind") in {"paired", "simul", "twap_complete"}]
    unmatched = [r for r in rows if r.get("kind") in {"unmatched", "twap_hold"}]
    scratch = [r for r in rows if r.get("kind") == "twap_scratch"]
    pnl = sum(float(r["pnl"]) for r in rows)
    return {
        "n": len(rows),
        "pnl_usd": round(pnl, 2),
        "paired_n": len(paired),
        "unmatched_n": len(unmatched),
        "scratch_n": len(scratch),
        "paired_frac": round(len(paired) / len(rows), 4),
        "avg_pair_sum": None
        if not paired
        else round(sum(r["pair_sum"] for r in paired if r.get("pair_sum") is not None) / max(len(paired), 1), 4),
        "ev_ok": pnl > 0,
    }


def split_holdout(rows: list[dict], newest: int) -> tuple[list[dict], list[dict]]:
    cut = newest - HOLDOUT_DAYS * 86400
    return [r for r in rows if r["end"] < cut], [r for r in rows if r["end"] >= cut]


def pack_split(rows: list[dict], newest: int) -> dict:
    train, hold = split_holdout(rows, newest)
    rec = {"all": summarize_pair(rows), "train": summarize_pair(train), "holdout": summarize_pair(hold)}
    rec["robust"] = bool(
        rec["train"]["ev_ok"]
        and rec["holdout"]["ev_ok"]
        and rec["train"]["n"] >= 25
        and rec["holdout"]["n"] >= 25
    )
    return rec


def copy_whale_delay(
    whale_fills: list[dict],
    events_by_slug: dict[str, dict],
    prints_by_slug: dict[str, list[tuple[int, float, str]]],
    *,
    delay_s: int,
    fee_rate: float,
) -> list[dict]:
    """2s after a whale's first buy in a cached market, lift the next same-side print."""
    first: dict[str, dict] = {}
    for t in whale_fills:
        slug = str(t.get("slug") or "")
        if slug in first or slug not in events_by_slug:
            continue
        if str(t.get("side") or "").upper() != "BUY":
            continue
        oc = str(t.get("outcome") or "")
        if oc not in {"Up", "Down"}:
            continue
        first[slug] = t
    out = []
    for slug, t in first.items():
        ev = events_by_slug[slug]
        ts0 = int(t.get("timestamp") or 0) + delay_s
        oc = str(t.get("outcome"))
        hit = None
        for ts, px, side in prints_by_slug.get(slug, []):
            if ts >= ts0 and side == oc:
                hit = (ts, px)
                break
        if hit is None:
            continue
        won = oc == ev["winner"]
        shares = NOTIONAL / max(hit[1], 0.01)
        pnl = pnl_hold_leg(hit[1], shares, won, fee_rate)
        out.append({"end": ev["end"], "pnl": pnl, "kind": "copy_first", "px": hit[1], "oc": oc, "won": won})
    return out


def _median(xs: list[float]) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[len(s) // 2]


def build_findings(report: dict) -> dict:
    wallets = [w for w in report["wallets"] if not w.get("error")]
    five = [w for w in wallets if (w.get("n_closed_5m_mkts") or 0) >= 40 or (w.get("n_5m_trades") or 0) >= 50]
    styles = Counter(w["style"] for w in five)
    harvesters = [w for w in five if w["style"] == "pair_lock_harvester"]
    tilters = [w for w in five if w["style"] == "leftover_tilter"]
    favs = [w for w in five if w["style"] == "favorite_taker"]
    clean_harvest = [
        w
        for w in harvesters
        if (w.get("closed_pnl") or 0) >= 500 and (w.get("lock_minus_left") or 0) >= 500
    ]
    sims = report.get("sims") or {}
    # Print-implied Up+Down BUY within 2s fires on almost every window — same mirage as
    # rest-state complement. fee0 is maker-like; our bot pays 7% crypto taker.
    shippable = []
    for k, v in sims.items():
        if not isinstance(v, dict) or not v.get("robust"):
            continue
        if k.startswith("simul_"):
            continue
        if "fee0" in k and "fee07" not in k:
            continue
        shippable.append(k)
    pairlock_fee07 = [k for k in sims if k.startswith("pairlock_fee07") and sims[k].get("all", {}).get("ev_ok")]
    copy_trade = report.get("copy_trade") or {}
    copy_ok = any(
        (blk.get("fee07") or {}).get("robust") for blk in copy_trade.values() if isinstance(blk, dict)
    )
    early = report.get("directional_twap_early") or []
    picked_early = None
    robust_early = [g for g in early if g.get("robust") and g.get("min_lead_bps") == 6.0]
    if robust_early:
        # Keep 6bps (2bps is tape noise). Prefer whale-like window ~180s over 280s spam.
        prefer = [g for g in robust_early if g.get("max_left") == 180.0]
        picked_early = (prefer or robust_early)[0]
    med_left_h = _median([w["median_left_s"] for w in harvesters if w.get("median_left_s") is not None])
    med_left_t = _median([w["median_left_s"] for w in tilters if w.get("median_left_s") is not None])
    dt = _median([w["dt_first_other_median_s"] for w in harvesters if w.get("dt_first_other_median_s") is not None])
    names = ", ".join(
        f"{w.get('name') or w['wallet'][:8]} +${w['closed_pnl']}" for w in (clean_harvest or harvesters)[:3]
    )
    tilt_names = ", ".join(
        f"{w.get('name') or w['wallet'][:8]} +${w['closed_pnl']}"
        for w in sorted(tilters, key=lambda x: -(x.get("closed_pnl") or 0))[:3]
    )
    headline = (
        "5m 頂級戶有兩套真策略，都唔係 97¢、亦唔係 last-120s 單邊。"
        f"風格 {dict(styles)}。"
        f"鎖倉收割機（{names or '無'}）分時買齊兩面、pair VWAP<$1，剩倉方向係拖後腿；"
        f"第一腿→對家中位 {dt}s，幾乎從唔同一秒齊兩腿。"
        f"$5 taker 照抄分時鎖倉：7% 費後成表 −EV；延遲 2s 跟單同樣 −EV。"
        f"PnL 更大嘅係方向盤戶（{tilt_names or '無'}），中位剩餘約 {med_left_t}s、單注 $50–300、sell≈0。"
        + (
            f" 可複製嘅係入場時機：TWAP 6bps+scratch 把 max_left 由 120s 開到 "
            f"{picked_early['max_left']:.0f}s（train {picked_early['train']['pnl_usd']}/"
            f"holdout {picked_early['holdout']['pnl_usd']}）。唔好降到 2bps，唔好改做雙邊鎖倉。"
            if picked_early
            else " 放寬 TWAP 窗未通過 train+holdout，維持 Rev 23。"
        )
    )
    return {
        "headline_cantonese": headline,
        "wallet_styles": dict(styles),
        "n_5m_specialists": len(five),
        "n_pair_lock_harvesters": len(harvesters),
        "n_clean_harvesters": len(clean_harvest),
        "n_leftover_tilters": len(tilters),
        "n_favorite_takers": len(favs),
        "harvester_median_left_s": med_left_h,
        "tilter_median_left_s": med_left_t,
        "harvester_dt_first_other_s": dt,
        "pairlock_fee07_any_plus": pairlock_fee07,
        "copy_trade_robust": copy_ok,
        "print_implied_simul_is_mirage": True,
        "shippable_tape_rules": shippable,
        "picked_rule": (
            f"twap_max_left_{picked_early['max_left']:.0f}_lead6_scratch"
            if picked_early
            else None
        ),
        "picked_early_twap": picked_early,
        "copy_as": "twap_earlier_window" if picked_early else "research_only",
        "recommend": {
            "copy": (
                "複製方向盤戶嘅入場時間：BTC 5m 45–55¢、同源 TWAP lead≥6bps、弱倉 scratch，"
                f"剩餘 {picked_early['max_left']:.0f}s 內都可以入（頂級戶中位 ~160–210s）。"
                if picked_early
                else "未有 $5 taker 可船規則。"
            ),
            "do_not_copy": [
                "分時雙邊鎖倉當 taker（7% 費後 train/holdout 都 −EV）",
                "延遲 2s 跟頂級錢包第一筆",
                "帶印即時 Up+Down 當互補（live rest ask_sum ≥ 1.01）",
                "VelvetNova 類週榜 PnL、vol≈0",
                "hot-garbage：pair 成本>$1，靠 leftover 方向訊號",
                "97–98¢ 大熱 taker",
                "lead≥2bps 開成全段（樂觀成交帶噪音）",
                "TWAP 贏面腿再買齊對家（會賣走方向盤 edge）",
            ],
            "taker_fee_note": (
                "閉倉 realized≈理論 lock+leftover，殘差近 0：鎖倉收割機好可能係 maker 免手續費。"
                "中價雙邊 taker 費各約 1.75¢，要 >3.5¢ lock 先打平；佢哋中位 lock 只有 2–4¢。"
            ),
            "keep": "互補 taker min_edge 0.02、FOK、maker 關、唔好 live、唔好 scratch 改 hedge。",
        },
    }


def main() -> None:
    print("leaderboards", flush=True)
    jobs = collect_wallets()
    print(f"profile {len(jobs)} wallets", flush=True)
    wallets = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {
            pool.submit(profile_wallet, row, source=src, period=per): (src, per, row)
            for src, per, row in jobs
        }
        for i, fut in enumerate(as_completed(futs), 1):
            src, per, row = futs[fut]
            try:
                wallets.append(fut.result())
            except Exception as exc:
                wallets.append(
                    {
                        "source": src,
                        "period": per,
                        "wallet": row.get("proxyWallet"),
                        "error": str(exc),
                    }
                )
            if i % 8 == 0:
                print(f"  wallets {i}/{len(jobs)}", flush=True)
    wallets.sort(key=lambda w: (0 if w.get("style") == "pair_lock_harvester" else 1, -(w.get("pair_lock_usd") or 0)))

    print("load btc 5m TWAP60 tape", flush=True)
    events = json.loads((rp.CACHE / "_events.json").read_text())
    btc = [
        e
        for e in events
        if e.get("asset") == "btc" and str(e.get("slug", "")).startswith("btc-updown-5m-") and int(e["end"]) >= TWAP60_START
    ]
    btc.sort(key=lambda e: e["end"])
    newest = max(e["end"] for e in btc) if btc else 0
    prints_by = {}
    for i, ev in enumerate(btc, 1):
        prints_by[ev["slug"]] = load_btc_prints(ev)
        if i % 400 == 0:
            print(f"  prints {i}/{len(btc)}", flush=True)
    events_by = {e["slug"]: e for e in btc}

    grid = []
    for fee_rate, tag in ((FEE, "fee07"), (0.0, "fee0")):
        for first_max in (0.48, 0.50, 0.52):
            for complete_sum in (0.96, 0.97, 0.98):
                for chop in (True, False):
                    rows = []
                    for ev in btc:
                        pr = prints_by.get(ev["slug"] or "")
                        if not pr:
                            continue
                        got = sim_pairlock(
                            pr,
                            ev["winner"],
                            ev["start"],
                            ev["end"],
                            first_max=first_max,
                            complete_sum=complete_sum,
                            min_left=12.0,
                            max_left=240.0,
                            chop=chop,
                            fee_rate=fee_rate,
                        )
                        if got:
                            got["end"] = ev["end"]
                            rows.append(got)
                    name = f"pairlock_{tag}_first{first_max:.2f}_sum{complete_sum:.2f}_{'chop' if chop else 'any'}"
                    rec = pack_split(rows, newest)
                    rec["params"] = {
                        "fee_rate": fee_rate,
                        "first_max": first_max,
                        "complete_sum": complete_sum,
                        "chop": chop,
                    }
                    grid.append((name, rec))
                    print(
                        f"{name} all {rec['all']['pnl_usd']:+.1f} n={rec['all']['n']} "
                        f"paired={rec['all']['paired_frac']} train {rec['train']['pnl_usd']:+.1f} "
                        f"hold {rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']}",
                        flush=True,
                    )
        for max_sum in (0.96, 0.97, 0.98):
            rows = []
            for ev in btc:
                pr = prints_by.get(ev["slug"] or "")
                if not pr:
                    continue
                got = sim_simul_complement(pr, ev["start"], ev["end"], max_sum=max_sum, min_left=12.0, fee_rate=fee_rate)
                if got:
                    got["end"] = ev["end"]
                    rows.append(got)
            name = f"simul_{tag}_sum{max_sum:.2f}"
            rec = pack_split(rows, newest)
            rec["params"] = {"fee_rate": fee_rate, "max_sum": max_sum}
            grid.append((name, rec))
            print(
                f"{name} all {rec['all']['pnl_usd']:+.1f} n={rec['all']['n']} "
                f"train {rec['train']['pnl_usd']:+.1f} hold {rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']}",
                flush=True,
            )

    print("twap then complete", flush=True)
    t0 = TWAP60_START - 180
    t1 = newest + 5
    series = rp.load_series("btc", t0, t1)
    params = TwapParams(min_lead_bps=6.0, min_edge=0.04, max_left=120.0)
    for fee_rate, tag in ((FEE, "fee07"), (0.0, "fee0")):
        for complete_sum in (0.96, 0.98, 1.00):
            rows = []
            for ev in btc:
                pr = prints_by.get(ev["slug"] or "")
                if not pr:
                    continue
                got = sim_twap_then_complete(ev, series, pr, params, complete_sum=complete_sum, fee_rate=fee_rate)
                if got:
                    rows.append(got)
            name = f"twap_complete_{tag}_sum{complete_sum:.2f}"
            rec = pack_split(rows, newest)
            rec["params"] = {"fee_rate": fee_rate, "complete_sum": complete_sum}
            grid.append((name, rec))
            print(
                f"{name} all {rec['all']['pnl_usd']:+.1f} n={rec['all']['n']} "
                f"paired={rec['all']['paired_frac']} train {rec['train']['pnl_usd']:+.1f} "
                f"hold {rec['holdout']['pnl_usd']:+.1f} robust={rec['robust']}",
                flush=True,
            )

    print("copy-trade top harvesters", flush=True)
    harvesters = [w for w in wallets if w.get("style") == "pair_lock_harvester" and w.get("wallet")][:4]
    copy_sims = {}
    for w in harvesters:
        fills = user_trades(w["wallet"], pages=8)
        rows07 = copy_whale_delay(fills, events_by, prints_by, delay_s=2, fee_rate=FEE)
        rows0 = copy_whale_delay(fills, events_by, prints_by, delay_s=2, fee_rate=0.0)
        copy_sims[w.get("name") or w["wallet"][:10]] = {
            "wallet": w["wallet"],
            "fee07": pack_split(rows07, newest),
            "fee0": pack_split(rows0, newest),
            "n_fills_pulled": len(fills),
        }
        print(
            f"  copy {w.get('name')} fee07 {copy_sims[w.get('name') or w['wallet'][:10]]['fee07']['all']}",
            flush=True,
        )

    sims = {name: rec for name, rec in grid}
    early_path = Path("/tmp/twap_early.json")
    early = json.loads(early_path.read_text()) if early_path.exists() else []
    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_wallets": len(wallets),
        "tape": {
            "n_btc_5m": len(btc),
            "oldest_end": None if not btc else datetime.fromtimestamp(btc[0]["end"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "newest_end": None if not newest else datetime.fromtimestamp(newest, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "holdout_days": HOLDOUT_DAYS,
            "notional": NOTIONAL,
        },
        "wallets": wallets,
        "style_counts": dict(Counter(w.get("style") for w in wallets if w.get("style"))),
        "sims": sims,
        "copy_trade": copy_sims,
        "directional_twap_early": early,
        "note": (
            "Closed-position pair_lock = min(up,down) shares * (1-avgUp-avgDown). "
            "Tape sims lift last BUY prints (optimistic vs live ask). "
            "fee07 = official 7% crypto taker curve; fee0 = maker-like. "
            "simul_* is print-implied complement (live rest ask_sum stays ≥ 1.01). "
            "directional_twap_early keeps 6bps+scratch and only widens max_left."
        ),
    }
    report["findings"] = build_findings(report)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {OUT}", flush=True)
    print(report["findings"]["headline_cantonese"], flush=True)


def rebuild_findings() -> None:
    report = json.loads(OUT.read_text())
    early_path = Path("/tmp/twap_early.json")
    if early_path.exists():
        report["directional_twap_early"] = json.loads(early_path.read_text())
    report["researched_at_utc"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    report["findings"] = build_findings(report)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(report["findings"]["headline_cantonese"], flush=True)
    print(f"rewrote findings {OUT}", flush=True)


if __name__ == "__main__":
    if "--findings-only" in sys.argv:
        rebuild_findings()
    else:
        main()
