#!/usr/bin/env python3
"""BTC 5-minute Up/Down 95–99¢ reversal study (read-only public APIs)."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "surf-arb-research/1.1 (read-only; no trading)"}
OUT = Path(__file__).with_name("btc_5m_reversal.json")
TICKS = (0.95, 0.96, 0.97, 0.98, 0.99)
WINDOWS = (30, 45, 90, 180, 300, 900)
FEE = 0.07


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


def ever_max(trades: list[dict], left_max: float) -> dict[str, float]:
    out = {"Up": 0.0, "Down": 0.0}
    for t in trades:
        if 0 <= t["left"] <= left_max:
            out[t["outcome"]] = max(out[t["outcome"]], t["px"])
    return out


def snapshot_at(trades: list[dict], left: float) -> dict[str, float]:
    """Last print of each side with seconds_left >= left (i.e. at that clock)."""
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
        out[key] = {
            "n": n,
            "win": row["win"],
            "lose": row["lose"],
            "wr": None if wr is None else round(wr, 6),
            "wr_ci95": None if ci is None else [round(ci[0], 6), round(ci[1], 6)],
            "reverse": None if wr is None else round(1.0 - wr, 6),
            "pnl_per_share": round(row["pnl"] / n, 6) if n else None,
            "pnl_sizew_per_share": round(row["pnl_sizew"] / row["size"], 6) if row["size"] else None,
            "need_wins_per_blowup": None
            if not n or wr in (None, 1.0) or row["lose"] == 0
            else round((row["win"] / row["lose"]) if row["lose"] else None, 2),
            "avg_px": round(sum(row["prices"]) / n, 4) if n else None,
        }
    return out


def main() -> None:
    now = int(time.time())
    last_closed = now - (now % 300) - 300
    days = 14
    starts = [last_closed - i * 300 for i in range(days * 24 * 12)]
    print(f"fetch {len(starts)} btc 5m windows ending {datetime.fromtimestamp(last_closed, timezone.utc).isoformat()}", flush=True)
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
        for win in WINDOWS:
            mx = ever_max(trades, win)
            fav = "Up" if mx["Up"] >= mx["Down"] else "Down"
            px = mx[fav]
            tick = bucket(px) or (0.99 if px >= 0.99 else None)
            if tick is None or px < 0.945:
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

    # unique steamroller events at 300s (full 5m)
    steam_full = [x for x in steam if x["window"] == 300]
    seen = set()
    steam_uniq = []
    for x in steam_full:
        if x["slug"] in seen:
            continue
        seen.add(x["slug"])
        steam_uniq.append(x)

    def math_table():
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
                "wins_to_offset_one_25usd_loss": round((-lose * 25) / (win * 25), 1),
            }
        return rows

    out = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asset": "btc",
        "horizon": "5m",
        "days": days,
        "markets_resolved": len(markets),
        "markets_with_tape": sum(1 for m in markets if m["trades"]),
        "errors": errors,
        "skipped": skipped,
        "fee_rate": FEE,
        "math": math_table(),
        "rule_first_print_any_side": "First public print that rounds to 95–99¢, that outcome held to official resolve.",
        "rule_first_buy": "First BUY print (lift ask) that rounds to 95–99¢.",
        "rule_ever_max": "If a side’s max print in last T seconds rounds to that tick, hold that side to resolve.",
        "rule_snapshot": "Richer last print at T seconds left, if it rounds into 95–99¢.",
        "first_print": finish(first_any),
        "first_buy": finish(first_buy),
        "ever_max": finish(ever),
        "snapshot_at_T": finish(snap),
        "steamrollers_hit_99_then_lost_in_5m": steam_uniq[:25],
        "steamroller_count_5m": len(steam_uniq),
        "honest": [
            "99¢ is not certain. One dump after you lift is −~99¢/share vs +~0.93¢ if you win.",
            "Win rate at 99¢ can look 98–99% and still be −EV after fees if blowups are fat.",
            "Prints that already traded are not your 250ms FAK fill. Live fill rate is lower.",
            "5m books can trade before the BTC window; this study keeps last 900s only.",
        ],
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT}", flush=True)
    # compact stdout
    for label, block in (("BUY", out["first_buy"]), ("MAX", out["ever_max"])):
        print(f"\n== {label} ==")
        for tick in TICKS:
            for win in (30, 45, 90, 300):
                prefix = "buy" if label == "BUY" else "max"
                key = f"{prefix}_{tick:.2f}_last{win}s"
                row = block.get(key)
                if not row:
                    continue
                print(
                    f"  {tick:.2f} last{win:>3}s n={row['n']:>4} wr={row['wr']} reverse={row['reverse']} "
                    f"pnl/sh={row['pnl_per_share']} ci={row['wr_ci95']}"
                )


if __name__ == "__main__":
    main()
