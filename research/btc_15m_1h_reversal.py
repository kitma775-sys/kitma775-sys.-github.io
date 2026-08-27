#!/usr/bin/env python3
"""BTC 15-minute and 1-hour Up/Down 90–99¢ reversal study (read-only public APIs)."""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "surf-arb-research/1.2 (read-only; no trading)"}
OUT = Path(__file__).with_name("btc_15m_1h_reversal.json")
FIVE_M = Path(__file__).with_name("btc_5m_reversal.json")
TICKS = tuple(round(i / 100.0, 2) for i in range(90, 100))
WINDOWS_15M = (45, 90, 180, 300, 600, 900)
WINDOWS_1H = (90, 180, 300, 600, 900, 1800, 3600)
FEE = 0.07
ET = timezone(timedelta(hours=-4))  # US EDT; study window is summer 2026


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


def hour_slug(end_utc: datetime) -> str:
    start = end_utc - timedelta(hours=1)
    et = start.astimezone(ET)
    h = et.hour
    if h == 0:
        hour_s = "12am"
    elif h < 12:
        hour_s = f"{h}am"
    elif h == 12:
        hour_s = "12pm"
    else:
        hour_s = f"{h - 12}pm"
    month = et.strftime("%B").lower()
    return f"bitcoin-up-or-down-{month}-{et.day}-{et.year}-{hour_s}-et"


def fifteen_slug(end_utc: datetime) -> str:
    start = end_utc - timedelta(minutes=15)
    ts = int(start.timestamp())
    ts -= ts % 900
    return f"btc-updown-15m-{ts}"


def load_trades(cid: str, end: int, max_left: int) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while offset < 8000:
        batch = get_json(f"{DATA}/trades?market={cid}&limit=1000&offset={offset}") or []
        if not batch:
            break
        stop_older = False
        for t in batch:
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
            if left > max_left:
                stop_older = True
                break
            if left < -20:
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
        if stop_older or len(batch) < 1000:
            break
        offset += 1000
    rows.sort(key=lambda r: r["ts"])
    return rows


def load_market(slug: str, max_left: int) -> dict | None:
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
    trades = load_trades(cid, end, max_left)
    return {
        "slug": slug,
        "end": end,
        "winner": winner,
        "volume": float(m.get("volume") or 0),
        "trades": trades,
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
            "avg_px": round(sum(row["prices"]) / n, 4) if n else None,
        }
    return out


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


def analyze(markets: list[dict], windows: tuple[int, ...]) -> dict:
    first_buy: dict = {}
    first_any: dict = {}
    ever: dict = {}
    snap: dict = {}
    steam = []
    full_win = max(windows)
    for m in markets:
        winner = m["winner"]
        trades = m["trades"]
        if not trades:
            continue
        for win in windows:
            hits_any = first_hits_in(trades, win, buy_only=False)
            hits_buy = first_hits_in(trades, win, buy_only=True)
            for (tick, outcome), t in hits_any.items():
                add_stat(first_any, f"first_{tick:.2f}_last{win}s", outcome == winner, t["px"], t["size"])
            for (tick, outcome), t in hits_buy.items():
                add_stat(first_buy, f"buy_{tick:.2f}_last{win}s", outcome == winner, t["px"], t["size"])
        for win in windows:
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
        for win in windows:
            last = snapshot_at(trades, win)
            if not last:
                continue
            fav = "Up" if last.get("Up", 0) >= last.get("Down", 0) else "Down"
            px = last.get(fav, 0)
            tick = bucket(px)
            if tick is None:
                continue
            add_stat(snap, f"snap_{tick:.2f}_at{win}s", fav == winner, px, 25.0)
    steam_full = [x for x in steam if x["window"] == full_win]
    seen = set()
    steam_uniq = []
    for x in steam_full:
        if x["slug"] in seen:
            continue
        seen.add(x["slug"])
        steam_uniq.append(x)
    return {
        "first_buy": finish(first_buy),
        "first_print": finish(first_any),
        "ever_max": finish(ever),
        "snapshot_at_T": finish(snap),
        "steamrollers_hit_99_then_lost": steam_uniq[:20],
        "steamroller_count_full_window": len(steam_uniq),
    }


def compact_table(block: dict, prefix: str, windows: tuple[int, ...]) -> dict:
    out = {}
    for tick in TICKS:
        row = {}
        for win in windows:
            key = f"{prefix}_{tick:.2f}_last{win}s"
            cell = block.get(key)
            if not cell:
                continue
            row[f"last{win}s"] = {
                "n": cell["n"],
                "reverse": cell["reverse"],
                "wr": cell["wr"],
                "pnl_per_share": cell["pnl_per_share"],
                "wr_ci95": cell["wr_ci95"],
            }
        out[f"{tick:.2f}"] = row
    return out


def fetch_horizon(label: str, slugs: list[str], max_left: int, workers: int = 14) -> tuple[list[dict], int, int]:
    print(f"fetch {len(slugs)} {label} markets", flush=True)
    markets: list[dict] = []
    errors = 0
    skipped = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(load_market, slug, max_left): slug for slug in slugs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 100 == 0:
                print(
                    f"  {label} {done}/{len(slugs)} ok={len(markets)} skip={skipped} err={errors}",
                    flush=True,
                )
            try:
                row = fut.result()
            except Exception:
                errors += 1
                continue
            if not row or row.get("skip"):
                skipped += 1
                continue
            markets.append(row)
    print(f"loaded {len(markets)} resolved {label} markets (skip={skipped} err={errors})", flush=True)
    return markets, errors, skipped


