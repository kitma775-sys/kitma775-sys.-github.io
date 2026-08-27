#!/usr/bin/env python3
"""BTC 5-minute Up/Down 90–99¢ reversal + window/band study (read-only public APIs)."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "surf-arb-research/1.3 (read-only; no trading)"}
OUT = Path(__file__).with_name("btc_5m_reversal.json")
TICKS = tuple(round(i / 100.0, 2) for i in range(90, 100))
WINDOWS = (30, 45, 60, 90, 120, 180, 240, 300)
FEE = 0.07
DAYS = 30
BANDS = [(lo, hi) for lo in TICKS for hi in TICKS if hi >= lo]


def get_json(url: str, timeout: float = 25.0, tries: int = 4):
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
            if exc.code in {429, 500, 502, 503}:
                time.sleep(0.4 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.3 * (2**i))
    if last:
        raise last
    return None


def parse_field(raw, default):
    if isinstance(raw, (list, dict)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def fee_on(price: float) -> float:
    p = min(max(float(price), 0.0), 1.0)
    return FEE * p * (1.0 - p)


def pnl_per_share(price: float, won: bool) -> float:
    fee = fee_on(price)
    if won:
        return 1.0 - price - fee
    return -price - fee


def breakeven_wr(price: float) -> float:
    win = 1.0 - price - fee_on(price)
    lose = -price - fee_on(price)
    return -lose / (win - lose)


def wilson(wins: int, n: int) -> tuple[float, float] | None:
    if n <= 0:
        return None
    z = 1.96
    phat = wins / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    spread = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def end_ts(iso: str) -> int:
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def load_market(start: int) -> dict | None:
    slug = f"btc-updown-5m-{start}"
    m = get_json(f"{GAMMA}/markets/slug/{slug}")
    if not m or not m.get("conditionId"):
        return None
    if not m.get("closed"):
        return {"slug": slug, "skip": "open"}
    prices = [float(x) for x in parse_field(m.get("outcomePrices"), ["0", "0"])]
    outcomes = [str(x) for x in parse_field(m.get("outcomes"), ["Up", "Down"])]
    if len(prices) < 2 or abs(max(prices) - 1.0) > 0.05:
        return {"slug": slug, "skip": "unresolved"}
    winner = outcomes[0] if prices[0] >= prices[1] else outcomes[1]
    end = end_ts(m["endDate"])
    cid = m["conditionId"]
    trades = get_json(f"{DATA}/trades?market={cid}&limit=1000") or []
    rows = []
    for t in trades:
        if str(t.get("conditionId") or "") != str(cid):
            continue
        try:
            px = float(t["price"])
            ts = int(t["timestamp"])
            size = float(t.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            continue
        outcome = str(t.get("outcome") or "")
        if outcome not in {"Up", "Down"}:
            continue
        left = end - ts
        if left < -20 or left > 920:
            continue
        rows.append(
            {
                "ts": ts,
                "left": left,
                "px": px,
                "size": size,
                "side": str(t.get("side") or ""),
                "outcome": outcome,
            }
        )
    rows.sort(key=lambda r: r["ts"])
    return {
        "slug": slug,
        "end": end,
        "winner": winner,
        "volume": float(m.get("volume") or 0),
        "trades": rows,
    }


def bucket(px: float) -> float | None:
    tick = round(px, 2)
    if tick in TICKS:
        return tick
    return None


def first_hits_in(trades: list[dict], left_max: float, *, buy_only: bool) -> dict[tuple[float, str], dict]:
    seen: dict[tuple[float, str], dict] = {}
    for t in trades:
        if t["left"] < 0 or t["left"] > left_max:
            continue
        if buy_only and t["side"] != "BUY":
            continue
        tick = bucket(t["px"])
        if tick is None:
            continue
        key = (tick, t["outcome"])
        if key not in seen:
            seen[key] = t
    return seen


def first_in_band(trades: list[dict], left_max: float, lo: float, hi: float) -> dict | None:
    """First BUY whose rounded price sits in [lo, hi] after the window opens."""
    for t in trades:
        if t["left"] < 0 or t["left"] > left_max:
            continue
        if t["side"] != "BUY":
            continue
        tick = round(t["px"], 2)
        if lo <= tick <= hi:
            return t
    return None


def ever_max(trades: list[dict], left_max: float) -> dict[str, float]:
    out = {"Up": 0.0, "Down": 0.0}
    for t in trades:
        if 0 <= t["left"] <= left_max:
            out[t["outcome"]] = max(out[t["outcome"]], t["px"])
    return out


def snapshot_at(trades: list[dict], left: float) -> dict[str, float]:
    last = {}
    for t in trades:
        if t["left"] >= left:
            last[t["outcome"]] = t["px"]
    return last


def add_stat(store: dict, key: str, won: bool, price: float, size: float) -> None:
    row = store.setdefault(
        key,
        {"n": 0, "win": 0, "lose": 0, "pnl": 0.0, "pnl_sizew": 0.0, "size": 0.0, "prices": []},
    )
    row["n"] += 1
    row["win"] += int(won)
    row["lose"] += int(not won)
    pnl = pnl_per_share(price, won)
    row["pnl"] += pnl
    cap = min(size, 25.0 / max(price, 0.01))
    row["pnl_sizew"] += pnl * cap
    row["size"] += cap
    row["prices"].append(round(price, 4))


def finish(store: dict) -> dict:
    out = {}
    for key, row in store.items():
        n = row["n"]
        wr = row["win"] / n if n else None
        ci = wilson(row["win"], n)
        avg_px = round(sum(row["prices"]) / n, 4) if n else None
        out[key] = {
            "n": n,
            "win": row["win"],
            "lose": row["lose"],
            "wr": None if wr is None else round(wr, 6),
            "wr_ci95": None if ci is None else [round(ci[0], 6), round(ci[1], 6)],
            "reverse": None if wr is None else round(1.0 - wr, 6),
            "pnl_per_share": round(row["pnl"] / n, 6) if n else None,
            "pnl_sizew_per_share": round(row["pnl_sizew"] / row["size"], 6) if row["size"] else None,
            "total_pnl_unweighted": round(row["pnl"], 4) if n else None,
            "avg_px": avg_px,
        }
    return out


def compact_buy(block: dict) -> dict:
    out = {}
    for tick in TICKS:
        row = {}
        for win in WINDOWS:
            cell = block.get(f"buy_{tick:.2f}_last{win}s")
            if not cell:
                continue
            row[f"last{win}s"] = {
                "n": cell["n"],
                "lose": cell.get("lose"),
                "reverse": cell["reverse"],
                "wr": cell["wr"],
                "pnl_per_share": cell["pnl_per_share"],
                "wr_ci95": cell["wr_ci95"],
                "avg_px": cell.get("avg_px"),
            }
        out[f"{tick:.2f}"] = row
    return out


def load_priors() -> tuple[dict | None, dict | None]:
    if not OUT.exists():
        return None, None
    old = json.loads(OUT.read_text())
    prior_14d = old.get("prior_14d")
    if old.get("days") == 14 and old.get("first_buy"):
        prior_14d = {
            "researched_at_utc": old.get("researched_at_utc"),
            "markets_resolved": old.get("markets_resolved"),
            "first_buy": compact_buy(old["first_buy"]),
            "recommendation": old.get("recommendation"),
        }
    prior_30d = old.get("prior_30d_95_99")
    ticks = set((old.get("buy_table") or {}).keys())
    if old.get("days") == 30 and old.get("buy_table") and "0.90" not in ticks:
        prior_30d = {
            "researched_at_utc": old.get("researched_at_utc"),
            "markets_resolved": old.get("markets_resolved"),
            "markets_with_tape": old.get("markets_with_tape"),
            "steamroller_count_5m": old.get("steamroller_count_5m"),
            "buy_table": old.get("buy_table"),
            "recommendation": old.get("recommendation"),
        }
    return prior_14d, prior_30d


def math_table() -> dict:
    rows = {}
    for p in TICKS:
        win = 1.0 - p - fee_on(p)
        lose = -p - fee_on(p)
        be = -lose / (win - lose)
        rows[f"{p:.2f}"] = {
            "fee": round(fee_on(p), 6),
            "win_pnl": round(win, 6),
            "lose_pnl": round(lose, 6),
            "breakeven_wr": round(be, 6),
            "breakeven_reverse": round(1.0 - be, 6),
            "wins_to_offset_one_25usd_loss": round((-lose * 25) / (win * 25), 1),
        }
    return rows


def annotate(cell: dict) -> dict:
    avg = cell.get("avg_px") or 0.95
    be = breakeven_wr(avg)
    ci = cell.get("wr_ci95") or [0.0, 1.0]
    pnl = cell.get("pnl_per_share") or 0.0
    return {
        **{k: cell[k] for k in cell if k != "total_pnl_unweighted"},
        "be_wr_at_avg_px": round(be, 6),
        "ci_clears": bool(pnl > 0 and ci[0] >= be),
        "plus_ev": bool(pnl > 0),
        "total_pnl_unweighted": cell.get("total_pnl_unweighted"),
    }


def parse_band_key(key: str) -> tuple[float, float, int] | None:
    # band_0.90_0.96_last180s
    if not key.startswith("band_"):
        return None
    try:
        rest = key[len("band_") :]
        lo_s, hi_s, tail = rest.split("_", 2)
        win = int(tail.replace("last", "").replace("s", ""))
        return float(lo_s), float(hi_s), win
    except (ValueError, IndexError):
        return None


def rank_bands(finished: dict, min_n: int = 500) -> dict:
    rows = []
    for key, cell in finished.items():
        parsed = parse_band_key(key)
        if not parsed:
            continue
        lo, hi, win = parsed
        row = annotate(cell)
        rows.append(
            {
                "min": lo,
                "max": hi,
                "window": win,
                "band": f"{lo:.2f}-{hi:.2f}",
                **row,
            }
        )

    def slim(r: dict) -> dict:
        return {
            "band": r["band"],
            "window": r["window"],
            "n": r["n"],
            "lose": r["lose"],
            "avg_px": r["avg_px"],
            "reverse": r["reverse"],
            "wr": r["wr"],
            "pnl_per_share": r["pnl_per_share"],
            "total_pnl_unweighted": r["total_pnl_unweighted"],
            "wr_ci95": r["wr_ci95"],
            "ci_clears": r["ci_clears"],
        }

    clearing = [r for r in rows if r["ci_clears"] and r["n"] >= min_n]
    plus = [r for r in rows if r["plus_ev"] and r["n"] >= min_n]

    def top(xs, keyfn, k=10, reverse=True):
        return [slim(r) for r in sorted(xs, key=keyfn, reverse=reverse)[:k]]

    best_window_by_tick = {}
    for tick in TICKS:
        cands = [r for r in rows if abs(r["min"] - tick) < 1e-9 and abs(r["max"] - tick) < 1e-9]
        if not cands:
            continue
        clear = [r for r in cands if r["ci_clears"]]
        pool = clear or cands
        best = max(pool, key=lambda r: (r["pnl_per_share"] or -9, r["n"]))
        best_window_by_tick[f"{tick:.2f}"] = slim(best)

    current = next(
        (slim(r) for r in rows if abs(r["min"] - 0.96) < 1e-9 and abs(r["max"] - 0.98) < 1e-9 and r["window"] == 180),
        None,
    )
    return {
        "min_n": min_n,
        "n_combos": len(rows),
        "n_ci_clears": len(clearing),
        "current_bot_96_98_last180s": current,
        "best_ev": top(clearing, lambda r: r["pnl_per_share"] or -9),
        "safest": top(clearing, lambda r: (r["reverse"] if r["reverse"] is not None else 9, -r["n"]), reverse=False),
        "best_total_edge": top(clearing, lambda r: (r["total_pnl_unweighted"] or -9)),
        "best_balance": top(
            clearing,
            lambda r: (r["pnl_per_share"] or 0) / max(r["reverse"] or 0.001, 0.001),
        ),
        "best_plus_ev_if_ci_thin": top(plus, lambda r: r["pnl_per_share"] or -9)[:5],
        "best_window_by_tick": best_window_by_tick,
        "avoid_windows": [30, 45],
    }


def make_recommendation(ranks: dict) -> dict:
    current = ranks.get("current_bot_96_98_last180s")
    best_ev = (ranks.get("best_ev") or [None])[0]
    safest = (ranks.get("safest") or [None])[0]
    best_bal = (ranks.get("best_balance") or [None])[0]
    by_tick = ranks.get("best_window_by_tick") or {}
    pick = best_bal or best_ev
    rec = {
        "sample": "BTC 5m, 30d, first BUY in band after window opens, 7% fee, hold to official resolve",
        "current_bot": current,
        "best_ev_combo": best_ev,
        "safest_combo": safest,
        "best_balance_combo": best_bal,
        "best_window_by_tick": by_tick,
        "avoid": "Last 30–45s. Taker 99¢. 15m books. Full-hour 90–99¢.",
    }
    if pick:
        rec["bot_settings_if_following_tape"] = {
            "favorite_min_price": float(pick["band"].split("-")[0]),
            "favorite_max_price": float(pick["band"].split("-")[1]),
            "favorite_window_seconds": pick["window"],
            "why": (
                f"{pick['band']} last {pick['window']}s: reverse {pick['reverse']}, "
                f"+{pick['pnl_per_share']} /share, n={pick['n']}, CI clears breakeven at avg fill."
            ),
        }
    return rec


def main() -> None:
    prior_14d, prior_30d = load_priors()
    now = int(time.time())
    last_closed = now - (now % 300) - 300
    days = DAYS
    starts = [last_closed - i * 300 for i in range(days * 24 * 12)]
    print(
        f"fetch {len(starts)} btc 5m windows ending "
        f"{datetime.fromtimestamp(last_closed, timezone.utc).isoformat()}",
        flush=True,
    )
    markets = []
    errors = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=18) as pool:
        futs = {pool.submit(load_market, ts): ts for ts in starts}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 200 == 0:
                print(f"  {done}/{len(starts)} ok={len(markets)} skip={skipped} err={errors}", flush=True)
            try:
                row = fut.result()
            except Exception:
                errors += 1
                continue
            if not row:
                skipped += 1
                continue
            if row.get("skip"):
                skipped += 1
                continue
            markets.append(row)
    print(f"loaded {len(markets)} resolved markets", flush=True)

    first_any: dict = {}
    first_buy: dict = {}
    band_buy: dict = {}
    ever: dict = {}
    snap: dict = {}
    steam = []

    for m in markets:
        winner = m["winner"]
        trades = m["trades"]
        if not trades:
            continue
        for win in WINDOWS:
            hits_any = first_hits_in(trades, win, buy_only=False)
            hits_buy = first_hits_in(trades, win, buy_only=True)
            for (tick, outcome), t in hits_any.items():
                add_stat(first_any, f"first_{tick:.2f}_last{win}s", outcome == winner, t["px"], t["size"])
            for (tick, outcome), t in hits_buy.items():
                add_stat(first_buy, f"buy_{tick:.2f}_last{win}s", outcome == winner, t["px"], t["size"])
            for lo, hi in BANDS:
                cands = [t for (tick, _o), t in hits_buy.items() if lo <= tick <= hi]
                if not cands:
                    continue
                t = min(cands, key=lambda x: x["ts"])
                add_stat(
                    band_buy,
                    f"band_{lo:.2f}_{hi:.2f}_last{win}s",
                    t["outcome"] == winner,
                    t["px"],
                    t["size"],
                )
        for win in WINDOWS:
            mx = ever_max(trades, win)
            fav = "Up" if mx["Up"] >= mx["Down"] else "Down"
            px = mx[fav]
            tick = bucket(px) or (0.99 if px >= 0.99 else None)
            if tick is None or px < 0.895:
                continue
            won = fav == winner
            add_stat(ever, f"max_{tick:.2f}_last{win}s", won, px if px <= 0.999 else 0.99, 25.0)
            if tick >= 0.99 and not won:
                steam.append(
                    {
                        "slug": m["slug"],
                        "window": win,
                        "fav": fav,
                        "winner": winner,
                        "max_px": round(px, 4),
                    }
                )
        for win in WINDOWS:
            last = snapshot_at(trades, win)
            if not last:
                continue
            fav = "Up" if last.get("Up", 0) >= last.get("Down", 0) else "Down"
            px = last.get(fav, 0)
            tick = bucket(px)
            if tick is None:
                continue
            add_stat(snap, f"snap_{tick:.2f}_at{win}s", fav == winner, px, 25.0)

    steam_full = [x for x in steam if x["window"] == 300]
    seen = set()
    steam_uniq = []
    for x in steam_full:
        if x["slug"] in seen:
            continue
        seen.add(x["slug"])
        steam_uniq.append(x)

    math_rows = math_table()
    finished_buy = finish(first_buy)
    finished_band = finish(band_buy)
    ranks = rank_bands(finished_band, min_n=500)
    rec = make_recommendation(ranks)
    out = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asset": "btc",
        "horizon": "5m",
        "days": days,
        "ticks": [f"{t:.2f}" for t in TICKS],
        "windows_s": list(WINDOWS),
        "markets_resolved": len(markets),
        "markets_with_tape": sum(1 for m in markets if m["trades"]),
        "errors": errors,
        "skipped": skipped,
        "fee_rate": FEE,
        "math": math_rows,
        "rule_first_buy": "First BUY print (lift ask) that rounds to a tick, held to official resolve.",
        "rule_band": (
            "First BUY whose rounded price is inside [min, max] after the tail window opens. "
            "This is what favorite_min/max + favorite_window_seconds does."
        ),
        "first_buy": finished_buy,
        "buy_table": compact_buy(finished_buy),
        "ranks": ranks,
        "steamrollers_hit_99_then_lost_in_5m": steam_uniq[:25],
        "steamroller_count_5m": len(steam_uniq),
        "snapshot_at_T": finish(snap),
        "ever_max": finish(ever),
        "prior_14d": prior_14d,
        "prior_30d_95_99": prior_30d,
        "recommendation": rec,
        "honest": [
            "A wide cheap band (90–99 last 5m) fills at the first 90¢ print, then has the whole window to get steamrolled.",
            "99¢ is not certain. One dump after you lift is −~99¢/share vs +~0.93¢ if you win.",
            "Prints that already traded are not a 250ms FAK fill. Live fill rate is lower.",
            "5m books can trade before the BTC window; this study keeps last 900s of tape only.",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT}", flush=True)

    print("\n== BUY ticks ==", flush=True)
    for tick in TICKS:
        parts = []
        for win in (60, 90, 120, 180, 300):
            row = finished_buy.get(f"buy_{tick:.2f}_last{win}s")
            if not row:
                continue
            parts.append(f"last{win}s n={row['n']} rev={row['reverse']} pnl={row['pnl_per_share']}")
        print(f"  {tick:.2f}  " + " | ".join(parts), flush=True)

    print("\n== TOP EV bands (CI clears, n>=500) ==", flush=True)
    for r in ranks["best_ev"][:8]:
        print(
            f"  {r['band']} last{r['window']}s n={r['n']} rev={r['reverse']} "
            f"pnl={r['pnl_per_share']} avg={r['avg_px']}",
            flush=True,
        )
    print("\n== SAFEST bands ==", flush=True)
    for r in ranks["safest"][:8]:
        print(
            f"  {r['band']} last{r['window']}s n={r['n']} rev={r['reverse']} pnl={r['pnl_per_share']}",
            flush=True,
        )
    print("\n== BEST BALANCE ==", flush=True)
    for r in ranks["best_balance"][:8]:
        print(
            f"  {r['band']} last{r['window']}s n={r['n']} rev={r['reverse']} pnl={r['pnl_per_share']}",
            flush=True,
        )
    print("\n== current 96-98 last180 ==", ranks.get("current_bot_96_98_last180s"), flush=True)
    print("== rec ==", rec.get("bot_settings_if_following_tape"), flush=True)


if __name__ == "__main__":
    main()
