"""Mechanical test of the Kane/ICT intraday rules on Polymarket crypto perps.

Trades BTC-USD or ETH-USD perpetuals. The other coin is the SMT counterpart
(BTC plays the role of the higher-volume ES, ETH the role of NQ).

Downtrend: last completed UTC day closed below the midpoint of the prior 10
daily ranges. Short only while price is above that midpoint. The 1h bar must
sweep the prior hour's high and close back below it, during 13:00-16:00 UTC.
SMT version also requires the other coin NOT to have swept its own prior high.
Stop is the sweep high. Target is the 10-day midpoint. If the reward is under
1.5 times the risk, skip. Exit at 21:00 UTC if neither is hit. Same-bar
stop and target counts as a stop. Round-trip taker fee is 0.08% of price.

    python research/kane_perp.py research/kane_perp_2026-10-16.json
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("research/kane_perp.json")
API = "https://api.perpetuals.polymarket.com/v1/info"
FEE = 0.0008  # 0.04% in and 0.04% out, the lowest volume tier


def get(path: str, **params) -> dict | list:
    url = f"{API}/{path}?{urllib.parse.urlencode(params)}"
    with urllib.request.urlopen(url, timeout=30) as resp:
        return json.loads(resp.read())


def load(symbol: str, instrument_id: int) -> list[list]:
    bars: list[list] = []
    start = 0
    while True:
        page = get("klines", instrument_id=instrument_id, interval="1h", start_timestamp=start or 1)
        chunk = page["data"]
        if not chunk:
            break
        bars.extend(chunk)
        if not page.get("more"):
            break
        start = int(chunk[-1][0]) + 1
    # dedupe
    seen = {}
    for bar in bars:
        seen[int(bar[0])] = bar
    out = [seen[k] for k in sorted(seen)]
    print(f"{symbol} {len(out)} hours", flush=True)
    return out


def f(bar, i):
    return float(bar[i])


def daily(bars):
    days = {}
    for bar in bars:
        day = datetime.fromtimestamp(bar[0] / 1000, timezone.utc).strftime("%Y-%m-%d")
        high, low, close = f(bar, 2), f(bar, 3), f(bar, 4)
        row = days.get(day)
        if row is None:
            days[day] = [high, low, close]
        else:
            row[0] = max(row[0], high)
            row[1] = min(row[1], low)
            row[2] = close
    return days


def run(primary, other, primary_name: str, smt: bool, need_4h: bool = True) -> dict:
    other_by_ts = {int(b[0]): b for b in other}
    days = daily(primary)
    day_keys = sorted(days)
    trades = []
    used_days = set()
    for i in range(2, len(primary) - 1):
        bar = primary[i]
        ts = int(bar[0])
        hour = datetime.fromtimestamp(ts / 1000, timezone.utc)
        if hour.hour not in (13, 14, 15):
            continue
        day = hour.strftime("%Y-%m-%d")
        if day in used_days:
            continue
        prev_days = [k for k in day_keys if k < day]
        if len(prev_days) < 10:
            continue
        window = prev_days[-10:]
        hi = max(days[k][0] for k in window)
        lo = min(days[k][1] for k in window)
        mid = (hi + lo) / 2
        if days[prev_days[-1]][2] >= mid:
            continue  # not a downtrend
        prev = primary[i - 1]
        if not (f(bar, 2) > f(prev, 2) and f(bar, 4) < f(prev, 2) and f(bar, 4) < f(bar, 1)):
            continue
        if f(bar, 4) <= mid:
            continue  # not in premium
        # 4h sweep: current 4h block high vs previous block
        block = hour.hour // 4
        cur_hi = prev_hi = None
        for b in primary[max(0, i - 12): i + 1]:
            h = datetime.fromtimestamp(int(b[0]) / 1000, timezone.utc)
            if h.strftime("%Y-%m-%d") != day and h.hour // 4 == block:
                continue
            key = (h.strftime("%Y-%m-%d"), h.hour // 4)
            if key == (day, block):
                cur_hi = f(b, 2) if cur_hi is None else max(cur_hi, f(b, 2))
        # previous 4h block
        prev_block_hour = block * 4 - 4
        prev_day = day
        if prev_block_hour < 0:
            prev_block_hour = 20
            prev_day = prev_days[-1]
        for b in primary[max(0, i - 16): i + 1]:
            h = datetime.fromtimestamp(int(b[0]) / 1000, timezone.utc)
            if h.strftime("%Y-%m-%d") == prev_day and h.hour // 4 == (prev_block_hour // 4):
                prev_hi = f(b, 2) if prev_hi is None else max(prev_hi, f(b, 2))
        if need_4h and (cur_hi is None or prev_hi is None or cur_hi <= prev_hi):
            continue
        peer = other_by_ts.get(ts)
        peer_prev = other_by_ts.get(int(primary[i - 1][0]))
        if smt:
            if peer is None or peer_prev is None or f(peer, 2) > f(peer_prev, 2):
                continue  # other coin also swept, or missing: no divergence
        entry = f(bar, 4)
        stop = f(bar, 2)
        risk = stop - entry
        reward = entry - mid
        if risk <= 0 or reward < 1.5 * risk:
            continue
        exit_px = None
        outcome = "time"
        for nxt in primary[i + 1:]:
            h = datetime.fromtimestamp(int(nxt[0]) / 1000, timezone.utc)
            if h.strftime("%Y-%m-%d") != day or h.hour >= 21:
                exit_px = f(nxt, 1)  # open of the bar we refuse to hold, approx prior close
                # use previous close instead
                break
            if f(nxt, 2) >= stop:
                exit_px = stop
                outcome = "stop"
                break
            if f(nxt, 3) <= mid:
                exit_px = mid
                outcome = "target"
                break
        if exit_px is None:
            continue
        if outcome == "time":
            # flat at the close of the last bar still inside the session
            held = [b for b in primary[i + 1:] if datetime.fromtimestamp(int(b[0]) / 1000, timezone.utc).strftime("%Y-%m-%d") == day and datetime.fromtimestamp(int(b[0]) / 1000, timezone.utc).hour < 21]
            if not held:
                continue
            exit_px = f(held[-1], 4)
        gross = (entry - exit_px) / risk
        fee_r = (entry * FEE) / risk
        trades.append({"day": day, "R": round(gross - fee_r, 3), "outcome": outcome, "risk_px": round(risk, 2)})
        used_days.add(day)
    wins = sum(1 for t in trades if t["R"] > 0)
    return {
        "market": primary_name,
        "smt": smt,
        "four_hour": need_4h,
        "n": len(trades),
        "win_rate": round(wins / len(trades), 3) if trades else None,
        "total_R": round(sum(t["R"] for t in trades), 2),
        "avg_R": round(sum(t["R"] for t in trades) / len(trades), 3) if trades else None,
        "trades": trades,
    }


def main() -> None:
    instruments = {row["symbol"]: row["instrument_id"] for row in get("instruments")}
    btc = load("BTC-USD", instruments["BTC-USD"])
    eth = load("ETH-USD", instruments["ETH-USD"])
    nas = load("NAS100-USD", instruments["NAS100-USD"])
    spx = load("SP500-USD", instruments["SP500-USD"])
    t0 = datetime.fromtimestamp(btc[0][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
    t1 = datetime.fromtimestamp(btc[-1][0] / 1000, timezone.utc).strftime("%Y-%m-%d")
    results = [
        run(btc, eth, "BTC-USD", False, need_4h=False),
        run(btc, eth, "BTC-USD", True, need_4h=False),
        run(btc, eth, "BTC-USD", False),
        run(btc, eth, "BTC-USD", True),
        run(eth, btc, "ETH-USD", False),
        run(eth, btc, "ETH-USD", True),
        run(nas, spx, "NAS100-USD", False),
        run(nas, spx, "NAS100-USD", True),
    ]
    payload = {"from": t0, "to": t1, "btc_hours": len(btc), "nas_hours": len(nas), "fee_round_trip": FEE, "results": results}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    for row in results:
        print({k: row[k] for k in ("market", "smt", "four_hour", "n", "win_rate", "total_R", "avg_R")})


if __name__ == "__main__":
    main()
