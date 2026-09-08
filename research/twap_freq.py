#!/usr/bin/env python3
"""Rev 27: more TWAP takes without lowering 6bps or restoring complement/favorite.

Reuses the TWAP-60 tape (Binance 1s vs window-open, same-source proxy).
Live still uses Chainlink vs Chainlink T0. Scratch every 15s as live.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import TwapParams  # noqa: E402
import reverse_predict as rp  # noqa: E402
import twap_engine as te  # noqa: E402

OUT = Path(__file__).with_name("twap_freq.json")
TWAP60_START = te.TWAP60_START
BANDS = (
    (0.45, 0.55, "45-55"),
    (0.42, 0.58, "42-58"),
    (0.40, 0.60, "40-60"),
    (0.45, 0.60, "45-60"),
)
MAX_LEFT = (180.0, 240.0, 280.0)


def prints_in_band(trades: list[dict], start: int, end: int, lo: float, hi: float) -> list[dict]:
    out = []
    for t in trades:
        if str(t.get("side") or "").upper() != "BUY":
            continue
        try:
            px = float(t.get("price") or t.get("px") or 0)
            ts = int(t.get("timestamp") or t.get("ts") or 0)
        except (TypeError, ValueError):
            continue
        if px < lo or px > hi:
            continue
        if ts < start or ts > end:
            continue
        oc = str(t.get("outcome") or t.get("title") or "")
        if oc not in {"Up", "Down"}:
            continue
        out.append({"ts": ts, "px": px, "outcome": oc})
    out.sort(key=lambda x: x["ts"])
    return out


def load_asset_tape(events, asset: str) -> list[tuple[dict, list[dict]]]:
    out = []
    for ev in events:
        if ev.get("asset") != asset or ev["end"] < TWAP60_START:
            continue
        path = rp.CACHE / f"{ev['slug']}.json"
        if not path.exists():
            continue
        try:
            trades = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        # Widest band once; filter cheaper later.
        prints = prints_in_band(trades, ev["start"], ev["end"], 0.40, 0.60)
        if prints:
            out.append((ev, prints))
    return out


def run_tape(tape, series, params: TwapParams, lo: float, hi: float) -> list[dict]:
    rows = []
    for ev, prints in tape:
        band = [p for p in prints if lo - 1e-12 <= p["px"] <= hi + 1e-12]
        if not band:
            continue
        row = te.simulate_market(ev, series, band, params)
        if row:
            rows.append(row)
    return rows


def main() -> None:
    events = json.loads((rp.CACHE / "_events.json").read_text())
    newest = max(e["end"] for e in events)
    t0 = TWAP60_START - 180
    t1 = newest + 5
    print("load btc series", flush=True)
    btc_series = rp.load_series("btc", t0, t1)
    print("load eth series", flush=True)
    eth_series = rp.load_series("eth", t0, t1)
    print("index tapes", flush=True)
    tapes = {
        "btc": load_asset_tape(events, "btc"),
        "eth": load_asset_tape(events, "eth"),
    }
    print(f"tape btc {len(tapes['btc'])} eth {len(tapes['eth'])}", flush=True)

    grid = []
    series_of = {"btc": btc_series, "eth": eth_series}
    for asset, series in series_of.items():
        for max_left in MAX_LEFT:
            for lo, hi, band in BANDS:
                params = TwapParams(
                    min_price=lo,
                    max_price=hi,
                    min_lead_bps=6.0,
                    min_edge=0.04,
                    max_left=max_left,
                )
                rows = run_tape(tapes[asset], series, params, lo, hi)
                train, hold = te.split_holdout(rows)
                rec = {
                    "asset": asset,
                    "band": band,
                    "min_price": lo,
                    "max_price": hi,
                    "max_left": max_left,
                    "min_lead_bps": 6.0,
                    "min_edge": 0.04,
                    "all": te.summarize(rows),
                    "train": te.summarize(train),
                    "holdout": te.summarize(hold),
                }
                rec["robust"] = bool(
                    rec["train"]["ev_ok"]
                    and rec["holdout"]["ev_ok"]
                    and rec["train"]["n"] >= 25
                    and rec["holdout"]["n"] >= 25
                )
                rec["pnl_per"] = None if rec["all"]["n"] == 0 else round(rec["all"]["pnl_usd"] / rec["all"]["n"], 3)
                grid.append(rec)
                print(
                    f"{asset} left≤{max_left:.0f} {band} "
                    f"all {rec['all']['pnl_usd']:+.1f} n={rec['all']['n']} "
                    f"train {rec['train']['pnl_usd']:+.1f} n={rec['train']['n']} "
                    f"hold {rec['holdout']['pnl_usd']:+.1f} n={rec['holdout']['n']} "
                    f"$/{rec['pnl_per']} robust={rec['robust']}",
                    flush=True,
                )

    btc_base = next(g for g in grid if g["asset"] == "btc" and g["max_left"] == 180 and g["band"] == "45-55")
    btc_ok = [
        g
        for g in grid
        if g["asset"] == "btc" and g["robust"] and g["all"]["n"] >= btc_base["all"]["n"]
    ]
    # Ship 45-55 + the longest 6bps window. Wider bands add tape prints at 58-60¢
    # (optimistic vs live ask) and *lower* train PnL at 280s.
    tight = [g for g in btc_ok if g["band"] == "45-55"]
    picked = max(tight, key=lambda g: (g["max_left"], g["holdout"]["pnl_usd"])) if tight else btc_base
    eth_tight = [
        g
        for g in grid
        if g["asset"] == "eth" and g["robust"] and g["band"] == "45-55" and g["max_left"] == picked["max_left"]
    ]
    eth_pick = eth_tight[0] if eth_tight else None
    ship_eth = bool(
        eth_pick
        and eth_pick["holdout"]["take_win_rate"]
        and eth_pick["holdout"]["take_win_rate"] >= 0.62
        and eth_pick["pnl_per"] is not None
        and eth_pick["pnl_per"] >= 0.4
    )

    headline = (
        f"Rev 27 加頻：BTC 5m 維持 lead≥6bps + scratch + 45–55¢ + 費後缺口 0.04。"
        f"校準由 left≤180s n={btc_base['all']['n']} / hold {btc_base['holdout']['pnl_usd']}"
        f" → left≤{picked['max_left']:.0f}s n={picked['all']['n']} / "
        f"train {picked['train']['pnl_usd']} hold {picked['holdout']['pnl_usd']}。"
        + (
            f" ETH 5m 同樣規則 holdout +EV（n={eth_pick['all']['n']} hold {eth_pick['holdout']['pnl_usd']}），一齊開。"
            if ship_eth and eth_pick
            else " ETH 5m 唔夠穩，仍然只入場 BTC。"
        )
        + " 唔降 6bps、唔放寬到 60¢、唔抄雙邊鎖倉、唔開大熱。"
    )
    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "proxy": "Binance 1s TWAP vs Binance open. Live = Chainlink vs Chainlink T0.",
        "notional": te.NOTIONAL,
        "holdout_days": te.HOLDOUT_DAYS,
        "baseline": btc_base,
        "picked": picked,
        "eth_pick": eth_pick,
        "ship_eth": ship_eth,
        "shipped": {
            "max_left": picked["max_left"],
            "band": picked["band"],
            "min_lead_bps": 6.0,
            "min_edge": 0.04,
            "assets": ["btc", "eth"] if ship_eth else ["btc"],
            "why_not_wider_band": "280s 45-60 train PnL < 45-55; 60¢ prints optimistic vs live ask",
            "why_not_2bps": "tape noise / optimistic last print",
        },
        "grid": grid,
        "findings": {
            "headline_cantonese": headline,
            "keep": ["min_lead_bps=6", "scratch", "min_edge=0.04", "no pair-lock taker", "no favorite", "paper"],
            "copy": (
                f"方向盤戶更早入場：max_left {picked['max_left']:.0f}s、價帶 {picked['band']}¢、"
                f"lead≥6bps、scratch。頂級戶中位剩餘 ~165–183s。"
            ),
            "do_not_copy": [
                "lead 2bps 全段（tape 樂觀成交）",
                "分時雙邊鎖倉 taker",
                "97–98 大熱",
                "scratch 改對沖",
            ],
        },
    }
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(headline, flush=True)
    print(f"wrote {OUT}", flush=True)


if __name__ == "__main__":
    main()
