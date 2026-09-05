#!/usr/bin/env python3
"""Pull closed 15M/1H crypto Up/Down trades and replay the live hunt.

Usage:
  python3 research/backtest.py --hours 8
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.replay import live_replay_settings, replay_market
from app.rescue import parse_outcome_prices
from app.universe import DEFAULT_ASSETS, DEFAULT_TAGS, asset_hit, is_updown, parse_tokens

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {"User-Agent": "surf-arb-backtest/1.0 (read-only; no trading)"}
OUT = Path(__file__).with_name("backtest_results.json")


def get_json(url: str, timeout: float = 40.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fetch_closed_events(tag: str, start: datetime, end: datetime) -> list[dict]:
    rows: list[dict] = []
    offset = 0
    while offset <= 2000:
        q = urllib.parse.urlencode(
            {
                "closed": "true",
                "limit": 100,
                "offset": offset,
                "tag_slug": tag,
                "end_date_min": iso_z(start),
                "end_date_max": iso_z(end),
                "order": "endDate",
                "ascending": "false",
            }
        )
        page = get_json(f"{GAMMA}/events?{q}")
        if not isinstance(page, list) or not page:
            break
        rows.extend(page)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(0.05)
    return rows


def fetch_trades(condition_id: str, limit_pages: int = 12) -> list[dict]:
    out: list[dict] = []
    offset = 0
    for _ in range(limit_pages):
        q = urllib.parse.urlencode({"market": condition_id, "limit": 100, "offset": offset})
        try:
            batch = get_json(f"{DATA}/trades?{q}")
        except urllib.error.HTTPError:
            time.sleep(0.4)
            batch = get_json(f"{DATA}/trades?{q}")
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.04)
    return out


def pick_markets(events: list[dict], assets: list[str]) -> list[dict]:
    picked: list[dict] = []
    seen: set[str] = set()
    for ev in events:
        markets = ev.get("markets") or []
        if not markets:
            continue
        m = markets[0]
        slug = str(ev.get("slug") or m.get("slug") or "")
        if not is_updown(slug) or not asset_hit(slug, assets):
            continue
        cid = str(m.get("conditionId") or "")
        if not cid or cid in seen:
            continue
        tokens = parse_tokens(m.get("clobTokenIds"))
        if len(tokens) < 2:
            continue
        end = m.get("endDate") or ev.get("endDate")
        if not end:
            continue
        seen.add(cid)
        prices = parse_outcome_prices(m.get("outcomePrices"))
        picked.append(
            {
                "slug": slug,
                "condition_id": cid,
                "end": end,
                "tag": ev.get("_tag") or "",
                "resolution": prices,
            }
        )
    return picked


def sum_stats(parts: list[dict]) -> dict:
    keys = [
        "n_trades_in",
        "taker_n",
        "taker_pnl",
        "maker_quoted",
        "maker_two_sided_n",
        "maker_two_sided_pnl",
        "maker_hedge_n",
        "maker_hedge_pnl",
        "maker_dump_n",
        "maker_dump_pnl",
        "maker_expire_unfilled",
        "maker_expire_settle_n",
        "maker_expire_settle_pnl",
        "pnl",
    ]
    out = {k: 0.0 if "pnl" in k else 0 for k in keys}
    out["markets"] = len(parts)
    worst: list[dict] = []
    best: list[dict] = []
    for p in parts:
        for k in keys:
            out[k] = round(out[k] + (p.get(k) or 0), 6) if "pnl" in k else out[k] + int(p.get(k) or 0)
        rec = {"slug": p.get("slug"), "pnl": p.get("pnl"), "tag": p.get("tag")}
        worst.append(rec)
        best.append(rec)
    worst.sort(key=lambda r: r["pnl"] or 0)
    best.sort(key=lambda r: r["pnl"] or 0, reverse=True)
    out["worst"] = worst[:8]
    out["best"] = best[:8]
    out["pnl"] = round(out["pnl"], 4)
    out["taker_pnl"] = round(out["taker_pnl"], 4)
    out["maker_two_sided_pnl"] = round(out["maker_two_sided_pnl"], 4)
    out["maker_hedge_pnl"] = round(out["maker_hedge_pnl"], 4)
    out["maker_dump_pnl"] = round(out["maker_dump_pnl"], 4)
    out["maker_expire_settle_pnl"] = round(out["maker_expire_settle_pnl"], 4)
    return out


def run(hours: float, max_markets: int) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    start = now - timedelta(hours=hours)
    events: list[dict] = []
    for tag in DEFAULT_TAGS:
        page = fetch_closed_events(tag, start, now)
        for ev in page:
            ev["_tag"] = tag
        events.extend(page)
        time.sleep(0.05)
    markets = pick_markets(events, DEFAULT_ASSETS)
    if max_markets > 0:
        markets = markets[:max_markets]

    live_s = live_replay_settings()
    maker_s = live_replay_settings(maker_window_seconds=75.0)
    tight_s = live_replay_settings(maker_window_seconds=75.0, maker_max_skew=0.10)
    sync_parts: list[dict] = []
    maker_parts: list[dict] = []
    tight_parts: list[dict] = []
    errors = 0
    for i, mkt in enumerate(markets, 1):
        try:
            trades = fetch_trades(mkt["condition_id"])
            kw = {"end": mkt["end"], "resolution": mkt.get("resolution"), "slug": mkt["slug"]}
            sync = replay_market(trades, settings=live_s, allow_taker=True, **kw)
            maker = replay_market(trades, settings=maker_s, allow_taker=False, **kw)
            tight = replay_market(trades, settings=tight_s, allow_taker=False, **kw)
        except Exception as exc:
            errors += 1
            print(f"skip {mkt['slug']}: {type(exc).__name__}: {exc}"[:200], flush=True)
            time.sleep(0.3)
            continue
        for part in (sync, maker, tight):
            part["slug"] = mkt["slug"]
            part["tag"] = mkt["tag"]
            part.pop("events", None)
        sync_parts.append(sync)
        maker_parts.append(maker)
        tight_parts.append(tight)
        if i % 15 == 0 or i == len(markets):
            print(
                f"replay {i}/{len(markets)} maker_pnl={sum(p['pnl'] for p in maker_parts):.2f} "
                f"sync_taker_pnl={sum(p['pnl'] for p in sync_parts):.2f}",
                flush=True,
            )
        time.sleep(0.03)

    sync_sum = sum_stats(sync_parts)
    maker_sum = sum_stats(maker_parts)
    tight_sum = sum_stats(tight_parts)
    starting = 500.0
    verdict = _verdict(maker_sum, sync_sum)
    summary = {
        "researched_at_utc": now.isoformat().replace("+00:00", "Z"),
        "window_hours": hours,
        "tags": DEFAULT_TAGS,
        "assets": DEFAULT_ASSETS,
        "markets_listed": len(markets),
        "markets_replayed": maker_sum["markets"],
        "fetch_errors": errors,
        "data": "data-api.polymarket.com/trades per conditionId. prices-history cannot be used: YES+NO always sums to 1.00.",
        "models": {
            "sync_taker": "Taker uses last BUY prints on both legs within 1s (optimistic vs live CLOB asks). Maker uses last SELL prints within 10s, fills when a BUY prints through the resting bid.",
            "maker_only": "Same maker model, taker disabled. Closest to the HTTP bot, which almost never sees ask_sum < 1 at rest.",
            "tight_skew_maker": "maker_only with maker_max_skew=0.10 instead of 0.28.",
        },
        "sync_taker": _book(starting, sync_sum),
        "maker_only": _book(starting, maker_sum),
        "tight_skew_maker": _book(starting, tight_sum),
        "feasibility": verdict,
        "settings": {
            "min_edge": live_s["min_edge"],
            "maker_min_edge": live_s["maker_min_edge"],
            "maker_window_seconds": maker_s["maker_window_seconds"],
            "live_maker_window_seconds": live_s["maker_window_seconds"],
            "maker_max_skew": live_s["maker_max_skew"],
            "maker_min_leg": live_s["maker_min_leg"],
            "fee_rate": live_s["fee_rate"],
            "max_usd_per_trade": live_s["max_usd_per_trade"],
            "ask_stale_s": 1.0,
            "bid_stale_s": 10.0,
        },
    }
    OUT.write_text(json.dumps(summary, indent=2)[:400000])
    print(json.dumps({k: summary[k] for k in ("markets_replayed", "fetch_errors", "maker_only", "tight_skew_maker", "sync_taker", "feasibility")}, indent=2, default=str)[:5000])
    return summary


def _book(starting: float, summed: dict) -> dict:
    return {
        "starting_cash": starting,
        "ending_equity": round(starting + float(summed.get("pnl") or 0), 4),
        "total_pnl": summed.get("pnl"),
        **{k: v for k, v in summed.items() if k not in {"worst", "best"}},
        "worst": summed.get("worst"),
        "best": summed.get("best"),
    }


def _verdict(maker: dict, sync: dict) -> str:
    mp = float(maker.get("pnl") or 0)
    quoted = int(maker.get("maker_quoted") or 0)
    two = int(maker.get("maker_two_sided_n") or 0)
    hedge_n = int(maker.get("maker_hedge_n") or 0)
    hedge_pnl = float(maker.get("maker_hedge_pnl") or 0)
    dump_n = int(maker.get("maker_dump_n") or 0)
    dump_pnl = float(maker.get("maker_dump_pnl") or 0)
    taker_n = int(sync.get("taker_n") or 0)
    taker_pnl = float(sync.get("taker_pnl") or 0)
    bits = [
        f"HTTP-like maker-only 6h tape: quotes {quoted}, two-sided fills {two}, "
        f"hedge {hedge_n} (${hedge_pnl:.2f}), dump {dump_n} (${dump_pnl:.2f}), total ${mp:.2f}.",
        f"Optimistic 1s print-implied taker (not live books): {taker_n} fills, ${taker_pnl:.2f}. Live rest-state ask_sum stays ≥ 1.01 so the HTTP bot does not harvest this.",
    ]
    if two == 0 and mp <= 0:
        bits.append("Last-window maker is not +EV on this history: fills are one-sided, matching live paper (SOL hedge loss). Top-tier would be websocket + tighter skew or no maker.")
    elif mp > 0:
        bits.append("Maker-only is slightly positive on this window; still thin vs live adverse selection.")
    return " ".join(bits)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=float, default=8.0)
    p.add_argument("--max-markets", type=int, default=0, help="0 = all in window")
    args = p.parse_args()
    run(args.hours, args.max_markets)


if __name__ == "__main__":
    main()
