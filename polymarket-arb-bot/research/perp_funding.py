"""Funding on Polymarket perps: is getting paid to hold the other side positive?

Funding is hourly. A positive rate means longs pay shorts. Two books:

1. Single name. Short when the last rate is positive, long when negative.
   Hold one hour. PnL is the funding received minus the hour's price change
   minus a 0.08% round-trip taker fee. This is directional and should lose
   to noise unless the rate predicts the next hour.

2. Pair. Each hour, short the crypto with the higher funding rate and long
   the one with the lower rate, equal notional. PnL is the funding gap plus
   the relative price change. Fee is charged only when the pair flips.
   Sample is split in half.

    python research/perp_funding.py research/perp_funding_2026-10-16.json
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research/perp_funding.json")
API = "https://api.perpetuals.polymarket.com/v1/info"
NAMES = ("BTC-USD", "ETH-USD", "SOL-USD", "NAS100-USD", "SP500-USD")
ROUND_TRIP = 0.0008


def get(path: str, **params):
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=40) as resp:
        return json.loads(resp.read())


def pages(path: str, **params) -> list:
    rows = []
    start = 1
    while True:
        page = get(path, start_timestamp=start, **params)
        chunk = page.get("data") or []
        if not chunk:
            break
        rows.extend(chunk)
        if not page.get("more"):
            break
        last = chunk[-1][0] if isinstance(chunk[-1], list) else chunk[-1]["timestamp"]
        start = int(last) + 1
    return rows


def load_all() -> dict[str, dict]:
    ids = {row["symbol"]: row["instrument_id"] for row in get("instruments")}
    out = {}
    for name in NAMES:
        iid = ids[name]
        funding = pages("funding", instrument_id=iid)
        bars = pages("klines", instrument_id=iid, interval="1h")
        print(f"{name} funding {len(funding)} bars {len(bars)}", flush=True)
        out[name] = {"funding": funding, "bars": bars}
    return out


def hour_close(bars) -> dict[int, float]:
    return {int(b[0]): float(b[4]) for b in bars}


def single(name: str, pack: dict) -> dict:
    close = hour_close(pack["bars"])
    times = sorted(close)
    nxt = {times[i]: times[i + 1] for i in range(len(times) - 1)}
    rates = {}
    for row in pack["funding"]:
        rates[(int(row["timestamp"]) // 3_600_000) * 3_600_000] = float(row["funding_rate"])
    pnl = []
    for ts, rate in sorted(rates.items()):
        if ts not in close or ts not in nxt or nxt[ts] not in close:
            continue
        entry, exit_ = close[ts], close[nxt[ts]]
        side = -1 if rate > 0 else 1  # receive the funding
        price = side * (exit_ - entry) / entry
        carried = abs(rate)  # the side we chose receives |rate| when we fade the sign
        # If rate > 0, shorts receive +rate. We are short. Good.
        # If rate < 0, longs receive -rate = |rate|. We are long. Good.
        pnl.append(carried + price - ROUND_TRIP)
    half = len(pnl) // 2
    def pack_pnl(xs):
        return {
            "n": len(xs),
            "mean": round(sum(xs) / len(xs), 6) if xs else None,
            "total": round(sum(xs), 4) if xs else None,
            "positive_hours": round(sum(x > 0 for x in xs) / len(xs), 3) if xs else None,
        }
    rate_vals = list(rates.values())
    return {
        "hours": len(rate_vals),
        "mean_funding": round(sum(rate_vals) / len(rate_vals), 8) if rate_vals else None,
        "pct_positive_funding": round(sum(r > 0 for r in rate_vals) / len(rate_vals), 3) if rate_vals else None,
        "fade_funding_after_fee": pack_pnl(pnl),
        "first_half": pack_pnl(pnl[:half]),
        "second_half": pack_pnl(pnl[half:]),
    }


def pair(a: str, b: str, data: dict) -> dict:
    ca, cb = hour_close(data[a]["bars"]), hour_close(data[b]["bars"])
    fa, fb = {}, {}
    for row in data[a]["funding"]:
        fa[(int(row["timestamp"]) // 3_600_000) * 3_600_000] = float(row["funding_rate"])
    for row in data[b]["funding"]:
        fb[(int(row["timestamp"]) // 3_600_000) * 3_600_000] = float(row["funding_rate"])
    common = sorted(set(fa) & set(fb) & set(ca) & set(cb))
    times_a = sorted(ca)
    times_b = sorted(cb)
    nxt_a = {times_a[i]: times_a[i + 1] for i in range(len(times_a) - 1)}
    nxt_b = {times_b[i]: times_b[i + 1] for i in range(len(times_b) - 1)}
    pnl = []
    flips = 0
    prev = None
    for ts in common:
        if ts not in nxt_a or ts not in nxt_b:
            continue
        if nxt_a[ts] not in ca or nxt_b[ts] not in cb:
            continue
        # short the richer funding, long the cheaper one
        short_a = fa[ts] >= fb[ts]
        side = "short_a" if short_a else "short_b"
        if prev is not None and side != prev:
            flips += 1
        prev = side
        ret_a = (ca[nxt_a[ts]] - ca[ts]) / ca[ts]
        ret_b = (cb[nxt_b[ts]] - cb[ts]) / cb[ts]
        price = (-ret_a + ret_b) if short_a else (ret_a - ret_b)
        carry = (fa[ts] - fb[ts]) if short_a else (fb[ts] - fa[ts])
        pnl.append((carry, price, carry + price))
    # fee only on flips, spread across hours so the total is right
    fee = flips * ROUND_TRIP * 2  # two legs
    total = sum(x[2] for x in pnl) - fee
    half = len(pnl) // 2
    def side_sum(xs):
        return round(sum(xs), 4)
    return {
        "pair": f"{a}/{b}",
        "hours": len(pnl),
        "flips": flips,
        "funding_leg": side_sum(x[0] for x in pnl),
        "price_leg": side_sum(x[1] for x in pnl),
        "total_after_fee": round(total, 4),
        "mean_after_fee": round(total / len(pnl), 6) if pnl else None,
        "first_half_after_fee": round(sum(x[2] for x in pnl[:half]) - fee / 2, 4) if pnl else None,
        "second_half_after_fee": round(sum(x[2] for x in pnl[half:]) - fee / 2, 4) if pnl else None,
    }


def main() -> None:
    data = load_all()
    t0 = min(int(r["timestamp"]) for p in data.values() for r in p["funding"])
    t1 = max(int(r["timestamp"]) for p in data.values() for r in p["funding"])
    payload = {
        "from": datetime.fromtimestamp(t0 / 1000, timezone.utc).strftime("%Y-%m-%d"),
        "to": datetime.fromtimestamp(t1 / 1000, timezone.utc).strftime("%Y-%m-%d"),
        "single": {name: single(name, pack) for name, pack in data.items()},
        "pairs": [
            pair("BTC-USD", "ETH-USD", data),
            pair("ETH-USD", "SOL-USD", data),
            pair("NAS100-USD", "SP500-USD", data),
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(json.dumps({"from": payload["from"], "to": payload["to"], "single": payload["single"], "pairs": payload["pairs"]}, indent=2))


if __name__ == "__main__":
    main()
