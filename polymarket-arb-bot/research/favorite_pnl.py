"""Does a high win rate make high-risk Polymarket bets profitable?

Two measurements on markets that already resolved:

1. Favorite. Buy the side priced above 50c at a chosen time before the end.
   Win rate is high by construction. PnL is positive only when that win rate
   beats the price plus the taker fee.

2. Crypto 5-minute Up/Down, with a public spot path as a filter. These markets
   settle on the Chainlink 60-second TWAP of the window versus the price at
   the open. One minute before the end, four of the five minutes are finished,
   so the required last-minute price is knowable from a spot proxy. Coinbase
   1-minute candles stand in for Chainlink. The trade buys the signaled side
   at the Polymarket price then, and pays the market's taker fee.

The Polymarket series is a bucket price, not the ask, so taker results are
optimistic by about half the spread. No orders are sent.

    python research/favorite_pnl.py research/favorite_pnl_2026-10-16.json
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyarb.fees import fee_per_share  # noqa: E402
from polyarb.models import FeeSchedule  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "research" / "favorite_pnl.json"
DAYS = 3
ASSETS = {"btc": "BTC-USD", "eth": "ETH-USD"}
FEE_FALLBACK = FeeSchedule(rate=0.07, exponent=1.0, taker_only=True, rebate_rate=0.2)


def fee_of(raw: dict) -> FeeSchedule:
    sched = raw.get("feeSchedule") or {}
    try:
        rate = float(sched.get("rate") or 0)
    except (TypeError, ValueError):
        rate = 0.0
    if rate <= 0:
        return FEE_FALLBACK
    return FeeSchedule(rate=rate, exponent=float(sched.get("exponent") or 1) or 1.0, taker_only=True)


def parse_prices(raw: dict) -> list[float]:
    value = raw.get("outcomePrices")
    if isinstance(value, str):
        value = json.loads(value)
    return [float(x) for x in value]


def up_won(raw: dict) -> bool | None:
    prices = parse_prices(raw)
    if len(prices) < 2:
        return None
    if prices[0] >= 0.99 and prices[1] <= 0.01:
        return True
    if prices[1] >= 0.99 and prices[0] <= 0.01:
        return False
    return None


def bucket(price: float) -> str:
    edges = (0.5, 0.6, 0.7, 0.8, 0.9, 0.95, 0.98, 0.995, 1.01)
    labels = ("50-60c", "60-70c", "70-80c", "80-90c", "90-95c", "95-98c", "98-99.5c", "99.5c+")
    for lo, hi, label in zip(edges, edges[1:], labels):
        if lo <= price < hi:
            return label
    return "other"


class Acc:
    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.pnl = 0.0
        self.price = 0.0
        self.all_in = 0.0

    def add(self, price: float, won: bool, pnl: float, all_in: float) -> None:
        self.n += 1
        self.wins += int(won)
        self.pnl += pnl
        self.price += price
        self.all_in += all_in

    def row(self) -> dict:
        return {
            "n": self.n,
            "win_rate": round(self.wins / self.n, 4) if self.n else None,
            "avg_price": round(self.price / self.n, 4) if self.n else None,
            "avg_cost_with_fee": round(self.all_in / self.n, 4) if self.n else None,
            "pnl_per_share": round(self.pnl / self.n, 5) if self.n else None,
            "pnl_total_1share": round(self.pnl, 2),
        }


def trade_pnl(price: float, won: bool, fee: FeeSchedule) -> tuple[float, float]:
    cost = price + fee_per_share(fee, price)
    return ((1.0 - cost) if won else -cost), cost


async def get_json(client: httpx.AsyncClient, url: str, params: dict, tries: int = 5) -> dict | list:
    delay = 0.4
    for attempt in range(tries):
        resp = await client.get(url, params=params)
        if resp.status_code == 429 or resp.status_code >= 500:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 8)
            continue
        resp.raise_for_status()
        return resp.json()
    resp.raise_for_status()
    return {}


async def load_markets(client: httpx.AsyncClient, start: datetime, end: datetime) -> list[dict]:
    found: list[dict] = []
    cursor = end
    seen: set[str] = set()
    while cursor > start and len(found) < 20000:
        page = await get_json(client, "https://gamma-api.polymarket.com/markets", {
            "closed": "true",
            "limit": 100,
            "end_date_min": start.isoformat(),
            "end_date_max": cursor.isoformat(),
            "order": "endDate",
            "ascending": "false",
        })
        if not isinstance(page, list) or not page:
            break
        oldest = cursor
        fresh = 0
        for raw in page:
            key = str(raw.get("conditionId") or raw.get("id"))
            if key in seen:
                continue
            seen.add(key)
            found.append(raw)
            fresh += 1
            try:
                ended = datetime.fromisoformat(str(raw.get("endDate")).replace("Z", "+00:00"))
            except ValueError:
                continue
            if ended < oldest:
                oldest = ended
        if fresh == 0 or oldest >= cursor:
            break
        cursor = oldest - timedelta(seconds=1)
    return found


async def load_candles(client: httpx.AsyncClient, product: str, start: int, end: int) -> dict[int, tuple[float, float]]:
    """Coinbase 1-minute candles: start-ts -> (open, close)."""
    out: dict[int, tuple[float, float]] = {}
    cursor = start
    while cursor < end:
        chunk_end = min(cursor + 300 * 60, end)
        rows = await get_json(client, f"https://api.exchange.coinbase.com/products/{product}/candles", {
            "granularity": 60,
            "start": datetime.fromtimestamp(cursor, timezone.utc).isoformat(),
            "end": datetime.fromtimestamp(chunk_end, timezone.utc).isoformat(),
        })
        if isinstance(rows, list):
            for row in rows:
                # [time, low, high, open, close, volume]
                out[int(row[0])] = (float(row[3]), float(row[4]))
        cursor = chunk_end
    return out


async def price_before(
    client: httpx.AsyncClient, token: str, end_ts: int, seconds_before: int, lookback: int = 20 * 60
) -> float | None:
    body = await get_json(client, "https://data-api.polymarket.com/v2/prices-history", {
        "token_id": token,
        "start": end_ts - lookback,
        "end": end_ts + 120,
        "bucket_seconds": 60 if lookback <= 3600 else 300,
    })
    rows = body.get("data") if isinstance(body, dict) else None
    if not rows:
        return None
    cutoff = end_ts - seconds_before
    chosen = None
    for row in rows:
        if row.get("resolution_seconds") == 0:
            continue
        ts = int(row["timestamp"])
        if ts <= cutoff:
            chosen = float(row["price"])
    if chosen is None or not 0.005 < chosen < 0.995:
        return None
    return chosen


def projected_up(candles: dict[int, tuple[float, float]], start_ts: int) -> tuple[float, float] | None:
    """Return (projected_twap / open - 1 in bps, open) using four finished minutes and a flat last minute."""
    bars = []
    for i in range(4):
        bar = candles.get(start_ts + i * 60)
        if bar is None:
            return None
        bars.append(bar[1])  # close
    open_px = candles[start_ts][0]
    if open_px <= 0:
        return None
    projected = (sum(bars) + bars[-1]) / 5.0
    return (projected / open_px - 1.0) * 1e4, open_px


async def main() -> None:
    end = datetime.now(timezone.utc) - timedelta(minutes=20)
    start = end - timedelta(days=DAYS)
    t0 = time.time()
    async with httpx.AsyncClient(timeout=30, headers={"User-Agent": "polyarb-research"}) as client:
        markets = await load_markets(client, start, end)
        print(f"closed markets {len(markets)} in {time.time() - t0:.0f}s", flush=True)
        candles = {}
        for asset, product in ASSETS.items():
            candles[asset] = await load_candles(client, product, int(start.timestamp()) - 120, int(end.timestamp()) + 120)
            print(f"candles {asset} {len(candles[asset])}", flush=True)

        crypto = []
        slow = []
        for raw in markets:
            slug = str(raw.get("slug") or "")
            question = str(raw.get("question") or "")
            if "updown-5m" in slug and slug.split("-")[0] in ASSETS:
                crypto.append(raw)
            elif "Up or Down" not in question and float(raw.get("volumeNum") or 0) >= 5000 and up_won(raw) is not None:
                # up_won works for Yes/No too when prices are 0/1 on the first outcome.
                slow.append(raw)
        # Slow markets: first outcome is not always "Up". Keep a separate flag via outcome name.
        crypto = [m for m in crypto if str(m.get("slug") or "").split("-")[0] in ("btc", "eth")]
        slow = slow[:120]
        print(f"crypto 5m {len(crypto)} slow {len(slow)}", flush=True)

        sem = asyncio.Semaphore(8)

        async def one_crypto(raw: dict) -> dict | None:
            asset = str(raw.get("slug") or "").split("-")[0]
            won = up_won(raw)
            if won is None or asset not in candles:
                return None
            end_ts = int(datetime.fromisoformat(str(raw["endDate"]).replace("Z", "+00:00")).timestamp())
            start_ts = end_ts - 300
            signal = projected_up(candles[asset], start_ts)
            if signal is None:
                return None
            tokens = raw.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            async with sem:
                px = await price_before(client, tokens[0], end_ts, 60)
            if px is None:
                return None
            return {"asset": asset, "bps": signal[0], "up": px, "won_up": won, "fee": fee_of(raw), "question": raw.get("question")}

        async def one_slow(raw: dict) -> dict | None:
            prices = parse_prices(raw)
            if len(prices) < 2 or max(prices) < 0.99:
                return None
            first_won = prices[0] >= 0.99
            end_ts = int(datetime.fromisoformat(str(raw["endDate"]).replace("Z", "+00:00")).timestamp())
            tokens = raw.get("clobTokenIds")
            if isinstance(tokens, str):
                tokens = json.loads(tokens)
            async with sem:
                px = await price_before(client, tokens[0], end_ts, 3600, lookback=6 * 3600)
            if px is None:
                return None
            return {"name": raw.get("question"), "first": px, "first_won": first_won, "fee": fee_of(raw)}

        crypto_rows = [r for r in await asyncio.gather(*(one_crypto(m) for m in crypto)) if r]
        slow_rows = [r for r in await asyncio.gather(*(one_slow(m) for m in slow)) if r]
    print(f"priced crypto {len(crypto_rows)} slow {len(slow_rows)} in {time.time() - t0:.0f}s", flush=True)

    favorite: dict[str, Acc] = defaultdict(Acc)
    by_asset: dict[str, Acc] = defaultdict(Acc)
    for row in crypto_rows:
        price = row["up"] if row["up"] >= 0.5 else 1.0 - row["up"]
        won = row["won_up"] if row["up"] >= 0.5 else not row["won_up"]
        pnl, all_in = trade_pnl(price, won, row["fee"])
        favorite[bucket(price)].add(price, won, pnl, all_in)
        favorite["all"].add(price, won, pnl, all_in)
        by_asset[row["asset"]].add(price, won, pnl, all_in)

    signal_stats = {}
    for buf in (0, 2, 5, 10):
        groups = {"all": Acc(), "price_below_90c": Acc(), "price_below_80c": Acc()}
        for row in crypto_rows:
            bps = row["bps"]
            if abs(bps) < buf:
                continue
            buy_up = bps > 0
            price = row["up"] if buy_up else 1.0 - row["up"]
            won = row["won_up"] if buy_up else not row["won_up"]
            pnl, all_in = trade_pnl(price, won, row["fee"])
            groups["all"].add(price, won, pnl, all_in)
            if price < 0.90:
                groups["price_below_90c"].add(price, won, pnl, all_in)
            if price < 0.80:
                groups["price_below_80c"].add(price, won, pnl, all_in)
        signal_stats[f"buffer_{buf}bp"] = {k: v.row() for k, v in groups.items()}

    slow_acc: dict[str, Acc] = defaultdict(Acc)
    for row in slow_rows:
        price = row["first"] if row["first"] >= 0.5 else 1.0 - row["first"]
        won = row["first_won"] if row["first"] >= 0.5 else not row["first_won"]
        pnl, all_in = trade_pnl(price, won, row["fee"])
        slow_acc[bucket(price)].add(price, won, pnl, all_in)

    # Fee-free twin of the 5bp signal, so the report can separate forecasting from fees.
    gross = Acc()
    for row in crypto_rows:
        if abs(row["bps"]) < 5:
            continue
        buy_up = row["bps"] > 0
        price = row["up"] if buy_up else 1.0 - row["up"]
        won = row["won_up"] if buy_up else not row["won_up"]
        gross.add(price, won, (1.0 - price) if won else -price, price)

    payload = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "window_days": DAYS,
        "closed_markets_seen": len(markets),
        "crypto_5m_priced": len(crypto_rows),
        "slow_markets_priced": len(slow_rows),
        "note": "Entry is the Polymarket bucket price one minute (crypto) or one hour (other) before the end, not the ask. Spot is Coinbase, settlement is Chainlink TWAP.",
        "crypto_favorite_by_price": {k: v.row() for k, v in favorite.items()},
        "crypto_favorite_by_asset": {k: v.row() for k, v in by_asset.items()},
        "crypto_twap_signal": signal_stats,
        "crypto_twap_5bp_before_fees": gross.row(),
        "other_markets_favorite_1h_before_end": {k: v.row() for k, v in slow_acc.items()},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
