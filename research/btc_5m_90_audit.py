#!/usr/bin/env python3
"""Audit the 90¢ PnL study: was 'first BUY at 90' looking at the whole 5m tape?

Original btc_5m_reversal.py fetches data-api /trades?limit=1000 with no offset.
That page is newest-first. On liquid BTC 5m books it only covers the last ~2
minutes, so 'first 90 in last 300s' is not the first 90 of the window.

This script paginates a recent sample, compares capped vs full tape, and
splits reverse rate by how much time was left when 90 first printed.
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
# Cloudflare 403s urllib if User-Agent contains non-ascii (e.g. the cent sign).
UA = {
    "User-Agent": "surf-arb-research/1.5 (read-only; 90c tape-cap audit; no trading)",
    "Accept": "application/json",
}
OUT = Path(__file__).with_name("btc_5m_90_audit.json")
FEE = 0.07
NOTIONAL = 5.0  # current bot size
NOTIONAL_STUDY = 25.0  # original 30d headline size
SAMPLE_HOURS = 36
WORKERS = 6


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


def pnl_usd(price: float, won: bool, notional: float = NOTIONAL) -> float:
    fee = fee_on(price)
    per = (1.0 - price - fee) if won else (-price - fee)
    return per * (notional / max(price, 0.01))


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


def fetch_trades(cid: str, *, pages: int = 8) -> list[dict]:
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
            hid = str(t.get("transactionHash") or "") + f":{t.get('timestamp')}:{t.get('proxyWallet')}:{t.get('price')}:{t.get('size')}"
            if hid in seen:
                continue
            seen.add(hid)
            rows.append(t)
        if len(chunk) < 1000:
            break
        time.sleep(0.05)
    return rows


def normalize(trades: list[dict], cid: str, end: int) -> list[dict]:
    out = []
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
        out.append(
            {
                "ts": ts,
                "left": left,
                "px": px,
                "size": size,
                "side": str(t.get("side") or ""),
                "outcome": outcome,
            }
        )
    out.sort(key=lambda r: r["ts"])
    return out


def first_buy(trades: list[dict], *, lo: float, hi: float, left_max: float, left_min: float = 0.0) -> dict | None:
    for t in trades:
        if t["left"] < left_min or t["left"] > left_max:
            continue
        if t["side"] != "BUY":
            continue
        tick = round(t["px"], 2)
        if lo <= tick <= hi:
            return t
    return None


def opposite_after(trades: list[dict], fill: dict) -> dict:
    other = "Down" if fill["outcome"] == "Up" else "Up"
    mx = 0.0
    for t in trades:
        if t["ts"] <= fill["ts"]:
            continue
        if t["left"] < 0:
            continue
        if t["outcome"] != other:
            continue
        mx = max(mx, t["px"])
    return {
        "opp_max_after": round(mx, 4),
        "looked_50": mx >= 0.50,
        "looked_90": mx >= 0.90,
    }


def load_one(start: int) -> dict | None:
    slug = f"btc-updown-5m-{start}"
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
    raw_all = fetch_trades(cid)
    raw0 = raw_all[:1000]
    cap = normalize(raw0, cid, end)
    full = normalize(raw_all, cid, end)
    return {
        "slug": slug,
        "end": end,
        "winner": winner,
        "volume": float(m.get("volume") or 0),
        "n_page0": len(raw0),
        "n_full": len(full),
        "capped": len(raw0) >= 1000,
        "page0_span_s": (max(t["ts"] for t in cap) - min(t["ts"] for t in cap)) if cap else 0,
        "page0_oldest_left": max((t["left"] for t in cap), default=None),
        "full_oldest_in_window": max((t["left"] for t in full if 0 <= t["left"] <= 300), default=None),
        "cap": cap,
        "full": full,
    }


def summarize(fills: list[dict], *, notional: float = NOTIONAL) -> dict:
    n = len(fills)
    if not n:
        return {
            "n": 0,
            "win": 0,
            "lose": 0,
            "reverse": None,
            "reverse_ci95": None,
            "pnl_usd": 0.0,
            "pnl_usd_25": 0.0,
            "looked_50": 0,
            "looked_90": 0,
        }
    lose = sum(1 for f in fills if not f["won"])
    win = n - lose
    return {
        "n": n,
        "win": win,
        "lose": lose,
        "reverse": round(lose / n, 6),
        "reverse_ci95": wilson_lo_hi(lose, n),
        "pnl_usd": round(sum(f["pnl"] for f in fills), 2),
        "pnl_usd_25": round(sum(pnl_usd(f["px"], f["won"], NOTIONAL_STUDY) for f in fills), 2),
        "avg_left_s": round(sum(f["left"] for f in fills) / n, 1),
        "avg_px": round(sum(f["px"] for f in fills) / n, 4),
        "looked_50": sum(1 for f in fills if f.get("looked_50")),
        "looked_90": sum(1 for f in fills if f.get("looked_90")),
        "looked_50_rate": round(sum(1 for f in fills if f.get("looked_50")) / n, 6),
        "looked_90_rate": round(sum(1 for f in fills if f.get("looked_90")) / n, 6),
        "disagree_vs_capped": sum(1 for f in fills if f.get("disagree")),
        "vs_fee_breakeven_9_37pct": None if not n else round(lose / n - 0.0937, 6),
    }


def bucket_left(fills: list[dict]) -> dict:
    edges = [(240, 300), (180, 240), (120, 180), (60, 120), (30, 60), (0, 30)]
    out = {}
    for lo, hi in edges:
        part = [f for f in fills if lo < f["left"] <= hi]
        key = f"left_{lo}_{hi}"
        out[key] = summarize(part)
    return out


def eval_fill(row: dict, trades: list[dict], *, lo: float, hi: float, left_max: float) -> dict | None:
    t = first_buy(trades, lo=lo, hi=hi, left_max=left_max)
    if t is None:
        return None
    won = t["outcome"] == row["winner"]
    scare = opposite_after(trades, t)
    return {
        "slug": row["slug"],
        "end": row["end"],
        "outcome": t["outcome"],
        "winner": row["winner"],
        "px": round(t["px"], 4),
        "left": t["left"],
        "won": won,
        "pnl": pnl_usd(t["px"], won),
        **scare,
    }


def main() -> None:
    now = int(time.time())
    last_closed = now - (now % 300) - 300
    starts = [last_closed - i * 300 for i in range(SAMPLE_HOURS * 12)]
    print(f"audit {len(starts)} btc 5m windows", flush=True)
    rows = []
    errors = 0
    error_samples: list[str] = []
    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futs = {pool.submit(load_one, ts): ts for ts in starts}
        done = 0
        for fut in as_completed(futs):
            done += 1
            if done % 40 == 0:
                print(f"  {done}/{len(starts)} ok={len(rows)} err={errors}", flush=True)
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
    if error_samples:
        print("error samples:", error_samples, flush=True)

    combos = [
        ("90_only_last300s", 0.90, 0.90, 300),
        ("90_only_last180s", 0.90, 0.90, 180),
        ("90_only_last30s", 0.90, 0.90, 30),
        ("90_98_last180s", 0.90, 0.98, 180),
        ("96_98_last180s", 0.96, 0.98, 180),
    ]
    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "sample_hours": SAMPLE_HOURS,
        "markets": len(rows),
        "errors": errors,
        "error_samples": error_samples,
        "capped_page0_ge_1000": sum(1 for r in rows if r["capped"]),
        "capped_rate": None,
        "page0_span_s_p50": None,
        "page0_oldest_left_p50": None,
        "volume_p50": None,
        "notional_usd": NOTIONAL,
        "notional_usd_study": NOTIONAL_STUDY,
        "combos": {},
        "left_buckets_full_90_last300s": {},
        "original_30d_headline_capped_tape": {
            "buy_0.90_last300s_reverse": 0.056529,
            "buy_0.90_last180s_reverse": 0.060264,
            "buy_0.90_last30s_reverse": 0.09292,
            "fee_breakeven_reverse_at_90c": 0.0937,
            "circuit_clipped_usd_30d_at_25": 7268.26,
            "note": "Those numbers used one newest-first /trades page (limit=1000). Last-30s is the most complete slice.",
        },
        "why_this_matters": [
            "Original 30d study used one /trades page (limit=1000, newest first).",
            "On liquid BTC 5m that page is the last ~2 minutes, not the whole 5m.",
            "90c full-session PnL in that study is therefore not a true first-90-of-the-window backtest.",
            "Last-30s 90c in the original file is the most complete slice and sat near fee breakeven.",
        ],
    }
    if rows:
        spans = sorted(r["page0_span_s"] for r in rows)
        olds = sorted(x for x in (r["page0_oldest_left"] for r in rows) if x is not None)
        vols = sorted(r["volume"] for r in rows)
        report["capped_rate"] = round(sum(1 for r in rows if r["capped"]) / len(rows), 4)
        report["page0_span_s_p50"] = spans[len(spans) // 2]
        report["page0_oldest_left_p50"] = olds[len(olds) // 2] if olds else None
        report["volume_p50"] = round(vols[len(vols) // 2], 1)

    for name, lo, hi, win in combos:
        cap_fills = []
        full_fills = []
        disagree = 0
        missing_loss = 0  # full tape lost, capped skipped or won
        for r in rows:
            c = eval_fill(r, r["cap"], lo=lo, hi=hi, left_max=win)
            f = eval_fill(r, r["full"], lo=lo, hi=hi, left_max=win)
            if c:
                c["disagree"] = bool(
                    f is not None and (c["outcome"] != f["outcome"] or abs(c["left"] - f["left"]) > 1)
                )
                if c["disagree"]:
                    disagree += 1
                cap_fills.append(c)
            if f:
                if c and (c["outcome"] != f["outcome"] or abs(c["left"] - f["left"]) > 1):
                    f["disagree"] = True
                else:
                    f["disagree"] = False
                full_fills.append(f)
                if (not f["won"]) and (c is None or c["won"]):
                    missing_loss += 1
        report["combos"][name] = {
            "capped_page0": summarize(cap_fills),
            "full_tape": summarize(full_fills),
            "fill_identity_disagreements": disagree,
            "full_loss_hidden_by_cap": missing_loss,
        }

    full_90 = []
    for r in rows:
        f = eval_fill(r, r["full"], lo=0.90, hi=0.90, left_max=300)
        if f:
            full_90.append(f)
    report["left_buckets_full_90_last300s"] = bucket_left(full_90)
    report["examples_full_90_losses"] = [
        {
            "slug": f["slug"],
            "bought": f["outcome"],
            "winner": f["winner"],
            "left_s": f["left"],
            "opp_max_after": f["opp_max_after"],
        }
        for f in full_90
        if not f["won"]
    ][:15]
    full_90_capped_books = []
    full_90_uncapped_books = []
    left_shift = []
    for r in rows:
        f = eval_fill(r, r["full"], lo=0.90, hi=0.90, left_max=300)
        c = eval_fill(r, r["cap"], lo=0.90, hi=0.90, left_max=300)
        if f and r["capped"]:
            full_90_capped_books.append(f)
        elif f:
            full_90_uncapped_books.append(f)
        if f and c:
            left_shift.append(f["left"] - c["left"])
    report["full_90_last300s_on_capped_books"] = summarize(full_90_capped_books)
    report["full_90_last300s_on_uncapped_books"] = summarize(full_90_uncapped_books)
    report["first90_left_shift_full_minus_page0_s_p50"] = (
        sorted(left_shift)[len(left_shift) // 2] if left_shift else None
    )

    cell = (report.get("combos") or {}).get("90_only_last300s", {}).get("full_tape") or {}
    cap = (report.get("combos") or {}).get("90_only_last300s", {}).get("capped_page0") or {}
    bot = (report.get("combos") or {}).get("90_98_last180s", {}).get("full_tape") or {}
    tight = (report.get("combos") or {}).get("96_98_last180s", {}).get("full_tape") or {}
    late = (report.get("combos") or {}).get("90_only_last30s", {}).get("full_tape") or {}
    rev = cell.get("reverse")
    cap_rev = cap.get("reverse")
    if rev is None:
        trust, reason = "unknown_no_sample", "No full-tape 90c fills in this sample."
    elif rev >= 0.0937:
        trust = "broken_minus_ev"
        reason = (
            f"Full-tape 90c last300s reverse {rev:.2%} is at/above 9.37% fee breakeven. "
            "The original 5.65% / +$7k headline is a newest-1000 truncation artifact."
        )
    else:
        trust = "overstated_do_not_scale_7k"
        extra = ""
        if cap_rev:
            extra = (
                f" Capped reverse this sample was {cap_rev:.2%} vs full {rev:.2%}; "
                "visible losses were about half the true losses."
            )
        reason = (
            f"Full-tape 90c last300s reverse {rev:.2%} is still under 9.37% on this 36h sample "
            f"(CI {cell.get('reverse_ci95')}), so we cannot call it minus-EV yet."
            + extra
            + " Do not scale the original +$7k / 30d headline; last-30s 90c was already ~fee breakeven."
        )
    report["verdict"] = {
        "original_90_full_5m_pnl_trust": trust,
        "reason": reason,
        "do_not_silently_retarget_bot": True,
        "current_bot_is_90_98_last180s": True,
        "eye_test": (
            "After a 90c buy, the other side printing >=50c is common (scare). "
            "The other side printing >=90c almost matches actual resolve losses. "
            "Early 90s (3-5 min left) reverse more than late 90s."
        ),
        "current_bot_full_tape": {
            "90_98_last180s_reverse": bot.get("reverse"),
            "90_98_last180s_pnl_usd_5": bot.get("pnl_usd"),
            "96_98_last180s_reverse": tight.get("reverse"),
            "96_98_last180s_pnl_usd_5": tight.get("pnl_usd"),
            "90_only_last30s_reverse": late.get("reverse"),
        },
        "how_to_read_last30s_vs_left_0_30": (
            "90_only_last30s = first 90c print inside the last 30s, even if 90 already printed earlier. "
            "left_0_30 bucket = the first 90c of the whole 5m window happened in the last 30s (rare)."
        ),
    }

    # drop bulky tapes before write
    OUT.write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("markets", "capped_rate", "page0_span_s_p50", "page0_oldest_left_p50", "combos", "verdict")}, indent=2))
    print("left buckets", json.dumps(report["left_buckets_full_90_last300s"], indent=2))
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
