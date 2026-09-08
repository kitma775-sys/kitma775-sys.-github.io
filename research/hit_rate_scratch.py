#!/usr/bin/env python3
"""Scratch-stop / min_bid / rescore grid on the same BTC+ETH TWAP tape."""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.twap import TwapParams  # noqa: E402
import hit_rate_gates as hg  # noqa: E402
import twap_engine as te  # noqa: E402
import twap_freq as tf  # noqa: E402

OUT = Path(__file__).with_name("hit_rate_scratch.json")


def simulate_scratch(ev, series, prints, params: TwapParams, *, pick: str, rescore: int, adverse: float) -> dict | None:
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
        from app.twap import lead_bps, fair_p_up, entry_edge

        lead = lead_bps(tw, tw_open)
        if lead is None or abs(lead) < params.min_lead_bps:
            continue
        side = "Up" if lead >= 0 else "Down"
        pr = te.last_print(prints, ts, side, slack=25)
        if pr is None:
            continue
        if not (params.min_price - 1e-12 <= pr["px"] <= params.max_price + 1e-12):
            continue
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        if fair_up is None:
            continue
        fair = fair_up if side == "Up" else (1.0 - fair_up)
        if entry_edge(fair, pr["px"], 0.07) < params.min_edge:
            continue
        cand = {"ts": ts, "left": left, "side": side, "px": pr["px"], "lead": lead, "fair": fair}
        if pick == "first":
            picked = cand
            break
        picked = cand
    if not picked:
        return None
    from app.twap import lead_bps, fair_p_up, should_scratch

    shares = te.NOTIONAL / max(picked["px"], 0.01)
    exit_px = None
    exit_why = "settle"
    for ts in range(picked["ts"] + rescore, end - 3, rescore):
        left = end - ts
        tw = series.twap(ts, params.lookback)
        if tw is None:
            continue
        lead = lead_bps(tw, tw_open) or 0.0
        signed = lead if picked["side"] == "Up" else -lead
        vol = series.realized_vol_bps_sqrt_s(ts, 120)
        fair_up = fair_p_up(lead, vol, float(left), lookback=params.lookback)
        fair = None if fair_up is None else (fair_up if picked["side"] == "Up" else 1.0 - fair_up)
        mark = te.last_print(prints, ts, picked["side"], slack=30)
        bid = None if mark is None else mark["px"]
        go, why = should_scratch(
            fair_p=fair,
            lead_bps_signed=signed,
            bid=bid,
            shares=shares,
            fee_rate=0.07,
            left=float(left),
            params=params,
            fill_px=picked["px"],
            adverse=adverse,
        )
        if not go:
            continue
        nxt = te.next_print(prints, ts, picked["side"], slack=8) or mark
        if nxt is None:
            continue
        exit_px = nxt["px"]
        exit_why = why
        break
    won = picked["side"] == winner
    if exit_px is not None:
        pnl = te.pnl_scratch(picked["px"], exit_px)
        scratched = True
    else:
        pnl = te.pnl_hold(picked["px"], won)
        scratched = False
    return {
        "slug": ev["slug"],
        "asset": ev.get("asset"),
        "end": end,
        "px": picked["px"],
        "left": picked["left"],
        "lead": round(picked["lead"], 4),
        "won": won,
        "scratched": scratched,
        "exit_why": exit_why,
        "pnl": round(pnl, 5),
    }


def run(tape, series, params, *, pick, rescore, adverse):
    rows = []
    for ev, prints in tape:
        row = simulate_scratch(ev, series, prints, params, pick=pick, rescore=rescore, adverse=adverse)
        if row:
            rows.append(row)
    return rows


def main() -> None:
    events = json.loads((tf.rp.CACHE / "_events.json").read_text())
    newest = max(e["end"] for e in events)
    t0 = te.TWAP60_START - 180
    t1 = newest + 5
    print("load series", flush=True)
    series_of = {"btc": tf.rp.load_series("btc", t0, t1), "eth": tf.rp.load_series("eth", t0, t1)}
    tapes = {"btc": tf.load_asset_tape(events, "btc"), "eth": tf.load_asset_tape(events, "eth")}
    variants = [
        ("base_15s_bid38_adv0", dict(scratch_min_bid=0.38, scratch_p=0.48), 15, 0.0),
        ("bid32_15s_adv0", dict(scratch_min_bid=0.32, scratch_p=0.48), 15, 0.0),
        ("bid32_15s_adv08", dict(scratch_min_bid=0.32, scratch_p=0.48), 15, 0.08),
        ("bid32_8s_adv08", dict(scratch_min_bid=0.32, scratch_p=0.48), 8, 0.08),
        ("bid38_15s_adv08", dict(scratch_min_bid=0.38, scratch_p=0.48), 15, 0.08),
        ("p52_bid32_adv08", dict(scratch_min_bid=0.32, scratch_p=0.52), 15, 0.08),
        ("bid28_8s_adv10", dict(scratch_min_bid=0.28, scratch_p=0.48), 8, 0.10),
    ]
    gates = []
    params0 = TwapParams(min_price=0.45, max_price=0.55, min_lead_bps=6.0, min_edge=0.04, min_left=12.0, max_left=280.0)
    for name, kw, rescore, adverse in variants:
        params = TwapParams(
            min_price=0.45,
            max_price=0.55,
            min_lead_bps=6.0,
            min_edge=0.04,
            min_left=12.0,
            max_left=280.0,
            scratch_min_bid=kw["scratch_min_bid"],
            scratch_p=kw["scratch_p"],
        )
        rows = []
        for asset, series in series_of.items():
            rows.extend(run(tapes[asset], series, params, pick="last", rescore=rescore, adverse=adverse))
        rec = hg.pack(rows)
        rec["name"] = name
        rec["why"] = {k: sum(1 for r in rows if r["exit_why"] == k) for k in sorted({r["exit_why"] for r in rows})}
        gates.append(rec)
        h = rec["holdout"]
        print(
            f"{name:22s} n={rec['all']['n']} all ${rec['all']['pnl_usd']:+.1f} "
            f"hold hit={h.get('take_win_rate')} pnl={h['pnl_usd']:+.1f} "
            f"scratch={rec['all']['scratch_n']} why={rec['why']}",
            flush=True,
        )
    # also last min_left 120 + stop (rev46 entry + new scratch)
    params = TwapParams(min_price=0.45, max_price=0.55, min_lead_bps=6.0, min_edge=0.04, min_left=120.0, max_left=280.0, scratch_min_bid=0.32, scratch_p=0.48)
    rows = []
    for asset, series in series_of.items():
        rows.extend(run(tapes[asset], series, params, pick="last", rescore=8, adverse=0.08))
    rec = hg.pack(rows)
    rec["name"] = "rev46entry_bid32_8s_adv08"
    rec["why"] = {k: sum(1 for r in rows if r["exit_why"] == k) for k in sorted({r["exit_why"] for r in rows})}
    gates.append(rec)
    print("rev46entry", rec["holdout"].get("take_win_rate"), rec["all"]["pnl_usd"], rec["why"], flush=True)

    OUT.write_text(
        json.dumps(
            {
                "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "gates": [{k: g[k] for k in ("name", "all", "train", "holdout", "robust", "hit70", "hit80", "why") if k in g} for g in gates],
            },
            indent=2,
        )
        + "\n"
    )
    print("wrote", OUT)


if __name__ == "__main__":
    main()
