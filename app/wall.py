"""Neon ops wall: one payload for Dashboard A, Telegram log, and the running bot.

Telegram home stays the short operator_board. This module is the watch surface:
skip tape, TWAP gauges, 5-stage pipeline, per-coin slots, and a journal.
"""

from __future__ import annotations

import time
from typing import Any

from app.config import format_log_ts, format_signed_usd, inventory_matches_mode
from app.twap import hunt_assets, parse_window

WALL_TAPE_MAX = 48
PASS_REASONS = {"signal", "ready"}

SKIP_ZH = {
    "signal": "合格",
    "ready": "合格",
    "future_listing": "未開窗",
    "twap_ws_slot": "未入槽",
    "twap_window": "剩餘窗",
    "twap_no_ptb": "未有窗開價",
    "twap_no_feed": "CL 未到",
    "twap_oracle": "oracle",
    "twap_horizon": "週期",
    "twap_asset": "幣種",
    "twap_stale": "盤過期",
    "twap_thin": "盤薄",
    "twap_band": "價帶外",
    "twap_late_cheap": "遲平倉",
    "twap_no_bid": "無 bid",
    "twap_crossed": "交叉盤",
    "twap_wide": "spread 闊",
    "twap_lead": "lead 唔夠",
    "twap_no_fair": "無公平價",
    "twap_edge": "edge 唔夠",
    "twap_conflict": "已有倉",
    "twap_budget": "額度唔夠",
    "clob_halt": "CLOB 暫停",
}

STATUS_ZH = {
    "paper_filled": "紙盤成交",
    "paper_hedged": "單邊對沖",
    "paper_dumped": "單邊出貨",
    "dumped": "單邊出貨",
    "paper_settled": "結算",
    "redeemed": "redeem",
    "paper_fok_killed": "FOK殺單",
    "fok_killed": "FOK殺單",
    "filled": "成交",
    "cancelled": "已撤",
    "paper_missed": "錯過",
}

NOISE_TRADE = {"paper_leg_fill", "paper_resting", "resting"}

_REDEEM_WAIT_LOG = (
    "no market found",
    "market not found",
    "builder api key",
    "relayer api key",
    "not resolved",
    "condition not found",
)


def _is_redeem_wait_log(message: str) -> bool:
    """Hide the CLOB-delist retry spam; one-shot 等結算 stays visible."""
    text = str(message or "").lower()
    if not text.startswith("redeem fail"):
        return False
    return any(n in text for n in _REDEEM_WAIT_LOG)


def _is_dump_dust_log(message: str) -> bool:
    """Hide rounded-sell > wallet dust retries once the broker floors size."""
    text = str(message or "").lower()
    if not text.startswith("dump fail"):
        return False
    return "not enough balance" in text or "balance is not enough" in text

CURVE_STATUSES = frozenset(
    {"redeemed", "paper_settled", "dumped", "paper_dumped", "paper_hedged", "merged"}
)
HELD_STATUSES = frozenset({"redeemed", "paper_settled"})
SCRATCH_STATUSES = frozenset({"dumped", "paper_dumped"})


def utc_day_start(now: float | None = None) -> float:
    """Same UTC-day bucket as store.today_pnl / daily-loss circuit."""
    t = float(now if now is not None else time.time())
    return t - (t % 86400)


