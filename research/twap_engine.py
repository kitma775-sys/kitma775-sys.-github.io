#!/usr/bin/env python3
"""Calibrate the live TWAP mid-band engine on BTC 5m (TWAP-60 era).

Proxy: Binance 1s TWAP vs Binance window-open (same source). This is the
live model — Chainlink ticks vs the first Chainlink tick at T0 — NOT
Binance minus Gamma PTB.

Scratch: every 15s after entry, sell at the next same-side print if fair P
drops below 0.48, lead flips, or the bid (print) beats holding.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.fees import taker_fee  # noqa: E402
from app.twap import (  # noqa: E402
    TWAP_LOOKBACK,
    TwapParams,
    entry_edge,
    fair_p_up,
    lead_bps,
    should_scratch,
)
import reverse_30d as r30  # noqa: E402
import reverse_predict as rp  # noqa: E402

OUT = Path(__file__).with_name("twap_engine.json")
TWAP60_START = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp())
NOTIONAL = 5.0
RESCORE = 15
HOLDOUT_DAYS = 7  # TWAP-60 is short; 7d newest vs older train


def mid_prints(trades: list[dict], start: int, end: int) -> list[dict]:
    out = []
    for t in trades:
        if str(t.get("side") or "").upper() != "BUY":
            continue
        try:
            px = float(t.get("price") or t.get("px") or 0)
            ts = int(t.get("timestamp") or t.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if px < 0.45 or px > 0.55:
            continue
        if ts < start or ts > end:
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc not in {"Up", "Down"}:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc})
    out.sort(key=lambda x: x["ts"])
    return out


def last_print(prints: list[dict], ts: int, outcome: str, slack: int = 20) -> dict | None:
    best = None
    for p in prints:
        if p["outcome"] != outcome:
            continue
        if p["ts"] > ts:
            break
        if ts - p["ts"] <= slack:
            best = p
    return best


def next_print(prints: list[dict], ts: int, outcome: str, slack: int = 8) -> dict | None:
    for p in prints:
        if p["outcome"] != outcome:
            continue
        if p["ts"] < ts:
            continue
        if p["ts"] - ts <= slack:
            return p
        break
    return None


def pnl_hold(px: float, won: bool) -> float:
    shares = NOTIONAL / max(px, 0.01)
    fee = taker_fee(shares, px, 0.07)
    if won:
        return round(shares * (1.0 - px) - fee, 5)
    return round(-shares * px - fee, 5)


def pnl_scratch(entry_px: float, exit_px: float) -> float:
    shares = NOTIONAL / max(entry_px, 0.01)
    buy_fee = taker_fee(shares, entry_px, 0.07)
    sell_fee = taker_fee(shares, exit_px, 0.07)
    return round(shares * (exit_px - entry_px) - buy_fee - sell_fee, 5)


def simulate_market(ev: dict, series, prints: list[dict], params: TwapParams) -> dict | None:
    start, end = int(ev["start"]), int(ev["end"])
    winner = ev["winner"]
    tw_open = series.twap(start, params.lookback)
    if tw_open is None or tw_open <= 0:
        return None
    t0 = start + 15
    t1 = end - int(params.min_left)
    picked = None
    for ts in range(t0, t1 + 1, 5):
        left = end - ts
        if left > params.max_left or left < params.min_left:
            continue
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open)
        if lead is None or abs(lead) < params.min_lead_bps:
            continue
        side = "Up" if lead >= 0 else "Down"
        pr = last_print(prints, ts, side, slack=25)
        if pr is None:
            continue
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) < params.min_edge:
            continue
        picked = {
            "ts": ts,
            "left": left,
            "side": side,
            "px": pr["px"],
            "lead": lead,
            "fair": fair,
        }
        break
    if not picked:
        return None
    shares = NOTIONAL / max(picked["px"], 0.01)
    exit_px = None
    exit_why = "settle"
    for ts in range(picked["ts"] + RESCORE, end - 3, RESCORE):
        left = end - ts
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open) or 0.0
        signed = lead if picked["side"] == "Up" else -lead
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
        mark = last_print(prints, ts, picked["side"], slack=30)
        bid = None if mark is None else mark["px"]
        go, why = should_scratch(
            fair_p=fair,
            lead_bps_signed=signed,
            bid=bid,
            shares=shares,
            fee_rate=0.07,
            left=float(left),
            params=params,
        )
        if not go:
            continue
        nxt = next_print(prints, ts, picked["side"], slack=8) or mark
        if nxt is None:
            continue
        exit_px = nxt["px"]
        exit_why = why
        break
    won = picked["side"] == winner
    if exit_px is not None:
        pnl = pnl_scratch(picked["px"], exit_px)
        scratched = True
    else:
        pnl = pnl_hold(picked["px"], won)
        scratched = False
    return {
        "slug": ev["slug"],
        "end": end,
        "side": picked["side"],
        "px": picked["px"],
        "left": picked["left"],
        "lead": round(picked["lead"], 4),
        "fair": round(picked["fair"], 4),
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "pnl": round(pnl, 5),
    }


def summarize(rows: list[dict]) -> dict:
    if not rows:
        return {"n": 0, "pnl_usd": 0.0, "win": 0, "scratch_n": 0, "ev_ok": False}
    pnl = sum(r["pnl"] for r in rows)
    win = sum(1 for r in rows if r["won"] and not r["scratched"])
    lose = sum(1 for r in rows if (not r["won"]) and not r["scratched"])
    held = win + lose
    return {
        "n": len(rows),
        "pnl_usd": round(pnl, 2),
        "win": win,
        "lose": lose,
        "held": held,
        "scratch_n": sum(1 for r in rows if r["scratched"]),
        "avg_px": round(sum(r["px"] for r in rows) / len(rows), 4),
        "avg_left": round(sum(r["left"] for r in rows) / len(rows), 1),
        "avg_lead": round(sum(abs(r["lead"]) for r in rows) / len(rows), 2),
        "take_win_rate": None if held == 0 else round(win / held, 4),
        "ev_ok": pnl > 0,
    }


def split_holdout(rows: list[dict], days: int = HOLDOUT_DAYS):
    if not rows:
        return [], []
    newest = max(r["end"] for r in rows)
    cut = newest - days * 86400
    return [r for r in rows if r["end"] < cut], [r for r in rows if r["end"] >= cut]


def run_params(events, series, params: TwapParams) -> list[dict]:
    rows = []
    for ev in events:
        if ev["asset"] != "btc" or ev["end"] < TWAP60_START:
            continue
        path = rp.CACHE / f"{ev['slug']}.json"
        if not path.exists():
            continue
        try:
            trades = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        prints = mid_prints(trades, ev["start"], ev["end"])
        if not prints:
            continue
        row = simulate_market(ev, series, prints, params)
        if row:
            rows.append(row)
    return rows


def main() -> None:
    events = json.loads((rp.CACHE / "_events.json").read_text())
    newest = max(e["end"] for e in events)
    series = rp.load_series("btc", TWAP60_START - 180, newest + 5)
    grid = []
    for max_left in (90.0, 120.0):
        for min_lead in (6.0, 8.0, 12.0):
            for min_edge in (0.03, 0.04, 0.06):
                params = TwapParams(min_lead_bps=min_lead, min_edge=min_edge, max_left=max_left)
                rows = run_params(events, series, params)
                train, hold = split_holdout(rows)
                rec = {
                    "max_left": max_left,
                    "min_lead_bps": min_lead,
                    "min_edge": min_edge,
                    "all": summarize(rows),
                    "train": summarize(train),
                    "holdout": summarize(hold),
                }
                rec["robust"] = bool(
                    rec["train"]["ev_ok"]
                    and rec["holdout"]["ev_ok"]
                    and rec["train"]["n"] >= 25
                    and rec["holdout"]["n"] >= 25
                )
                grid.append(rec)
                print(
                    f"left≤{max_left:.0f} lead≥{min_lead:.0f} edge≥{min_edge:.2f} "
                    f"all {rec['all']['pnl_usd']:+.1f} n={rec['all']['n']} "
                    f"train {rec['train']['pnl_usd']:+.1f} n={rec['train']['n']} "
                    f"hold {rec['holdout']['pnl_usd']:+.1f} n={rec['holdout']['n']} "
                    f"robust={rec['robust']}",
                    flush=True,
                )
    robust = [g for g in grid if g["robust"]]
    robust.sort(key=lambda g: (g["train"]["pnl_usd"] + g["holdout"]["pnl_usd"], g["all"]["n"]), reverse=True)
    picked = robust[0] if robust else max(grid, key=lambda g: (g["all"]["ev_ok"], g["all"]["pnl_usd"]))
    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": "Binance 1s TWAP vs Binance open (same source). Live uses Chainlink vs Chainlink T0.",
        "notional": NOTIONAL,
        "rescore_s": RESCORE,
        "holdout_days": HOLDOUT_DAYS,
        "grid": grid,
        "picked": picked,
        "n_robust": len(robust),
        "findings": {
            "headline_cantonese": (
                "方案 2 引擎：官方同源 TWAP vs 窗開價，45–55¢，弱倉 scratch。"
                f"校準揀 left≤{picked['max_left']:.0f}s、lead≥{picked['min_lead_bps']:.0f}bps、"
                f"edge≥{picked['min_edge']:.2f}。全樣本 PnL {picked['all']['pnl_usd']} n={picked['all']['n']}，"
                f"train {picked['train']['pnl_usd']} / holdout {picked['holdout']['pnl_usd']}，"
                f"robust={picked['robust']}。實盤 feed 係 Polymarket RTDS crypto_prices_chainlink，唔係 Binance−PTB。"
            ),
            "use_live": True,
            "note": "Hold-to-settle follow was −EV. This grid includes 15s scratch.",
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print("picked", picked, flush=True)
    print(report["findings"]["headline_cantonese"], flush=True)


if __name__ == "__main__":
    main()
