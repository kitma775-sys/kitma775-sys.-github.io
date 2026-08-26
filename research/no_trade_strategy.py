#!/usr/bin/env python3
"""Why rev 7 still prints zero fills, and which hunt changes would actually fire.

Read-only Gamma + CLOB + data-api. No orders.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.fees import taker_net
from app.universe import DEFAULT_ASSETS, asset_hit, is_updown, parse_tokens, seconds_left

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
HEALTH = "https://surf-arb.zeabur.app/health"
UA = {"User-Agent": "surf-arb-research/rev8 (read-only; no trading)"}
OUT = Path(__file__).with_name("no_trade_strategy.json")


def get_json(url: str, timeout: float = 25.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def iso_z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def top(levels, *, asks: bool):
    if not levels:
        return None, 0.0
    rows = sorted(((float(x["price"]), float(x["size"])) for x in levels), key=lambda x: x[0], reverse=not asks)
    return rows[0][0], rows[0][1]


def fetch_events(tag: str, *, live: bool, limit: int, now: datetime, horizon: float) -> list:
    params = {
        "limit": limit,
        "tag_slug": tag,
        "order": "endDate",
        "ascending": "true",
        "end_date_min": iso_z(now if live else now - timedelta(hours=6)),
        "end_date_max": iso_z(now + timedelta(seconds=horizon) if live else now),
    }
    if live:
        params["active"] = "true"
        params["closed"] = "false"
    else:
        params["closed"] = "true"
        params["ascending"] = "false"
    q = urllib.parse.urlencode(params)
    try:
        rows = get_json(f"{GAMMA}/events?{q}")
    except Exception as exc:
        return [{"_error": str(exc), "tag": tag}]
    return rows if isinstance(rows, list) else []


def normalize(events: list, tag: str, now: datetime) -> list[dict]:
    out = []
    for ev in events:
        if ev.get("_error"):
            return [ev]
        markets = ev.get("markets") or []
        if not markets:
            continue
        m = markets[0]
        slug = str(ev.get("slug") or m.get("slug") or "")
        if not is_updown(slug) or not asset_hit(slug, list(DEFAULT_ASSETS)):
            continue
        tokens = parse_tokens(m.get("clobTokenIds"))
        if len(tokens) < 2:
            continue
        end = m.get("endDate") or ev.get("endDate")
        fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        outs = m.get("outcomes")
        try:
            names = json.loads(outs) if isinstance(outs, str) else (outs or ["Up", "Down"])
        except json.JSONDecodeError:
            names = ["Up", "Down"]
        out.append(
            {
                "slug": slug,
                "tag": tag,
                "title": ev.get("title") or slug,
                "condition_id": m.get("conditionId") or "",
                "up_token": tokens[0],
                "down_token": tokens[1],
                "end": end,
                "seconds_left": seconds_left(end, now=now),
                "best_ask": m.get("bestAsk"),
                "best_bid": m.get("bestBid"),
                "fee_rate": fs.get("rate"),
                "outcomes": names,
                "volume24hr": float(m.get("volume24hr") or ev.get("volume24hr") or 0),
                "closed": bool(m.get("closed") or ev.get("closed")),
            }
        )
    return out


def book(token: str) -> dict:
    if not token:
        return {"asks": [], "bids": []}
    try:
        return get_json(f"{CLOB}/book?{urllib.parse.urlencode({'token_id': token})}") or {}
    except Exception:
        return {"asks": [], "bids": []}


def score_book(row: dict, fee_rate: float = 0.07) -> dict:
    up = book(row["up_token"])
    dn = book(row["down_token"])
    ua, us = top(up.get("asks") or [], asks=True)
    da, ds = top(dn.get("asks") or [], asks=True)
    ub, ubs = top(up.get("bids") or [], asks=False)
    db, dbs = top(dn.get("bids") or [], asks=False)
    ask_sum = None if ua is None or da is None else round(ua + da, 4)
    bid_sum = None if ub is None or db is None else round(ub + db, 4)
    tnet = None if ua is None or da is None else round(taker_net(1.0, ua, da, fee_rate), 4)
    mint_net = None
    if ub is not None and db is not None:
        # mint $1, sell both into bids as taker
        mint_net = round((ub + db) - 1.0 - (taker_net(1.0, 0, 0, 0) or 0), 4)
        from app.fees import pair_taker_fee

        mint_net = round((ub + db) - 1.0 - pair_taker_fee(1.0, ub, db, fee_rate), 4)
    return {
        **{k: row[k] for k in ("slug", "tag", "seconds_left", "best_ask", "volume24hr", "fee_rate")},
        "up_ask": ua,
        "down_ask": da,
        "up_ask_sz": us,
        "down_ask_sz": ds,
        "up_bid": ub,
        "down_bid": db,
        "ask_sum": ask_sum,
        "bid_sum": bid_sum,
        "taker_net": tnet,
        "mint_sell_net": mint_net,
        "empty_ask": ua is None or da is None,
        "two_ask": ua is not None and da is not None,
    }


def fetch_trades(cid: str, pages: int = 4) -> list[dict]:
    out = []
    offset = 0
    for _ in range(pages):
        q = urllib.parse.urlencode({"market": cid, "limit": 100, "offset": offset})
        try:
            batch = get_json(f"{DATA}/trades?{q}")
        except urllib.error.HTTPError:
            time.sleep(0.3)
            try:
                batch = get_json(f"{DATA}/trades?{q}")
            except Exception:
                break
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
        time.sleep(0.03)
    return out


def print_races(trades: list[dict], fee_rate: float = 0.07, window_s: int = 2) -> dict:
    """Same-second(ish) BUY prints on both legs imply someone lifted both asks."""
    rows = []
    for raw in trades:
        try:
            ts = int(raw.get("timestamp") or raw.get("t") or 0)
            px = float(raw["price"])
            sz = float(raw.get("size") or 0)
            side = str(raw.get("side") or "").upper()
        except (KeyError, TypeError, ValueError):
            continue
        outcome = str(raw.get("outcome") or "").strip().lower()
        idx = raw.get("outcomeIndex")
        if outcome in {"up", "yes"} or idx in (0, "0"):
            leg = "up"
        elif outcome in {"down", "no"} or idx in (1, "1"):
            leg = "down"
        else:
            continue
        if side != "BUY" or px <= 0 or sz <= 0 or ts <= 0:
            continue
        rows.append((ts, leg, px, sz))
    rows.sort()
    hits = []
    by_sec = defaultdict(lambda: {"up": [], "down": []})
    for ts, leg, px, sz in rows:
        for w in range(-window_s, window_s + 1):
            by_sec[ts + w][leg].append((px, sz, ts))
    seen = set()
    for ts, legs in by_sec.items():
        if not legs["up"] or not legs["down"]:
            continue
        up = min(legs["up"], key=lambda x: x[0])
        dn = min(legs["down"], key=lambda x: x[0])
        key = (min(up[2], dn[2]), round(up[0], 4), round(dn[0], 4))
        if key in seen:
            continue
        seen.add(key)
        gross = 1.0 - (up[0] + dn[0])
        net = taker_net(min(up[1], dn[1]), up[0], dn[0], fee_rate)
        if gross > 0:
            hits.append(
                {
                    "t": key[0],
                    "up": up[0],
                    "down": dn[0],
                    "gross": round(gross, 4),
                    "net": round(net, 4),
                    "size": round(min(up[1], dn[1]), 2),
                }
            )
    pos = [h for h in hits if h["net"] > 0]
    return {
        "buy_prints": len(rows),
        "two_leg_windows": len(hits),
        "taker_net_positive": len(pos),
        "best_net": max((h["net"] for h in pos), default=None),
        "examples": sorted(pos, key=lambda h: -h["net"])[:8],
    }


def probe_tags(now: datetime) -> dict:
    found = {}
    for tag in ("5M", "5m", "5min", "5-min", "5MIN", "15M", "1H", "1h", "4H"):
        rows = fetch_events(tag, live=True, limit=8, now=now, horizon=7200)
        if rows and isinstance(rows, list) and rows[0].get("_error"):
            found[tag] = {"error": rows[0]["_error"]}
            continue
        slugs = []
        for ev in rows or []:
            slug = str(ev.get("slug") or "")
            if slug:
                slugs.append(slug)
        found[tag] = {"n": len(rows or []), "slugs": slugs[:6]}
    return found


def main() -> None:
    now = datetime.now(timezone.utc)
    health = {}
    try:
        health = get_json(HEALTH, timeout=12)
    except Exception as exc:
        health = {"error": str(exc)}

    tag_probe = probe_tags(now)

    live_rows = []
    for tag, horizon in (("5M", 900), ("15M", 1800), ("1H", 3600)):
        raw = fetch_events(tag, live=True, limit=40, now=now, horizon=horizon)
        live_rows.extend(normalize(raw, tag, now))

    scored = []
    for row in live_rows:
        try:
            scored.append(score_book(row, float(row.get("fee_rate") or 0.07)))
        except Exception as exc:
            scored.append({"slug": row.get("slug"), "error": str(exc)})
        time.sleep(0.04)

    two = [r for r in scored if r.get("two_ask")]
    empty = [r for r in scored if r.get("empty_ask")]
    under = [r for r in two if (r.get("ask_sum") is not None and r["ask_sum"] < 1)]
    pos_net = [r for r in two if (r.get("taker_net") is not None and r["taker_net"] > 0)]
    mint_pos = [r for r in scored if (r.get("mint_sell_net") is not None and r["mint_sell_net"] > 0)]

# Historical: last 3 hours of closed 15M + 5M windows, print races.
    # ±2s min(up)+min(down) cherry-picks independent prints — not a simultaneous
    # two-ask book. Recorded only to show 5M has two-sided flow.
    start = now - timedelta(hours=3)
    races = []
    closed_n = 0
    for tag in ("5M", "15M"):
        raw = fetch_events(tag, live=False, limit=40, now=now, horizon=0)
        # closed fetch uses end_date_max=now, min=now-6h already in helper when live=False
        mkts = normalize(raw, tag, now)
        for m in mkts[:12]:
            closed_n += 1
            trades = fetch_trades(m["condition_id"], pages=3)
            info = print_races(trades)
            info["slug"] = m["slug"]
            info["tag"] = tag
            races.append(info)
            time.sleep(0.05)

    pos_races = [r for r in races if r.get("taker_net_positive")]
    findings = {
        "researched_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live_bot": health,
        "tag_probe": tag_probe,
        "live_books": {
            "n": len(scored),
            "two_ask": len(two),
            "empty_ask": len(empty),
            "ask_sum_lt_1": len(under),
            "taker_net_positive": len(pos_net),
            "mint_sell_positive": len(mint_pos),
            "min_ask_sum": min((r["ask_sum"] for r in two if r.get("ask_sum") is not None), default=None),
            "max_taker_net": max((r["taker_net"] for r in two if r.get("taker_net") is not None), default=None),
            "max_mint_sell_net": max((r["mint_sell_net"] for r in scored if r.get("mint_sell_net") is not None), default=None),
            "nearest_empty": sorted(
                [
                    {"slug": r.get("slug"), "left": r.get("seconds_left"), "up_ask": r.get("up_ask"), "down_ask": r.get("down_ask")}
                    for r in empty
                    if r.get("seconds_left") is not None
                ],
                key=lambda x: x["left"],
            )[:8],
            "best_two_ask": sorted(two, key=lambda r: (r.get("taker_net") is not None, r.get("taker_net") or -9))[-6:],
            "mint_examples": mint_pos[:6],
        },
        "print_races_3h": {
            "markets": closed_n,
            "markets_with_pos_net": len(pos_races),
            "total_pos_windows": sum(r.get("taker_net_positive") or 0 for r in races),
            "best": sorted(pos_races, key=lambda r: -(r.get("best_net") or 0))[:10],
        },
    }
    OUT.write_text(json.dumps(findings, indent=2, default=str))
    print(json.dumps({k: findings[k] for k in ("researched_at_utc", "live_bot", "tag_probe")}, indent=2, default=str))
    print(json.dumps(findings["live_books"], indent=2, default=str)[:4000])
    print(json.dumps(findings["print_races_3h"], indent=2, default=str)[:4000])
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
