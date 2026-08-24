#!/usr/bin/env python3
"""Paper scanner for Polymarket YES+NO complement gaps.

Read-only. Never signs or places orders. Uses public Gamma + CLOB HTTP APIs.

Usage:
  python3 research/scan_books.py
  python3 research/scan_books.py --tag 15M --limit 12
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "kitma775-paper-scanner/1.0 (research; no trading)"}


def get_json(url: str, timeout: float = 20.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def taker_fee(shares: float, price: float, rate: float) -> float:
    """Official Polymarket fee: C * feeRate * p * (1 - p). Makers pay 0."""
    p = min(max(price, 0.0), 1.0)
    return shares * rate * p * (1.0 - p)


def best_ask(book: dict) -> tuple[float | None, float]:
    asks = book.get("asks") or []
    if not asks:
        return None, 0.0
    levels = sorted(((float(x["price"]), float(x["size"])) for x in asks), key=lambda x: x[0])
    return levels[0][0], levels[0][1]


def is_live(market: dict, event: dict) -> bool:
    if market.get("closed") or event.get("closed"):
        return False
    if market.get("acceptingOrders") is False:
        return False
    end = market.get("endDate") or event.get("endDate")
    if not end:
        return True
    try:
        end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
    except ValueError:
        return True
    return end_dt >= datetime.now(timezone.utc)


def scan(tag: str, limit: int, min_edge: float, fee_rate: float) -> list[dict]:
    events = get_json(
        f"{GAMMA}/events?active=true&closed=false&limit={max(limit * 4, 24)}&tag_slug={urllib.parse.quote(tag)}"
    )
    rows: list[dict] = []
    for event in events or []:
        markets = event.get("markets") or []
        if not markets:
            continue
        market = markets[0]
        if not is_live(market, event):
            continue
        if len(rows) >= limit:
            break
        try:
            tokens = json.loads(market["clobTokenIds"])
            outcomes = json.loads(market.get("outcomes") or '["Up","Down"]')
        except (KeyError, json.JSONDecodeError, TypeError):
            continue
        if len(tokens) < 2:
            continue
        yes_book = get_json(f"{CLOB}/book?token_id={tokens[0]}")
        no_book = get_json(f"{CLOB}/book?token_id={tokens[1]}")
        yes_ask, yes_sz = best_ask(yes_book)
        no_ask, no_sz = best_ask(no_book)
        if yes_ask is None or no_ask is None:
            rows.append(
                {
                    "title": event.get("title"),
                    "slug": event.get("slug"),
                    "status": "ONE_SIDED_OR_EMPTY",
                    "yes_ask": yes_ask,
                    "no_ask": no_ask,
                }
            )
            continue
        pair = yes_ask + no_ask
        gross = 1.0 - pair
        fillable = min(yes_sz, no_sz)
        fees = taker_fee(fillable, yes_ask, fee_rate) + taker_fee(fillable, no_ask, fee_rate)
        net = gross * fillable - fees
        rows.append(
            {
                "title": event.get("title"),
                "slug": event.get("slug"),
                "outcomes": outcomes,
                "yes_ask": yes_ask,
                "no_ask": no_ask,
                "pair_cost": round(pair, 4),
                "gross_edge": round(gross, 4),
                "fillable": round(fillable, 2),
                "taker_fee_usd": round(fees, 4),
                "taker_net_usd": round(net, 4),
                "maker_gross_usd": round(gross * fillable, 4),
                "tradeable_taker": net > 0 and gross >= min_edge,
                "end": market.get("endDate") or event.get("endDate"),
            }
        )
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description="Paper-scan Polymarket complement gaps.")
    parser.add_argument("--tag", default="15M")
    parser.add_argument("--limit", type=int, default=12)
    parser.add_argument("--min-edge", type=float, default=0.01)
    parser.add_argument("--fee-rate", type=float, default=0.07, help="Crypto taker rate is 0.07")
    args = parser.parse_args()

    print(
        f"# paper scan {datetime.now(timezone.utc).isoformat()} tag={args.tag} fee_rate={args.fee_rate}",
        file=sys.stderr,
    )
    rows = scan(args.tag, args.limit, args.min_edge, args.fee_rate)
    print(json.dumps(rows, indent=2))
    tradeable = [r for r in rows if r.get("tradeable_taker")]
    print(
        f"# {len(rows)} markets, {len(tradeable)} would survive taker fees at top-of-book",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
