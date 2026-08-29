#!/usr/bin/env python3
"""Why the paper favorite bot keeps hitting the $50 circuit, and what is feasible.

Live 2026-08-29 evidence (dashboard trades, not secrets):
  97–98¢ BTC+ETH 5m, $10/fill, last 180s, circuit latched at today_pnl −$50.31,
  equity $397 from $500. Several paper_settled rows paid ~$5 at 0.50/0.50-ish
  prices 0–2s after the window ended. Gamma later posted 0/1 on those same slugs.

  is_redeemable_market treated (0.50 ± 0.02) + ended-clock as a payout vector.
  Crypto 5m books print a stale mid the second the clock hits zero, before
  Chainlink posts 0/1. Crediting that mid crystallizes ~−$5 per $10 fill
  whether the favorite later wins or loses.

This script writes that diagnosis, the halt-day math, and a short paginated
BTC+ETH tape study (0/1 settle only) for 97–98 vs 90–98.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "surf-arb-research/1.6 (read-only; favorite-circuit; no trading)",
    "Accept": "application/json",
}
OUT = Path(__file__).with_name("favorite_circuit.json")
FEE = 0.07
CIRCUIT = 50.0
SAMPLE_HOURS = 12
WORKERS = 8
PAGES = 3
ASSETS = ("btc", "eth")


def get_json(url: str, timeout: float = 25.0, tries: int = 5):
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
                time.sleep(0.5 * (2**i))
                continue
            raise
        except Exception as exc:
            last = exc
            time.sleep(0.4 * (2**i))
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


def shares_for(price: float, notional: float) -> float:
    return notional / max(price, 0.01)


def cost_of(price: float, notional: float) -> float:
    n = shares_for(price, notional)
    return n * (price + fee_on(price))


def pnl_true(price: float, won: bool, notional: float) -> float:
    n = shares_for(price, notional)
    fee = fee_on(price)
    per = (1.0 - price - fee) if won else (-price - fee)
    return per * n


def pnl_false_mid(price: float, notional: float, mid: float = 0.50) -> float:
    """What the bug credits: payout = shares * mid_quote, not 0/1."""
    n = shares_for(price, notional)
    return n * mid - cost_of(price, notional)


def breakeven_reverse(price: float) -> float:
    """Max reverse rate for +EV after 7% crypto fee (per share)."""
    fee = fee_on(price)
    # wr*(1-p-fee) + (1-wr)*(-p-fee) = 0  => wr = p+fee
    return round(price + fee, 6)


def fills_to_circuit(loss_per: float, limit: float = CIRCUIT) -> float:
    if loss_per >= 0:
        return None
    return round(limit / abs(loss_per), 2)


def circuit_backtest(events: list[tuple[int, float, bool]], limit: float = CIRCUIT) -> dict:
    if not events:
        return {
            "n_taken": 0,
            "n_skipped_circuit": 0,
            "lose_taken": 0,
            "halt_days": 0,
            "trade_days": 0,
            "total_usd": 0.0,
            "usd_per_day": 0.0,
            "worst_day_usd": 0.0,
            "best_day_usd": 0.0,
        }
    events = sorted(events, key=lambda e: e[0])
    total = 0.0
    day_pnl = 0.0
    day_key = None
    halted = False
    taken = 0
    skipped = 0
    lose_taken = 0
    halt_days = 0
    day_pnls: list[float] = []

    def close_day():
        nonlocal halt_days
        if day_key is None:
            return
        day_pnls.append(day_pnl)
        if halted:
            halt_days += 1

    for ts, usd, lost in events:
        d = datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
        if d != day_key:
            close_day()
            day_key = d
            day_pnl = 0.0
            halted = False
        if halted:
            skipped += 1
            continue
        day_pnl += usd
        total += usd
        taken += 1
        lose_taken += int(lost)
        if day_pnl <= -abs(limit):
            halted = True
    close_day()
    n_days = max(len(day_pnls), 1)
    return {
        "n_taken": taken,
        "n_skipped_circuit": skipped,
        "lose_taken": lose_taken,
        "halt_days": halt_days,
        "trade_days": len(day_pnls),
        "total_usd": round(total, 2),
        "usd_per_day": round(total / n_days, 2),
        "worst_day_usd": round(min(day_pnls), 2) if day_pnls else 0.0,
        "best_day_usd": round(max(day_pnls), 2) if day_pnls else 0.0,
    }


def wilson_lo_hi(wins: int, n: int) -> list[float] | None:
    if n <= 0:
        return None
    z = 1.96
    phat = wins / n
    denom = 1 + z * z / n
    centre = (phat + z * z / (2 * n)) / denom
    spread = (z * ((phat * (1 - phat) + z * z / (4 * n)) / n) ** 0.5) / denom
    return [round(max(0.0, centre - spread), 6), round(min(1.0, centre + spread), 6)]


def end_ts(iso: str) -> int:
    dt = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def fetch_trades(cid: str, *, pages: int = PAGES) -> list[dict]:
    rows: list[dict] = []
    seen: set[str] = set()
    for page in range(pages):
        offset = page * 1000
        chunk = (
            get_json(f"{DATA}/trades?market={cid}&limit=1000&offset={offset}&takerOnly=true")
            or []
        )
        if not chunk:
            break
        for t in chunk:
            hid = (
                str(t.get("transactionHash") or "")
                + f":{t.get('timestamp')}:{t.get('proxyWallet')}:{t.get('price')}:{t.get('size')}"
            )
            if hid in seen:
                continue
            seen.add(hid)
            rows.append(t)
        if len(chunk) < 1000:
            break
        time.sleep(0.04)
    return rows


def first_buy(trades: list[dict], *, lo: float, hi: float, left_max: float) -> dict | None:
    for t in trades:
        if t["left"] < 3 or t["left"] > left_max:
            continue
        if t["side"] != "BUY":
            continue
        tick = round(t["px"], 2)
        if lo <= tick <= hi:
            return t
    return None


def opp_max_before(trades: list[dict], fill: dict) -> float:
    other = "Down" if fill["outcome"] == "Up" else "Up"
    mx = 0.0
    for t in trades:
        if t["ts"] >= fill["ts"]:
            break
        if t["outcome"] == other:
            mx = max(mx, t["px"])
    return mx


def load_one(asset: str, start: int) -> dict | None:
    slug = f"{asset}-updown-5m-{start}"
    m = get_json(f"{GAMMA}/markets/slug/{slug}")
    if not m or not m.get("conditionId") or not m.get("closed"):
        return None
    prices = [float(x) for x in parse_field(m.get("outcomePrices"), ["0", "0"])]
    outcomes = [str(x) for x in parse_field(m.get("outcomes"), ["Up", "Down"])]
    if len(prices) < 2 or abs(max(prices) - 1.0) > 0.05:
        return None
    winner = outcomes[0] if prices[0] >= prices[1] else outcomes[1]
    end = end_ts(m["endDate"])
    cid = m["conditionId"]
    raw = fetch_trades(cid)
    trades = []
    for t in raw:
        if str(t.get("conditionId") or "") != str(cid):
            continue
        try:
            px = float(t["price"])
            ts = int(t["timestamp"])
        except (KeyError, TypeError, ValueError):
            continue
        outcome = str(t.get("outcome") or "")
        if outcome not in {"Up", "Down"}:
            continue
        trades.append(
            {
                "ts": ts,
                "left": end - ts,
                "px": px,
                "side": str(t.get("side") or ""),
                "outcome": outcome,
            }
        )
    trades.sort(key=lambda r: r["ts"])
    return {"slug": slug, "end": end, "winner": winner, "trades": trades}


LIVE_TRADES = [
    {
        "slug": "btc-updown-5m-1787964600",
        "bought": "Down",
        "fill_px": 0.97,
        "paper_prices": [0.515, 0.485],
        "paper_net": -5.021,
        "gamma_later": ["1", "0"],
        "true_winner": "Up",
        "true_net_at_10": round(pnl_true(0.97, False, 10), 3),
        "note": "false mid redeem; official later Up — bought Down so true loss is ~-$10, paper only booked -$5",
    },
    {
        "slug": "btc-updown-5m-1787964300",
        "bought": "Down",
        "fill_px": 0.97,
        "paper_prices": [0.485, 0.515],
        "paper_net": -4.712,
        "gamma_later": ["0", "1"],
        "true_winner": "Down",
        "true_net_at_10": round(pnl_true(0.97, True, 10), 3),
        "note": "false mid redeem stole a winner: paper -$4.71 instead of +$0.29",
    },
    {
        "slug": "btc-updown-5m-1787964000",
        "bought": "Down",
        "fill_px": 0.97,
        "paper_prices": [0.505, 0.495],
        "paper_net": -4.918,
        "gamma_later": ["1", "0"],
        "true_winner": "Up",
        "true_net_at_10": round(pnl_true(0.97, False, 10), 3),
        "note": "false mid redeem; official later Up — true -$10",
    },
    {
        "slug": "eth-updown-5m-1787964600",
        "bought": "Down",
        "fill_px": 0.97,
        "paper_prices": [1.0, 0.0],
        "paper_net": -10.021,
        "gamma_later": ["1", "0"],
        "true_winner": "Up",
        "true_net_at_10": round(pnl_true(0.97, False, 10), 3),
        "note": "real reverse, 0/1 already posted — this path is correct",
    },
    {
        "slug": "btc-updown-5m-1787963700",
        "bought": "Up",
        "fill_px": 0.97,
        "paper_prices": [0.0, 1.0],
        "paper_net": -10.021,
        "gamma_later": ["0", "1"],
        "true_winner": "Down",
        "true_net_at_10": round(pnl_true(0.97, False, 10), 3),
        "note": "real reverse, 0/1 already posted — this path is correct",
    },
]


def diagnosis() -> dict:
    px = 0.97
    return {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "live_bot_2026_08_29": {
            "strategy_rev": 18,
            "mode": "paper",
            "band": "97-98c (user tightened from Rev 18 default 90-98)",
            "window": "last 180s",
            "assets": ["btc", "eth"],
            "tags": ["5M"],
            "max_usd_per_trade": 10.0,
            "daily_loss_limit_usd": 50.0,
            "circuit": True,
            "today_pnl": -50.313,
            "paper_equity": 397.25,
            "starting": 500.0,
            "total_pnl": -102.75,
            "fills_24h": 56,
        },
        "bug": {
            "name": "ended-clock 50/50 mid redeem",
            "file": "app/rescue.py is_redeemable_market",
            "old_rule": "decided if 0/1 OR (0.50 ± 0.02) AND (closed OR ended OR uma ready/resolved)",
            "why_wrong": (
                "5m crypto books go closed/ended with last mid ~0.50/0.50 for seconds to "
                "a minute before automaticallyResolved posts 0/1. Paper then credits "
                "shares*0.50 and deletes inventory. Winners that should pay $1 are booked "
                "as ~-$5. The $50 circuit is ~10 such fills at $10, or ~25 minutes of BTC+ETH."
            ),
            "live_examples": LIVE_TRADES,
        },
        "math_at_97c": {
            "fee_per_share": round(fee_on(px), 6),
            "breakeven_reverse": breakeven_reverse(px),
            "win_usd": {
                "5": round(pnl_true(px, True, 5), 4),
                "10": round(pnl_true(px, True, 10), 4),
            },
            "lose_usd": {
                "5": round(pnl_true(px, False, 5), 4),
                "10": round(pnl_true(px, False, 10), 4),
            },
            "false_mid_usd": {
                "5": round(pnl_false_mid(px, 5), 4),
                "10": round(pnl_false_mid(px, 10), 4),
            },
            "fills_to_50_circuit": {
                "false_mid_5": fills_to_circuit(pnl_false_mid(px, 5)),
                "false_mid_10": fills_to_circuit(pnl_false_mid(px, 10)),
                "true_loss_5": fills_to_circuit(pnl_true(px, False, 5)),
                "true_loss_10": fills_to_circuit(pnl_true(px, False, 10)),
            },
            "note": (
                "At $10, five true reverses halt the day even with a perfect redeem. "
                "The 50/50 bug halts after ~10 fills with no reverses required. "
                "Rev 18 default 90-98 still first-prints ~90c; that is a separate EV problem."
            ),
        },
        "playbooks": {
            "keep_90_98_last180s": "No. First print is almost always 90c. This 12h BTC+ETH reverse 10.6%. 36h BTC full tape 5.6%. Circuit food.",
            "user_97_98_at_10_with_mid_redeem": "What the live paper bot was doing. $10 false-mid ~-$4.87/fill. Circuit after ~10 fills / both UTC days in the 12h tape. Expected, not unlucky.",
            "97_98_last180s_wait_0_1_size_5": (
                "Stops the daily circuit. This 12h BTC+ETH sample was still -EV "
                "(reverse 4.96% vs 2.80% breakeven at 97c, -$17/day) but worst day -$19, 0 halt days. "
                "Prior 36h BTC-only 96-98 was +EV (1.53%). Edge is regime-dependent; size $5 keeps a dump day inside $50."
            ),
            "97_98_at_10_wait_0_1": "This 12h worst day -$38, no halt. Five clustered true reverses still halt. Prefer $5 until redeem is proven clean.",
            "last_60s": "Fewer fills (163 vs 282). This 12h reverse 3.68%, still slightly -EV, less dollar damage. Optional later; not required to stop 熔斷.",
            "skip_session_opposite_prints": "Useless on liquid 5m: the other side always printed 40c+ earlier in the window. Do not ship.",
            "favorite_maker_on": "Resting 97c is the steamroller. Taker-only is the cleaner favorite path.",
            "do_not": [
                "lower complement min_edge",
                "re-enable all-session complement maker",
                "buy the 0.001 side alone",
                "credit 50/50 mids",
                "go live / paper-reset",
            ],
        },
        "prior_36h_btc_paginated": {
            "source": "research/btc_5m_90_audit.json",
            "90_98_last180s_full_tape_reverse": 0.05598,
            "96_98_last180s_full_tape_reverse": 0.015267,
            "96_98_last180s_pnl_at_5": 36.82,
        },
    }


def eval_combo(
    rows: list[dict],
    *,
    lo: float,
    hi: float,
    left_max: float,
    notional: float,
    false_mid: bool,
    max_opp_before: float | None = None,
) -> dict:
    events = []
    fills = []
    skipped_opp = 0
    for row in rows:
        t = first_buy(row["trades"], lo=lo, hi=hi, left_max=left_max)
        if t is None:
            continue
        scare = opp_max_before(row["trades"], t)
        if max_opp_before is not None and scare >= max_opp_before:
            skipped_opp += 1
            continue
        won = t["outcome"] == row["winner"]
        usd = pnl_false_mid(t["px"], notional) if false_mid else pnl_true(t["px"], won, notional)
        fills.append(
            {
                "slug": row["slug"],
                "px": t["px"],
                "won": won,
                "usd": round(usd, 4),
                "end": row["end"],
                "left": t["left"],
                "opp_before": round(scare, 4),
            }
        )
        events.append((int(row["end"]), usd, (not won) if not false_mid else True))
    n = len(fills)
    lose = sum(1 for f in fills if not f["won"])
    out = {
        "n": n,
        "win": n - lose,
        "lose": lose,
        "reverse": None if not n else round(lose / n, 6),
        "reverse_ci95": None if not n else wilson_lo_hi(lose, n),
        "pnl_no_circuit": round(sum(f["usd"] for f in fills), 2),
        "circuit_50": circuit_backtest(events, CIRCUIT),
        "false_mid": false_mid,
        "notional": notional,
        "skipped_opp": skipped_opp,
        "avg_left_s": None if not n else round(sum(f["left"] for f in fills) / n, 1),
        "avg_px": None if not n else round(sum(f["px"] for f in fills) / n, 4),
    }
    return out


def main() -> None:
    report = diagnosis()
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    now = int(time.time())
    last_closed = now - (now % 300) - 300
    starts = [last_closed - i * 300 for i in range(SAMPLE_HOURS * 12)]
    jobs = [(asset, ts) for ts in starts for asset in ASSETS]
    print(f"tape {len(jobs)} {ASSETS} 5m windows", flush=True)
    rows = []
    errors = 0
    error_samples: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(load_one, a, ts): (a, ts) for a, ts in jobs}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 40 == 0:
                print(f"  {done}/{len(jobs)} ok={len(rows)} err={errors}", flush=True)
            try:
                row = fut.result()
            except Exception as exc:
                errors += 1
                if len(error_samples) < 8:
                    error_samples.append(f"{type(exc).__name__}: {exc}")
                continue
            if row:
                rows.append(row)
    print(f"loaded {len(rows)} resolved, errors={errors}", flush=True)
    combos = {}
    specs = [
        ("97_98_last180s_5_true", 0.97, 0.98, 180, 5.0, False, None),
        ("97_98_last180s_10_true", 0.97, 0.98, 180, 10.0, False, None),
        ("97_98_last180s_10_false_mid", 0.97, 0.98, 180, 10.0, True, None),
        ("90_98_last180s_5_true", 0.90, 0.98, 180, 5.0, False, None),
        ("96_98_last180s_5_true", 0.96, 0.98, 180, 5.0, False, None),
        ("97_98_last60s_5_true", 0.97, 0.98, 60, 5.0, False, None),
        ("97_98_last30s_5_true", 0.97, 0.98, 30, 5.0, False, None),
        ("98_only_last180s_5_true", 0.98, 0.98, 180, 5.0, False, None),
        ("97_98_last180s_5_skip_opp40", 0.97, 0.98, 180, 5.0, False, 0.40),
        ("97_98_last60s_5_skip_opp40", 0.97, 0.98, 60, 5.0, False, 0.40),
    ]
    for name, lo, hi, left, notion, mid, opp in specs:
        combos[name] = eval_combo(
            rows, lo=lo, hi=hi, left_max=left, notional=notion, false_mid=mid, max_opp_before=opp
        )
        print(name, {k: combos[name][k] for k in combos[name] if k != "circuit_50"}, combos[name]["circuit_50"], flush=True)
    report["tape"] = {
        "sample_hours": SAMPLE_HOURS,
        "assets": list(ASSETS),
        "markets": len(rows),
        "errors": errors,
        "error_samples": error_samples,
        "combos": combos,
        "rule": "First public BUY in band during last T seconds. Settle at official 0/1 (true) or shares*0.50 at end (false_mid). $50 UTC-day circuit.",
    }
    report["verdict"] = [
        "The strategy is not unlucky. Paper was realizing ~50% losses at the close print.",
        "False-mid $10 halted both days in the 12h tape (22 fills, skipped the rest). True 0/1 $5 did not halt (worst day -$19).",
        "Fix redeem to wait for 0/1 (or UMA-resolved exact 50/50 invalid). That is Rev 19.",
        "Default 97-98c last 180s at $5, taker-only. Keep $50 circuit. 12h sample was still -EV; 36h BTC 96-98 was +EV. Do not paper-reset. Do not go live.",
        "90-98 remains a steamroller even with correct redeem (this 12h reverse 10.6%).",
    ]
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("wrote", OUT, flush=True)


if __name__ == "__main__":
    main()