def last_closed_15m() -> datetime:
    now = datetime.now(timezone.utc)
    aligned = now.replace(second=0, microsecond=0)
    aligned -= timedelta(minutes=aligned.minute % 15)
    return aligned - timedelta(minutes=15)


def last_closed_1h() -> datetime:
    now = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return now - timedelta(hours=1)


def compare_vs_5m(buy_15: dict, buy_1h: dict) -> dict:
    if not FIVE_M.exists():
        return {}
    five = json.loads(FIVE_M.read_text()).get("first_buy") or {}
    out = {}
    for tick in (0.90, 0.91, 0.92, 0.93, 0.94, 0.95, 0.96, 0.97, 0.98, 0.99):
        tick_key = f"{tick:.2f}"
        row = {}
        for win in (90, 180, 300):
            k = f"buy_{tick_key}_last{win}s"
            cell = {
                "5m": _rev(five.get(k)),
                "15m": _rev(buy_15.get(k)),
                "1h": _rev(buy_1h.get(k)),
            }
            row[f"last{win}s"] = cell
        row["full_session"] = {
            "5m": _rev(five.get(f"buy_{tick_key}_last300s")),
            "15m": _rev(buy_15.get(f"buy_{tick_key}_last900s")),
            "1h": _rev(buy_1h.get(f"buy_{tick_key}_last3600s")),
        }
        out[tick_key] = row
    return out


def _rev(cell: dict | None) -> dict | None:
    if not cell:
        return None
    return {
        "n": cell.get("n"),
        "reverse": cell.get("reverse"),
        "wr": cell.get("wr"),
        "pnl_per_share": cell.get("pnl_per_share"),
        "wr_ci95": cell.get("wr_ci95"),
    }


def main() -> None:
    days_15 = 14
    days_1h = 28
    end15 = last_closed_15m()
    end1h = last_closed_1h()
    slugs_15 = [fifteen_slug(end15 - timedelta(minutes=15 * i)) for i in range(days_15 * 96)]
    slugs_1h = [hour_slug(end1h - timedelta(hours=i)) for i in range(days_1h * 24)]

    markets_15, err15, skip15 = fetch_horizon("15m", slugs_15, max_left=1800)
    markets_1h, err1h, skip1h = fetch_horizon("1h", slugs_1h, max_left=7200)

    a15 = analyze(markets_15, WINDOWS_15M)
    a1h = analyze(markets_1h, WINDOWS_1H)

    out = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "asset": "btc",
        "ticks": [f"{t:.2f}" for t in TICKS],
        "fee_rate": FEE,
        "math": math_table(),
        "rule_first_buy": "First BUY print (lift ask) that rounds to 90–99¢, that outcome held to official resolve.",
        "rule_first_print": "First public print (buy or sell) that rounds to 90–99¢.",
        "rule_ever_max": "If a side’s max print in last T seconds rounds into 90–99¢, hold that side to resolve.",
        "rule_snapshot": "Richer last print at T seconds left, if it rounds into 90–99¢.",
        "honest": [
            "Last-3-minute of a 15m/1h contract is not the same as last-3-minute of a 5m contract: more of the move is already decided.",
            "Full-session 90¢ on 1h can mean-revert more than a 5m 90¢ because the path is hours long.",
            "99¢ still needs reverse <0.93% after 7% fees. A calmer reverse that is still 1.2% is −EV at 99¢.",
            "Prints that already traded are not a 250ms FAK fill. Live fill rate is lower.",
        ],
        "15m": {
            "days": days_15,
            "windows_s": list(WINDOWS_15M),
            "last_end_utc": end15.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "markets_resolved": len(markets_15),
            "markets_with_tape": sum(1 for m in markets_15 if m["trades"]),
            "errors": err15,
            "skipped": skip15,
            **a15,
            "buy_table": compact_table(a15["first_buy"], "buy", WINDOWS_15M),
        },
        "1h": {
            "days": days_1h,
            "windows_s": list(WINDOWS_1H),
            "last_end_utc": end1h.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "markets_resolved": len(markets_1h),
            "markets_with_tape": sum(1 for m in markets_1h if m["trades"]),
            "errors": err1h,
            "skipped": skip1h,
            **a1h,
            "buy_table": compact_table(a1h["first_buy"], "buy", WINDOWS_1H),
        },
        "vs_5m_first_buy": compare_vs_5m(a15["first_buy"], a1h["first_buy"]),
    }
    OUT.write_text(json.dumps(out, indent=2))
    print(f"wrote {OUT}", flush=True)
    for label, block, windows in (
        ("15m BUY", a15["first_buy"], (90, 180, 300, 900)),
        ("1h BUY", a1h["first_buy"], (180, 300, 900, 3600)),
    ):
        print(f"\n== {label} ==", flush=True)
        for tick in TICKS:
            parts = []
            for win in windows:
                key = f"buy_{tick:.2f}_last{win}s"
                row = block.get(key)
                if not row:
                    continue
                parts.append(
                    f"last{win}s n={row['n']} rev={row['reverse']} pnl={row['pnl_per_share']}"
                )
            if parts:
                print(f"  {tick:.2f}  " + " | ".join(parts), flush=True)


if __name__ == "__main__":
    main()