def _book_ts(row: dict, start_ts: float) -> float:
    try:
        ts = float(row.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0.0
    return ts if ts > 0 else start_ts


def _curve_event_ts(row: dict, start_ts: float) -> float:
    """Plot settles at window close so a later batch-redeem does not rewrite history."""
    sqlite_ts = _book_ts(row, start_ts)
    status = str(row.get("status") or "")
    parsed = parse_window(str(row.get("slug") or ""))
    if parsed is None or status not in HELD_STATUSES:
        return sqlite_ts
    if parsed.start < 1_000_000_000:
        return sqlite_ts
    win_end = float(parsed.start + parsed.window_seconds)
    if abs(win_end - sqlite_ts) > 86400:
        return sqlite_ts
    return win_end


def _curve_asset(slug: str) -> str:
    parsed = parse_window(slug)
    return parsed.asset.upper() if parsed else ""


def performance_today(rt) -> dict[str, Any]:
    """Mode-aware realized path + held-to-settle hit rate. Shared by TG, wall, engine."""
    live = rt.mode() == "live"
    mode = "live" if live else "paper"
    start_ts = utc_day_start()
    y0 = 0.0
    if not live:
        p = rt.store.paper_state()
        y0 = round(float(p.get("equity") or 0) - float(p.get("today_pnl") or 0), 6)
    rows = rt.store.trades_since(start_ts, mode=mode, limit=5000, statuses=tuple(CURVE_STATUSES))
    events: list[dict[str, Any]] = []
    wins = losses = scratch_n = 0
    for t in rows:
        status = str(t.get("status") or "")
        if status in NOISE_TRADE or status not in CURVE_STATUSES:
            continue
        try:
            net = float(t.get("net") or 0)
        except (TypeError, ValueError):
            net = 0.0
        if abs(net) < 1e-9:
            continue
        if status in SCRATCH_STATUSES:
            scratch_n += 1
            mark = "scratch"
        elif status in HELD_STATUSES:
            if net > 0.005:
                wins += 1
                mark = "win"
            elif net < -0.005:
                losses += 1
                mark = "lose"
            else:
                mark = "flat"
        else:
            mark = "flat"
        slug = str(t.get("slug") or "")
        events.append(
            {
                "ts": _curve_event_ts(t, start_ts),
                "book_ts": _book_ts(t, start_ts),
                "id": t.get("id") or 0,
                "net": round(net, 4),
                "slug": slug,
                "asset": _curve_asset(slug),
                "status": status,
                "mark": mark,
            }
        )
    events.sort(key=lambda e: (float(e["ts"]), float(e["book_ts"]), int(e.get("id") or 0)))
    y = y0
    t0 = start_ts
    if events:
        t0 = max(start_ts, float(events[0]["ts"]) - 60.0)
    points = [
        {
            "ts": t0,
            "y": round(y0, 4),
            "net": 0.0,
            "slug": "",
            "asset": "",
            "status": "open",
            "mark": "start",
        }
    ]
    last_t = t0
    for e in events:
        ts = float(e["ts"])
        if ts <= last_t:
            ts = last_t + 1.0
        last_t = ts
        y = round(y + float(e["net"]), 6)
        points.append(
            {
                "ts": ts,
                "y": round(y, 4),
                "net": e["net"],
                "slug": e["slug"],
                "asset": e["asset"],
                "status": e["status"],
                "mark": e["mark"],
            }
        )
    now = time.time()
    if now > last_t + 1.0:
        points.append(
            {
                "ts": now,
                "y": round(y, 4),
                "net": 0.0,
                "slug": "",
                "asset": "",
                "status": "now",
                "mark": "now",
            }
        )
    held = wins + losses
    hit_rate = None if held <= 0 else round(wins / held, 4)
    if held <= 0:
        hit_label = "—"
    else:
        hit_label = f"{wins}/{held}"
    return {
        "label": "今日已實現 PnL" if live else "今日權益",
        "note": "入金只變可用 USDC，唔計入呢條線" if live else "",
        "start": round(y0, 4),
        "end": round(y, 4),
        "points": points,
        "wins": wins,
        "losses": losses,
        "held": held,
        "scratch_n": scratch_n,
        "hit_rate": hit_rate,
        "hit_label": hit_label,
    }


def reason_zh(code: str | None) -> str:
    key = str(code or "").strip()
    return SKIP_ZH.get(key, key or "—")


def _is_future_listing(reason: str | None) -> bool:
    return str(reason or "") == "future_listing"


def note_wall_gate(rt, gate: dict | None) -> None:
    """Keep the latest row per slug so the tape moves without flooding.

    Future listings are hunt-loop noise (every coin has a next window). They
    must not overwrite the open 5m skip on the coin slots or scan tape.
    """
    if not gate:
        return
    slug = str(gate.get("slug") or "").strip()
    if not slug:
        return
    parsed = parse_window(slug)
    reason = str(gate.get("reason") or "skip")
    if _is_future_listing(reason):
        return
    row = {
        "ts": time.time(),
        "slug": slug,
        "asset": parsed.asset if parsed else "",
        "reason": reason,
        "reason_zh": reason_zh(reason),
        "ask": gate.get("ask"),
        "lead_bps": gate.get("lead_bps"),
        "left": gate.get("left"),
        "side": gate.get("side"),
        "ok": reason in PASS_REASONS,
    }
    buf = getattr(rt, "wall_tape", None)
    if buf is None:
        rt.wall_tape = []
        buf = rt.wall_tape
    for i in range(len(buf) - 1, max(-1, len(buf) - 16), -1):
        if buf[i].get("slug") == slug:
            buf[i] = row
            return
    buf.append(row)
    overflow = len(buf) - WALL_TAPE_MAX
    if overflow > 0:
        del buf[:overflow]


def _mode_inv(rt) -> list[dict]:
    live = rt.mode() == "live"
    return [r for r in rt.store.inventory_open() if inventory_matches_mode(r.get("kind"), live=live)]


def _conn_pct(status: str | None) -> int:
    s = str(status or "off").lower()
    if s in {"connected", "ok", "live"}:
        return 100
    if s in {"partial", "degraded"}:
        return 55
    if s in {"connecting", "wait"}:
        return 25
    return 8


def _gauges(rt, board: dict, gate: dict, s: dict) -> list[dict]:
    lead = gate.get("lead_bps")
    ask = gate.get("ask")
    left = gate.get("left")
    min_lead = float(s.get("twap_min_lead_bps") or 6)
    lo = float(s.get("twap_min_price") or 0.45)
    hi = float(s.get("twap_max_price") or 0.55)
    min_left = float(s.get("twap_min_left") or 120)
    max_left = float(s.get("twap_max_left") or 280)
    lead_pct = 8
    if lead is not None:
        lead_pct = int(max(8, min(100, round(abs(float(lead)) / max(min_lead, 0.01) * 70))))
        if float(lead) >= min_lead:
            lead_pct = min(100, lead_pct + 20)
    band_pct = 8
    band_txt = "—"
    if ask is not None:
        px = float(ask)
        band_txt = f"{px * 100:.0f}¢"
        if lo - 1e-9 <= px <= hi + 1e-9:
            band_pct = 100
        else:
            band_pct = 18
    left_pct = 8
    left_txt = "—"
    if left is not None:
        left_txt = f"{int(round(float(left)))}s"
        span = max(1.0, max_left - min_left)
        if min_left <= float(left) <= max_left:
            left_pct = int(max(12, min(100, round((float(left) - min_left) / span * 100))))
        else:
            left_pct = 14
    ws = board.get("ws") or rt.ws_status
    cl = board.get("chainlink") or rt.chainlink_status
    lead_txt = "—" if lead is None else f"{float(lead):+.1f}bps".replace("+-", "-")
    return [
        {"id": "lead", "label": "LEAD", "value": lead_txt, "pct": lead_pct, "hint": f"≥{min_lead:.0f}bps"},
        {"id": "band", "label": "BAND", "value": band_txt, "pct": band_pct, "hint": f"{lo * 100:.0f}–{hi * 100:.0f}¢"},
        {"id": "left", "label": "LEFT", "value": left_txt, "pct": left_pct, "hint": f"{min_left:.0f}–{max_left:.0f}s"},
        {"id": "ws", "label": "WS", "value": str(ws or "off"), "pct": _conn_pct(ws), "hint": "CLOB 盤"},
        {"id": "cl", "label": "CL", "value": str(cl or "off"), "pct": _conn_pct(cl), "hint": "Chainlink"},
    ]


def _prefer_live_slot(prev: dict | None, row: dict) -> dict:
    """Open-window skip beats a later 未開窗 print for the same coin."""
    if prev is None:
        return row
    prev_fut = _is_future_listing(prev.get("reason"))
    nxt_fut = _is_future_listing(row.get("reason"))
    if prev_fut and not nxt_fut:
        return row
    if not prev_fut and nxt_fut:
        return prev
    return row


def _live_tape_rows(rt, n: int | None = None) -> list[dict]:
    rows = [
        r
        for r in (getattr(rt, "wall_tape", None) or [])
        if not _is_future_listing(r.get("reason"))
    ]
    if n is None:
        return rows
    return rows[-n:]


def _slots(rt, board: dict) -> list[dict]:
    assets = [str(a) for a in hunt_assets(rt.settings()) if str(a).strip()]
    latest: dict[str, dict] = {}
    for row in getattr(rt, "wall_tape", None) or []:
        asset = str(row.get("asset") or "")
        if not asset:
            continue
        latest[asset] = _prefer_live_slot(latest.get(asset), row)
    inv_by_asset: dict[str, dict] = {}
    for row in _mode_inv(rt):
        parsed = parse_window(str(row.get("slug") or ""))
        if parsed and parsed.asset not in inv_by_asset:
            inv_by_asset[parsed.asset] = row
    out = []
    for asset in assets:
        inv = inv_by_asset.get(asset)
        tape = latest.get(asset)
        if inv:
            status = "LIVE"
        elif tape and tape.get("ok"):
            status = "PASS"
        elif tape:
            status = "SKIP"
        else:
            status = "SCANNING"
        src = tape or {}
        out.append(
            {
                "asset": asset,
                "status": status,
                "slug": (inv or {}).get("slug") or src.get("slug") or "",
                "ask": src.get("ask"),
                "lead_bps": src.get("lead_bps"),
                "left": src.get("left"),
                "reason": src.get("reason") or "",
                "reason_zh": src.get("reason_zh") or "",
                "cost": None if inv is None else round(float(inv.get("cost") or 0), 2),
                "side": src.get("side"),
            }
        )
    return out


def _pipeline(rt, last: dict, tape: dict) -> list[dict]:
    skips = tape.get("twap_skips") or {}
    skip_n = int(sum(int(v or 0) for v in skips.values())) if isinstance(skips, dict) else 0
    markets = int(last.get("markets") or 0)
    return [
        {"id": "hunt", "label": "HUNT", "hint": "掃窗", "n": markets, "sub": f"WS {tape.get('ws_pairs') or 0}"},
        {"id": "gate", "label": "GATE", "hint": "TWAP 閘", "n": skip_n, "sub": "skip"},
        {"id": "book", "label": "BOOK", "hint": "盤口", "n": int(tape.get("ws_pairs") or 0), "sub": f"stale {tape.get('stale_pairs') or 0}"},
        {"id": "fak", "label": "FAK", "hint": "落單", "n": int(last.get("fills") or 0), "sub": f"殺 {last.get('fok_kills') or 0}"},
        {"id": "scratch", "label": "SCRATCH", "hint": "弱倉", "n": int(last.get("rescues") or 0), "sub": "dump"},
    ]


def _journal(rt, board: dict) -> list[dict]:
    live = board.get("mode") == "live"
    rows: list[dict] = []
    for t in rt.store.recent_trades(24):
        if t.get("status") in NOISE_TRADE:
            continue
        if live and t.get("mode") == "paper":
            continue
        status = str(t.get("status") or "")
        net = float(t.get("net") or 0)
        rows.append(
            {
                "ts": t.get("ts"),
                "kind": "trade",
                "ok": status in {"filled", "paper_filled", "redeemed", "paper_settled"} and net >= 0,
                "text": f"{STATUS_ZH.get(status, status)} {t.get('slug') or ''} {format_signed_usd(net)}",
            }
        )
    started = float(getattr(rt, "started_at", 0) or 0)
    for e in rt.store.recent_events(30):
        if started and float(e.get("ts") or 0) < started - 30:
            continue
        msg = str(e.get("message") or "")
        if _is_redeem_wait_log(msg) or _is_dump_dust_log(msg):
            continue
        rows.append(
            {
                "ts": e.get("ts"),
                "kind": str(e.get("level") or "info"),
                "ok": str(e.get("level") or "") != "warn",
                "text": msg[:180],
            }
        )
    rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
    return rows[:24]


def operator_wall(rt, board: dict) -> dict[str, Any]:
    """Watch payload. Money/mode always come from operator_board."""
    b = board
    last = rt.last_loop or {}
    tape = last.get("tape") or {}
    gate = tape.get("twap_gate") or {}
    s = rt.settings()
    raw_tape = _live_tape_rows(rt, 24)
    view_tape = list(reversed(raw_tape))
    perf = performance_today(rt)
    return {
        "board": {
            "mode": b.get("mode"),
            "state": b.get("state"),
            "cash_label": b.get("cash_label"),
            "cash": b.get("cash"),
            "today_pnl": b.get("today_pnl"),
            "stake": b.get("stake"),
            "open_n": b.get("open_n"),
            "open_cost": b.get("open_cost"),
            "ws": b.get("ws"),
            "chainlink": b.get("chainlink"),
            "halted": b.get("halted"),
            "notes": list(b.get("notes") or []),
            "rev": b.get("rev"),
            "leftover_paper_n": b.get("leftover_paper_n"),
            "equity": b.get("equity"),
            "starting": b.get("starting"),
            "hit_rate": b.get("hit_rate"),
            "hit_wins": b.get("hit_wins"),
            "hit_losses": b.get("hit_losses"),
            "hit_held": b.get("hit_held"),
            "scratch_n": b.get("scratch_n"),
            "hit_label": b.get("hit_label"),
        },
        "curve": perf,
        "gate": gate,
        "gauges": _gauges(rt, b, gate, s),
        "pipeline": _pipeline(rt, last, tape),
        "slots": _slots(rt, b),
        "tape": view_tape,
        "log": _journal(rt, b),
        "skips": tape.get("twap_skips") or {},
        "qualified": next((row for row in view_tape if row.get("ok")), None),
    }


def format_tape_lines(rt, n: int = 6) -> list[str]:
    rows = list(reversed(_live_tape_rows(rt, n)))
    lines = []
    for row in rows:
        stamp = format_log_ts(row.get("ts"))
        tag = "PASS" if row.get("ok") else "SKIP"
        slug = str(row.get("slug") or "")[:28]
        why = row.get("reason_zh") or reason_zh(row.get("reason"))
        extra = ""
        if row.get("lead_bps") is not None:
            extra = f" {float(row['lead_bps']):+.1f}bps".replace("+-", "-")
        elif row.get("ask") is not None:
            extra = f" {float(row['ask']):.2f}"
        lines.append(f"{stamp} {tag} {slug} {why}{extra}".strip())
    return lines
