#!/usr/bin/env python3
"""Read-only census of Polymarket complement / complete-set edges.

Pages the top ~2100 open markets by 24h volume (Gamma offset cap), then
batch-fetches CLOB top-of-book. Never signs or places orders.

Usage:
  python3 research/universe_scan.py
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
UA = {"User-Agent": "surf-arb-research/1.0 (read-only; no trading)"}
OUT = Path(__file__).with_name("universe_scan.json")


def get_json(url: str, timeout: float = 40.0):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def post_json(url: str, payload, timeout: float = 40.0):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data, headers={**UA, "Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def parse_json(raw, default):
    if isinstance(raw, (list, dict)):
        return raw
    if not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default


def taker_fee(shares: float, price: float, rate: float) -> float:
    p = min(max(float(price), 0.0), 1.0)
    return shares * max(float(rate), 0.0) * p * (1.0 - p)


def pair_taker_net(up: float, down: float, rate: float, shares: float = 1.0) -> float:
    gross = (1.0 - (up + down)) * shares
    return gross - taker_fee(shares, up, rate) - taker_fee(shares, down, rate)


def fetch_markets() -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    offset = 0
    while offset <= 2000:
        page = get_json(
            f"{GAMMA}/markets?closed=false&limit=100&offset={offset}&order=volume24hr&ascending=false"
        )
        if not page:
            break
        for m in page:
            cid = str(m.get("conditionId") or m.get("id") or "")
            if cid in seen:
                continue
            seen.add(cid)
            rows.append(m)
        if len(page) < 100:
            break
        offset += 100
        time.sleep(0.05)
    return rows


def live_binary(m: dict, now: datetime) -> bool:
    if m.get("closed") or not m.get("acceptingOrders") or not m.get("enableOrderBook"):
        return False
    end = m.get("endDate")
    if end:
        try:
            end_dt = datetime.fromisoformat(str(end).replace("Z", "+00:00"))
            if end_dt < now:
                return False
        except ValueError:
            pass
    tokens = parse_json(m.get("clobTokenIds"), [])
    return isinstance(tokens, list) and len(tokens) >= 2


def batch_prices(requests: list[dict], chunk: int = 80) -> dict:
    out: dict = {}
    for i in range(0, len(requests), chunk):
        part = requests[i : i + chunk]
        for attempt in range(3):
            try:
                got = post_json(f"{CLOB}/prices", part)
                if isinstance(got, dict):
                    out.update(got)
                break
            except urllib.error.HTTPError as exc:
                if exc.code in {429, 500, 502, 503} and attempt < 2:
                    time.sleep(0.4 * (attempt + 1))
                    continue
                # skip bad chunk
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.3)
                    continue
                break
        time.sleep(0.04)
    return out


def px(blob: dict | None, side: str) -> float | None:
    if not isinstance(blob, dict):
        return None
    raw = blob.get(side) or blob.get(side.lower()) or blob.get(side.upper())
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def book_top(token_id: str) -> dict:
    empty = {"asks": [], "bids": []}
    try:
        data = get_json(f"{CLOB}/book?token_id={token_id}", timeout=12)
    except Exception:
        return empty
    return {
        "asks": data.get("asks") or [],
        "bids": data.get("bids") or [],
    }


def best_level(levels, reverse: bool) -> tuple[float | None, float]:
    parsed = []
    for row in levels or []:
        try:
            parsed.append((float(row["price"]), float(row["size"])))
        except (KeyError, TypeError, ValueError):
            continue
    if not parsed:
        return None, 0.0
    parsed.sort(key=lambda x: x[0], reverse=reverse)
    return parsed[0]


def main() -> int:
    now = datetime.now(timezone.utc)
    print(f"# universe scan {now.isoformat()}", flush=True)
    markets = fetch_markets()
    print(f"# gamma markets {len(markets)}", flush=True)

    fee_n: Counter = Counter()
    fee_vol: dict[str, float] = defaultdict(float)
    fee_type: Counter = Counter()
    tick_n: Counter = Counter()
    live_bins: list[dict] = []
    events: dict[str, list[dict]] = defaultdict(list)

    for m in markets:
        fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
        rate = fs.get("rate")
        rate_k = "none" if rate is None else f"{float(rate):.2f}"
        fee_n[rate_k] += 1
        fee_vol[rate_k] += float(m.get("volume24hr") or 0)
        fee_type[str(m.get("feeType") or "unknown")] += 1
        tick_n[str(m.get("orderPriceMinTickSize"))] += 1
        evs = m.get("events") or []
        eid = str((evs[0].get("id") if evs else None) or m.get("conditionId") or m.get("id"))
        events[eid].append(m)
        if live_binary(m, now):
            tokens = parse_json(m.get("clobTokenIds"), [])
            live_bins.append(
                {
                    "id": m.get("id"),
                    "slug": m.get("slug"),
                    "question": m.get("question") or m.get("slug"),
                    "event_id": eid,
                    "event_title": (evs[0].get("title") if evs else None) or m.get("question"),
                    "yes_token": str(tokens[0]),
                    "no_token": str(tokens[1]),
                    "fee_rate": float(rate or 0.0),
                    "fee_type": m.get("feeType"),
                    "neg_risk": bool(m.get("negRisk")),
                    "volume24hr": float(m.get("volume24hr") or 0),
                    "liquidity": float(m.get("liquidityNum") or m.get("liquidity") or 0),
                    "tick": float(m.get("orderPriceMinTickSize") or 0.01),
                    "end": m.get("endDate"),
                    "outcomes": parse_json(m.get("outcomes"), ["Yes", "No"]),
                }
            )

    print(f"# live binaries {len(live_bins)}", flush=True)

    reqs = []
    for row in live_bins:
        for tok in (row["yes_token"], row["no_token"]):
            reqs.append({"token_id": tok, "side": "SELL"})
            reqs.append({"token_id": tok, "side": "BUY"})
    print(f"# price requests {len(reqs)}", flush=True)
    prices = batch_prices(reqs)
    print(f"# price rows {len(prices)}", flush=True)

    scored = []
    missing = 0
    for row in live_bins:
        y = prices.get(row["yes_token"]) or {}
        n = prices.get(row["no_token"]) or {}
        ya, na = px(y, "SELL"), px(n, "SELL")
        yb, nb = px(y, "BUY"), px(n, "BUY")
        if ya is None or na is None:
            missing += 1
            continue
        ask_sum = round(ya + na, 6)
        bid_sum = None if yb is None or nb is None else round(yb + nb, 6)
        rate = row["fee_rate"]
        tnet = round(pair_taker_net(ya, na, rate, 1.0), 6)
        maker_gross = None if bid_sum is None else round(1.0 - bid_sum, 6)
        slug = str(row["slug"] or "")
        scored.append(
            {
                **row,
                "yes_ask": ya,
                "no_ask": na,
                "yes_bid": yb,
                "no_bid": nb,
                "ask_sum": ask_sum,
                "bid_sum": bid_sum,
                "taker_net_1": tnet,
                "maker_gross": maker_gross,
                "is_15m_crypto": ("-updown-15m-" in slug) or ("15m" in slug and any(a in slug for a in ("btc", "eth", "sol", "xrp"))),
            }
        )

    taker_hits = [r for r in scored if r["taker_net_1"] > 0 and r["ask_sum"] < 1]
    under = [r for r in scored if r["ask_sum"] < 0.999]
    one_tick = [r for r in scored if 0.999 <= r["ask_sum"] <= 1.002]
    over = [r for r in scored if r["ask_sum"] > 1.002]
    maker_1c = [r for r in scored if r.get("maker_gross") is not None and r["maker_gross"] >= 0.009]
    by_fee_taker = Counter(f"{r['fee_rate']:.2f}" for r in taker_hits)

    # Deep-book the best taker candidates for fillable size.
    deep = []
    for row in sorted(taker_hits, key=lambda r: r["taker_net_1"], reverse=True)[:40]:
        yb, nbk = book_top(row["yes_token"]), book_top(row["no_token"])
        ya, ys = best_level(yb["asks"], False)
        na, ns = best_level(nbk["asks"], False)
        if ya is None or na is None:
            continue
        fillable = min(ys, ns)
        fees = taker_fee(fillable, ya, row["fee_rate"]) + taker_fee(fillable, na, row["fee_rate"])
        net = (1.0 - (ya + na)) * fillable - fees
        deep.append(
            {
                "slug": row["slug"],
                "question": row["question"],
                "fee_rate": row["fee_rate"],
                "fee_type": row["fee_type"],
                "volume24hr": row["volume24hr"],
                "ask_sum": round(ya + na, 4),
                "fillable": round(fillable, 2),
                "taker_net_usd": round(net, 4),
                "taker_net_1": row["taker_net_1"],
            }
        )
        time.sleep(0.03)

    # Complete-set (sum of YES asks) on multi-market events.
    complete = []
    for eid, group in events.items():
        if len(group) < 3:
            continue
        live = [m for m in group if live_binary(m, now)]
        if len(live) < 3:
            continue
        yes_asks = []
        ok = True
        rate = 0.0
        vol = 0.0
        for m in live:
            tokens = parse_json(m.get("clobTokenIds"), [])
            ya = px(prices.get(str(tokens[0])), "SELL")
            if ya is None:
                ok = False
                break
            yes_asks.append(ya)
            fs = m.get("feeSchedule") if isinstance(m.get("feeSchedule"), dict) else {}
            rate = float(fs.get("rate") or 0.0)
            vol += float(m.get("volume24hr") or 0)
        if not ok:
            continue
        s = sum(yes_asks)
        title = (group[0].get("events") or [{}])[0].get("title") or group[0].get("question")
        complete.append(
            {
                "event_id": eid,
                "title": title,
                "n": len(live),
                "yes_ask_sum": round(s, 4),
                "fee_rate": rate,
                "neg_risk": any(bool(m.get("negRisk")) for m in live),
                "volume24hr": round(vol, 1),
                "taker_gross": round(1.0 - s, 4),
            }
        )
    complete_hits = [c for c in complete if c["yes_ask_sum"] < 0.995]

    def bucket(rows, key="ask_sum"):
        xs = [r[key] for r in rows if r.get(key) is not None]
        if not xs:
            return {}
        xs.sort()
        return {
            "n": len(xs),
            "p10": round(xs[int(0.10 * (len(xs) - 1))], 4),
            "p50": round(xs[int(0.50 * (len(xs) - 1))], 4),
            "p90": round(xs[int(0.90 * (len(xs) - 1))], 4),
            "min": round(xs[0], 4),
            "max": round(xs[-1], 4),
        }

    by_fee_ask = {}
    for r in scored:
        by_fee_ask.setdefault(f"{r['fee_rate']:.2f}", []).append(r)
    fee_ask_stats = {k: bucket(v) for k, v in by_fee_ask.items()}

    crypto_15 = [r for r in scored if r["is_15m_crypto"]]
    rest = [r for r in scored if not r["is_15m_crypto"]]

    summary = {
        "researched_at_utc": now.isoformat(),
        "gamma_markets": len(markets),
        "live_binaries": len(live_bins),
        "scored": len(scored),
        "missing_book": missing,
        "fee_count": dict(fee_n),
        "fee_volume24hr": {k: round(v, 1) for k, v in fee_vol.items()},
        "fee_type": dict(fee_type.most_common()),
        "tick": dict(tick_n.most_common()),
        "events": len(events),
        "multi_market_events": sum(1 for v in events.values() if len(v) >= 3),
        "ask_sum_all": bucket(scored),
        "ask_sum_15m_crypto": bucket(crypto_15),
        "ask_sum_rest": bucket(rest),
        "ask_sum_by_fee": fee_ask_stats,
        "taker_net_all": bucket(scored, "taker_net_1"),
        "counts": {
            "ask_sum_lt_1": len(under),
            "ask_sum_around_1": len(one_tick),
            "ask_sum_gt_1.002": len(over),
            "taker_net_positive": len(taker_hits),
            "maker_gross_ge_1c": len(maker_1c),
            "complete_set_events": len(complete),
            "complete_set_underround": len(complete_hits),
            "scored_15m_crypto": len(crypto_15),
            "scored_rest": len(rest),
        },
        "taker_hits_by_fee": dict(by_fee_taker),
        "top_taker_hits": sorted(taker_hits, key=lambda r: r["taker_net_1"], reverse=True)[:25],
        "deep_book_taker_hits": deep,
        "top_complete_underround": sorted(complete_hits, key=lambda c: c["yes_ask_sum"])[:20],
        "worst_overround": sorted(scored, key=lambda r: r["ask_sum"], reverse=True)[:8],
        "tightest_underround": sorted(under, key=lambda r: r["ask_sum"])[:12],
    }
    OUT.write_text(json.dumps(summary, indent=2, default=str)[:900000])
    print(json.dumps({k: summary[k] for k in [
        "gamma_markets", "live_binaries", "scored", "missing_book",
        "fee_count", "fee_volume24hr", "counts", "taker_hits_by_fee",
        "ask_sum_all", "ask_sum_15m_crypto", "ask_sum_rest", "ask_sum_by_fee",
        "taker_net_all",
    ]}, indent=2))
    print(f"# wrote {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
