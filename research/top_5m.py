#!/usr/bin/env python3
"""How top 5m crypto wallets actually trade, vs a TWAP-follow mid-band taker.

Favorite 97-98 last-60s is slightly -EV. This study (1) profiles public CRYPTO
leaderboard wallets on btc/eth 5m tape, (2) reconstructs mid-band TWAP-follow
entries from Gamma PTB + Binance 1s on the TWAP-60 era, (3) compares PnL to the
favorite lift. Hold out the newest 10 days of the joined sample.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reverse_30d as r30
import reverse_predict as rp

OUT = Path(__file__).with_name("top_5m.json")
DATA = "https://data-api.polymarket.com"
UA = {
    "User-Agent": "surf-arb-research/1.9 (read-only; 5m top-wallet + midband; no trading)",
    "Accept": "application/json",
}
MID_LO, MID_HI = 0.45, 0.55
HOLDOUT_DAYS = 10
TWAP60_START = int(datetime(2026, 8, 14, tzinfo=timezone.utc).timestamp())


def get(url: str, tries: int = 4):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except Exception as exc:
            last = exc
            time.sleep(0.35 * (2**i))
    if last:
        raise last
    return None


def is_5m_crypto(slug: str) -> bool:
    s = str(slug or "")
    return s.startswith("btc-updown-5m-") or s.startswith("eth-updown-5m-")


def band_of(px: float) -> str:
    if px < 0.45:
        return "longshot_<45"
    if px <= 0.55:
        return "mid_45_55"
    if px < 0.90:
        return "midhi_56_89"
    if px < 0.97:
        return "hi_90_96"
    return "favorite_97_99"


def leaderboard(order: str, n: int = 20) -> list[dict]:
    return (
        get(
            f"{DATA}/v1/leaderboard?category=CRYPTO&timePeriod=WEEK&orderBy={order}&limit={n}"
        )
        or []
    )


def user_trades(wallet: str, pages: int = 2) -> list[dict]:
    rows = []
    for p in range(pages):
        chunk = get(f"{DATA}/trades?user={wallet}&limit=1000&offset={p * 1000}") or []
        if not chunk:
            break
        rows.extend(chunk)
        if len(chunk) < 1000:
            break
        time.sleep(0.04)
    return rows


def slug_start(slug: str) -> int | None:
    try:
        return int(str(slug).rsplit("-", 1)[-1])
    except (TypeError, ValueError):
        return None


def profile_wallet(row: dict, *, source: str) -> dict:
    w = row["proxyWallet"]
    trades = user_trades(w, pages=2)
    m5 = [t for t in trades if is_5m_crypto(t.get("slug") or t.get("eventSlug") or "")]
    buys = [t for t in m5 if str(t.get("side")) == "BUY"]
    sells = [t for t in m5 if str(t.get("side")) == "SELL"]
    px = [float(t["price"]) for t in buys]
    bands = Counter(band_of(p) for p in px)
    by_mkt: dict[str, list[dict]] = defaultdict(list)
    for t in m5:
        by_mkt[str(t.get("slug") or "")].append(t)
    both = 0
    for ts in by_mkt.values():
        oc = {str(x.get("outcome")) for x in ts if str(x.get("side")) == "BUY"}
        if "Up" in oc and "Down" in oc:
            both += 1
    lefts = []
    for t in buys:
        st = slug_start(t.get("slug") or "")
        ts = t.get("timestamp")
        if st and ts:
            lefts.append(st + 300 - int(ts))
    med_px = sorted(px)[len(px) // 2] if px else None
    med_left = sorted(lefts)[len(lefts) // 2] if lefts else None
    return {
        "source": source,
        "rank": row.get("rank"),
        "name": row.get("userName") or "",
        "wallet": w,
        "week_vol": round(float(row.get("vol") or 0), 2),
        "week_pnl": round(float(row.get("pnl") or 0), 2),
        "n_trades_pulled": len(trades),
        "n_5m": len(m5),
        "frac_5m": None if not trades else round(len(m5) / len(trades), 4),
        "n_buy": len(buys),
        "n_sell": len(sells),
        "sell_frac": None if not m5 else round(len(sells) / len(m5), 4),
        "n_markets": len(by_mkt),
        "both_sides_markets": both,
        "both_sides_frac": None if not by_mkt else round(both / len(by_mkt), 4),
        "bands": dict(bands),
        "favorite_buy_frac": None if not px else round(bands["favorite_97_99"] / len(px), 4),
        "mid_buy_frac": None if not px else round(bands["mid_45_55"] / len(px), 4),
        "longshot_buy_frac": None if not px else round(bands["longshot_<45"] / len(px), 4),
        "median_buy_px": None if med_px is None else round(med_px, 4),
        "median_left_s": med_left,
        "style": classify_style(bands, len(sells), len(m5), both, len(by_mkt)),
    }


def classify_style(bands: Counter, n_sell: int, n5: int, both: int, n_mkt: int) -> str:
    if n5 < 20:
        return "not_5m"
    fav = bands.get("favorite_97_99", 0) / max(sum(bands.values()), 1)
    mid = bands.get("mid_45_55", 0) / max(sum(bands.values()), 1)
    ls = bands.get("longshot_<45", 0) / max(sum(bands.values()), 1)
    sell = n_sell / max(n5, 1)
    two = both / max(n_mkt, 1)
    if sell > 0.25 and two > 0.4:
        return "two_sided_scratch_or_mm"
    if fav > 0.4:
        return "favorite_taker"
    if mid + ls > 0.7:
        return "mid_or_longshot_directional"
    if two > 0.5:
        return "both_sides_accumulator"
    return "mixed"


def first_mid_buys(trades: list[dict]) -> list[dict]:
    out = []
    seen = set()
    for t in trades:
        if t["side"] != "BUY":
            continue
        if not (MID_LO - 1e-12 <= t["px"] <= MID_HI + 1e-12):
            continue
        if t["left"] < 3 or t["left"] > 297:
            continue
        key = t["outcome"]
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
        if len(seen) == 2:
            break
    return out


def simulate_joined(events: list[dict], meta: dict, series_map: dict) -> list[dict]:
    """One row per 5m market with mid-band and favorite candidates."""
    rows = []
    for ev in events:
        if ev["end"] < TWAP60_START:
            continue
        m = meta.get(ev["slug"])
        if not m or int(m.get("lookback") or 0) < 60:
            continue
        path = rp.CACHE / f"{ev['slug']}.json"
        if not path.exists():
            continue
        try:
            trades = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if not trades:
            continue
        pxsrc = series_map[ev["asset"]]
        ptb = float(m["ptb"])
        lb = int(m["lookback"])
        fav = r30.first_band_fill(trades)
        mids = first_mid_buys(trades)
        # TWAP-follow: at first mid print, require same-source lead on that side
        picks = []
        for t in mids:
            tw_now = pxsrc.twap(t["ts"], lb)
            tw_open = pxsrc.twap(ev["start"], lb)
            if tw_now is None or tw_open is None or tw_open <= 0:
                continue
            lead = (tw_now - tw_open) / tw_open * 10000.0
            signed = lead if t["outcome"] == "Up" else -lead
            vol = pxsrc.realized_vol_bps_sqrt_s(t["ts"], 120)
            fair = rp.fair_p_stay(signed, vol, float(t["left"])) if vol else None
            won = t["outcome"] == ev["winner"]
            picks.append(
                {
                    "outcome": t["outcome"],
                    "px": t["px"],
                    "left": t["left"],
                    "ts": t["ts"],
                    "signed_open_bps": round(signed, 4),
                    "fair_p": fair,
                    "won": won,
                    "pnl": round(r30.pnl_usd(t["px"], won), 5),
                }
            )

        def best_follow(min_lead: float, min_fair: float | None = None):
            ok = [
                p
                for p in picks
                if p["signed_open_bps"] >= min_lead
                and (min_fair is None or (p["fair_p"] is not None and p["fair_p"] >= min_fair))
            ]
            if not ok:
                return None
            # earlier fill if both sides qualify
            return min(ok, key=lambda x: x["ts"])

        fav_row = None
        if fav is not None:
            won = fav["outcome"] == ev["winner"]
            fav_row = {
                "px": fav["px"],
                "left": fav["left"],
                "won": won,
                "pnl": round(r30.pnl_usd(fav["px"], won), 5),
            }
        dumb = None
        if mids:
            t = min(mids, key=lambda x: x["ts"])
            won = t["outcome"] == ev["winner"]
            dumb = {
                "px": t["px"],
                "left": t["left"],
                "won": won,
                "pnl": round(r30.pnl_usd(t["px"], won), 5),
                "outcome": t["outcome"],
            }
        rows.append(
            {
                "slug": ev["slug"],
                "asset": ev["asset"],
                "end": ev["end"],
                "winner": ev["winner"],
                "favorite": fav_row,
                "mid_first": dumb,
                "follow_0bps": best_follow(0.0),
                "follow_2bps": best_follow(2.0),
                "follow_fair58": best_follow(0.0, 0.58),
                "follow_fair62": best_follow(0.0, 0.62),
            }
        )
    return rows


def summarize_leg(rows: list[dict], key: str) -> dict:
    part = [r[key] for r in rows if r.get(key)]
    if not part:
        return {"n": 0, "pnl_usd": 0.0}
    wrapped = [
        {
            "won": p["won"],
            "px": p["px"],
            "pnl": p["pnl"],
            "left": p["left"],
            "looked_50": False,
            "looked_90": False,
        }
        for p in part
    ]
    out = r30.summarize(wrapped)
    out["take_frac"] = round(len(part) / max(len(rows), 1), 4)
    return out


def split_holdout(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    if not rows:
        return [], []
    newest = max(r["end"] for r in rows)
    cut = newest - HOLDOUT_DAYS * 86400
    return [r for r in rows if r["end"] < cut], [r for r in rows if r["end"] >= cut]


def build_findings(report: dict) -> dict:
    wallets = report["wallets"]
    five = [w for w in wallets if (w.get("n_5m") or 0) >= 50]
    styles = Counter(w["style"] for w in five)
    fav_fracs = [w["favorite_buy_frac"] for w in five if w.get("favorite_buy_frac") is not None]
    mid_fracs = [w["mid_buy_frac"] for w in five if w.get("mid_buy_frac") is not None]
    med_px = [w["median_buy_px"] for w in five if w.get("median_buy_px") is not None]
    sim = report["sim_twap60"]
    follow = sim["all"].get("follow_2bps") or {}
    fav = sim["all"].get("favorite") or {}
    follow_h = sim["holdout"].get("follow_2bps") or {}
    fav_h = sim["holdout"].get("favorite") or {}
    follow_ok = bool(follow.get("ev_ok") and follow_h.get("ev_ok") and (follow.get("n") or 0) >= 80)
    return {
        "headline_cantonese": (
            "5 分鐘頂層贏家唔係抬 97¢ 大熱。CRYPTO 週榜入面真正打 5m 嘅錢包，"
            "買入中位價約 0.49–0.52，大熱 97+ 只佔好細；有人雙邊／有人中間價方向盤。"
            f"TWAP-60 時代跟開盤 lead ≥2bps 喺 45–55¢ 入場："
            f"全樣本 PnL {follow.get('pnl_usd')}（n={follow.get('n')} ev_ok={follow.get('ev_ok')}），"
            f"holdout {follow_h.get('pnl_usd')}。"
            f"同期大熱 97–98 全樣本 {fav.get('pnl_usd')}。"
        ),
        "wallet_styles": dict(styles),
        "n_wallets_with_50plus_5m": len(five),
        "median_of_median_buy_px": None if not med_px else round(sorted(med_px)[len(med_px) // 2], 4),
        "mean_favorite_buy_frac": None if not fav_fracs else round(sum(fav_fracs) / len(fav_fracs), 4),
        "mean_mid_buy_frac": None if not mid_fracs else round(sum(mid_fracs) / len(mid_fracs), 4),
        "sim_follow_2bps_robust": follow_ok,
        "split_note": (
            "TWAP-60 starts 2026-08-14; newest-10d holdout is most of the sample. "
            "Train n is small. Sign-flip (train −EV, holdout +EV) = do not ship."
        ),
        "recommend": {
            "stop_now": "停大熱 97–98 taker（Rev 22：strategy_mode=complement）。呢個月費後略負，同頂層錢包行為相反。",
            "keep": "互補 taker 仍然要 min_edge 0.02、FOK、maker 關。唔好為咗『有單』減 edge。",
            "next_engine": (
                "新引擎先值得做：官方 Chainlink 60s TWAP vs 窗開 PTB → P(up)，"
                "只喺 45–55¢ 問價入場，10–30s 重估，弱倉 scratch。"
                "未接官方 TWAP 唔好用 Binance 減 PTB（9bps 基差）。"
                + (
                    " 呢個月 1s 代理 follow≥2bps 全樣本+holdout 都 +EV，可以當紙盤規格。"
                    if follow_ok
                    else " 呢個月 1s 代理 train −EV、holdout 先轉正（TWAP-60 只有約兩週，newest-10d 佔咗大半樣本），未好當上線訊號。"
                )
            ),
            "do_not": [
                "恢復大熱 97–98",
                "全段 maker（6h 回放 −EV）",
                "減 min_edge",
                "用 Binance/USDT 當 Chainlink PTB",
                "抄週榜 PnL 但 vol=0 嘅錢包（唔係 5m 流水）",
                "把 holdout 先轉正嘅 TWAP follow 當 hunt 訊號",
            ],
        },
    }


def main() -> None:
    print("leaderboards", flush=True)
    pnl_lb = leaderboard("PNL", 20)
    vol_lb = leaderboard("VOL", 20)
    seen: set[str] = set()
    jobs = []
    for src, xs in (("pnl", pnl_lb), ("vol", vol_lb)):
        for row in xs:
            w = row.get("proxyWallet")
            if not w or w in seen:
                continue
            if float(row.get("vol") or 0) < 1000:
                continue
            seen.add(w)
            jobs.append((src, row))
    print(f"profile {len(jobs)} wallets", flush=True)
    wallets = []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futs = {pool.submit(profile_wallet, row, source=src): (src, row) for src, row in jobs}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                wallets.append(fut.result())
            except Exception as exc:
                src, row = futs[fut]
                wallets.append({"source": src, "wallet": row.get("proxyWallet"), "error": str(exc)})
            if i % 5 == 0:
                print(f"  wallets {i}/{len(jobs)}", flush=True)
    wallets.sort(key=lambda w: (-(w.get("n_5m") or 0), -(w.get("week_pnl") or 0)))

    print("load fills/meta/1s for TWAP60 sim", flush=True)
    events = json.loads((rp.CACHE / "_events.json").read_text())
    fills = rp.load_fills()
    newest_end = max(r["end"] for r in fills)
    oldest_end = min(r["end"] for r in fills)
    meta = rp.list_gamma_meta(oldest_end - 300, newest_end + 300)
    meta = rp.fetch_missing_meta([e["slug"] for e in events if e["end"] >= TWAP60_START], meta)
    t0 = TWAP60_START - 180
    t1 = newest_end + 5
    series_map = {a: rp.load_series(a, t0, t1) for a in r30.ASSETS}
    sim_rows = simulate_joined(events, meta, series_map)
    train, test = split_holdout(sim_rows)
    keys = ["favorite", "mid_first", "follow_0bps", "follow_2bps", "follow_fair58", "follow_fair62"]

    def pack(rs):
        return {k: summarize_leg(rs, k) for k in keys}

    report = {
        "researched_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "n_wallets": len(wallets),
        "wallets": wallets,
        "style_counts": dict(Counter(w.get("style") for w in wallets if w.get("style"))),
        "sim_n_markets": len(sim_rows),
        "sim_range": {
            "oldest_end": r30.iso_utc(min((r["end"] for r in sim_rows), default=0)),
            "newest_end": r30.iso_utc(max((r["end"] for r in sim_rows), default=0)),
        },
        "sim_twap60": {"all": pack(sim_rows), "train": pack(train), "holdout": pack(test)},
        "note": (
            "Wallet trades are the public taker tape (up to 2000 prints/wallet). "
            "Makers who only rest may be under-counted. Mid-band sim uses Binance 1s TWAP "
            "vs Binance open as same-source lead (not Gamma PTB; ~9bps basis)."
        ),
    }
    report["findings"] = build_findings(report)
    OUT.write_text(json.dumps(report, indent=2) + "\n")
    print(f"wrote {OUT}", flush=True)
    print("styles", report["style_counts"], flush=True)
    print("sim", report["sim_twap60"]["all"], flush=True)
    print(report["findings"]["headline_cantonese"], flush=True)


if __name__ == "__main__":
    main()
