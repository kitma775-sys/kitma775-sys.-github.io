from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import SETTING_STEPS, format_leg_prices, is_directional_inventory, is_favorite_inventory, live_keys_ready
from app.geo import telegram_line
from app.runtime import Runtime
from app.twap import hunt_assets, hunt_horizons
from app.universe import DEFAULT_ASSETS

TG_MAX = 3900


def _strategy_label(s: dict) -> str:
    lo = float(s.get("twap_min_price") or 0.45)
    hi = float(s.get("twap_max_price") or 0.55)
    assets = [str(a).upper() for a in hunt_assets(s) if str(a).strip()]
    names = "+".join(assets) or "BTC+ETH"
    hz = "/".join(hunt_horizons(s) or ("5m",))
    return f"TWAP 中間價 {lo:.2f}–{hi:.2f} {names} {hz}"


def _rev_blurb(s: dict) -> str:
    rev = int(s.get("strategy_rev") or 0)
    return (
        f"Rev {rev}：Chainlink 60s TWAP vs 窗開價，只做 5m／15m up/down。"
        "45–55¢，剩餘 12–280s，lead ≥6 bps，弱倉 scratch。"
        "全幣各開一條 Chainlink socket。CLOB 只訂有 PTB 嘅獵窗＋持倉，最多 16 token。"
        "1 小時 Binance 收線盤永遠唔入場。唔做 YES+NO 互補，唔做大熱 97–98。"
        "FORCE_PAPER／兩步確認仍然鎖真錢。"
    )


NOISE_TRADE = {"paper_leg_fill", "paper_resting", "resting"}
STATUS_ZH = {
    "paper_filled": "紙盤成交",
    "paper_hedged": "單邊對沖",
    "paper_dumped": "單邊出貨",
    "dumped": "單邊出貨",
    "paper_settled": "結算",
    "redeemed": "redeem 取回",
    "paper_fok_killed": "FOK殺單",
    "fok_killed": "FOK殺單",
    "filled": "成交",
    "cancelled": "已撤",
}
KIND_ZH = {"taker": "taker", "maker": "掛單", "settle": "結算"}

TOGGLES = {
    "auto_execute": ("全自動落單", "開咗就唔會逐單問你"),
    "auto_redeem": ("自動 redeem", "完場官方結果後自動取回：紙盤入帳，實盤 redeemPositions"),
    "notify_signals": ("成交通知", "有紙盤／實盤動作即時彈"),
    "notify_rejects": ("跳過通知", "風控擋咗都會話你知（會嘈）"),
    "taker_fok": ("FOK 確認", "250ms 後 FAK；再等 RTT 重走簿，唔 requote。殺單 0.4s 可再試"),
}


def _clip(text: str) -> str:
    if len(text) <= TG_MAX:
        return text
    return text[: TG_MAX - 12] + "\n…（過長）"


def _fmt_ts(ts: float | None) -> str:
    try:
        return time.strftime("%H:%M:%S", time.gmtime(float(ts))) + "Z"
    except (TypeError, ValueError, OSError):
        return ""


async def _safe_edit(q, text: str, reply_markup=None) -> None:
    body = _clip(text)
    try:
        edit = q.edit_message_text
        await edit(body, reply_markup=reply_markup)
    except BadRequest as exc:
        if "not modified" in str(exc).lower():
            return
        try:
            if q.message is not None:
                await q.message.reply_text(body, reply_markup=reply_markup)
        except Exception:
            pass


def _owner(update: Update, rt: Runtime) -> bool:
    user = update.effective_user
    if user is None:
        return False
    env_owner = rt.env.telegram_owner_id
    stored = rt.store.owner_id()
    if env_owner is not None:
        return user.id == env_owner
    if stored is None:
        rt.store.set_owner_id(user.id)
        return True
    return user.id == stored


def dashboard_open_url(rt: Runtime) -> str | None:
    """Owner-only HTTPS link with dashboard token. Missing base or token → no button."""
    base = str(rt.env.dashboard_public_url or "").strip().rstrip("/")
    token = str(rt.env.dashboard_token or "").strip()
    if not base or not token:
        return None
    if not (base.startswith("https://") or base.startswith("http://")):
        return None
    return f"{base}/?t={quote(token, safe='')}"


def home_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    run = "⏸ 暫停" if s.get("engine_running") else "▶️ 繼續跑"
    run_cb = "pause" if s.get("engine_running") else "resume"
    rows: list[list[InlineKeyboardButton]] = []
    dash = dashboard_open_url(rt)
    if dash:
        rows.append([InlineKeyboardButton("🖥 開 Dashboard", url=dash)])
    rows.extend(
        [
            [InlineKeyboardButton("📊 而家狀況", callback_data="status"), InlineKeyboardButton("📦 倉位", callback_data="pos")],
            [InlineKeyboardButton(run, callback_data=run_cb), InlineKeyboardButton("📜 最近紀錄", callback_data="log")],
            [InlineKeyboardButton("💵 紙盤本金", callback_data="bank"), InlineKeyboardButton("♻️ 重置紙盤", callback_data="reset1")],
            [InlineKeyboardButton("⚙️ 高階設定", callback_data="set"), InlineKeyboardButton("🧪／🔴 盤口模式", callback_data="mode")],
            [InlineKeyboardButton("🆘 緊急停機", callback_data="kill")],
        ]
    )
    if rt.circuit_tripped():
        rows.insert(
            1 if dash else 0,
            [InlineKeyboardButton("🧊 解除今日熔斷（今日PnL重新計）", callback_data="circuit1")],
        )
    return InlineKeyboardMarkup(rows)


def mode_kb(rt: Runtime) -> InlineKeyboardMarkup:
    mode = rt.mode()
    amt = rt.paper_bankroll()
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 紙盤（而家）" if mode == "paper" else "轉返紙盤", callback_data="paper")],
            [InlineKeyboardButton("🔴 轉實盤（要確認）", callback_data="live1")],
            [InlineKeyboardButton(f"♻️ 重置紙盤 ${amt:.0f}", callback_data="reset1")],
            [InlineKeyboardButton("💵 改紙盤本金", callback_data="bank")],
            [InlineKeyboardButton("↩️ 返主頁", callback_data="home")],
        ]
    )


def bank_kb(rt: Runtime) -> InlineKeyboardMarkup:
    amt = rt.paper_bankroll()
    presets = [100, 250, 500, 1000, 2000, 5000, 10000]
    rows = []
    row = []
    for n in presets:
        mark = "✅ " if abs(amt - n) < 0.01 else ""
        row.append(InlineKeyboardButton(f"{mark}${n}", callback_data=f"bank:{n}"))
        if len(row) == 4:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append(
        [
            InlineKeyboardButton("➖ $100", callback_data="bankdec"),
            InlineKeyboardButton(f"本金 ${amt:.0f}", callback_data="bank"),
            InlineKeyboardButton("➕ $100", callback_data="bankinc"),
        ]
    )
    rows.append([InlineKeyboardButton(f"♻️ 重置紙盤到 ${amt:.0f}", callback_data="reset1")])
    rows.append([InlineKeyboardButton("↩️ 返主頁", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def _signed(n: float) -> str:
    return f"{'+' if n >= 0 else ''}${n:.2f}"


def _paper_block(rt: Runtime) -> str:
    p = rt.store.paper_state()
    planned = rt.paper_bankroll()
    extra = ""
    if abs(planned - float(p["starting"])) > 0.009:
        extra = f" · 下次重置 ${planned:.0f}"
    return (
        f"本金 ${p['starting']:.2f} · 現金 ${p['cash']:.2f} · 凍結 ${p.get('reserved') or 0:.2f} · 權益 ${p['equity']:.2f}{extra}\n"
        f"累計 PnL {_signed(p['total_pnl'])} · 今日 {_signed(p['today_pnl'])} · 掛單 {int(p.get('resting') or 0)}"
    )


def bank_text(rt: Runtime) -> str:
    p = rt.store.paper_state()
    amt = rt.paper_bankroll()
    return (
        "💵 紙盤本金\n"
        f"下次重置會用：${amt:.0f}\n"
        f"帳本而家：本金 ${p['starting']:.2f} · 現金 ${p['cash']:.2f} · 權益 ${p['equity']:.2f}\n"
        f"累計 PnL {_signed(p['total_pnl'])}\n\n"
        "改金額只係記住下次重置用幾多，唔會即刻清倉。\n"
        "要套用新本金，撳重置（會清倉同掛單，成交紀錄會留）。"
    )


def settings_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    rows = [
        [InlineKeyboardButton(f"策略：{_strategy_label(s)}（鎖定）", callback_data="set")],
    ]
    for key, (label, _hint) in TOGGLES.items():
        on = bool(s.get(key))
        rows.append([InlineKeyboardButton(f"{'✅' if on else '⬜️'} {label}", callback_data=f"tog:{key}")])
    for key, (_step, _lo, _hi) in SETTING_STEPS.items():
        val = s.get(key)
        if isinstance(val, float):
            shown = f"{val:.3g}"
        else:
            shown = str(val)
        rows.append(
            [
                InlineKeyboardButton("➖", callback_data=f"dec:{key}"),
                InlineKeyboardButton(f"{_label(key)} {shown}", callback_data="set"),
                InlineKeyboardButton("➕", callback_data=f"inc:{key}"),
            ]
        )
    rows.append([InlineKeyboardButton("幣種過濾", callback_data="assets")])
    rows.append([InlineKeyboardButton("週期 5M／15M／1H", callback_data="tags")])
    rows.append([InlineKeyboardButton("↩️ 返主頁", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def assets_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    cur = set(s.get("assets") or [])
    coins = list(DEFAULT_ASSETS)
    rows = []
    row = []
    for c in coins:
        mark = "✅" if c in cur else "⬜️"
        row.append(InlineKeyboardButton(f"{mark} {c.upper()}", callback_data=f"asset:{c}"))
        if len(row) == 3:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("↩️ 返設定", callback_data="set")])
    return InlineKeyboardMarkup(rows)


def tags_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    cur = set(s.get("tags") or [s.get("tag") or "15M"])
    rows = []
    for tag, hint in (
        ("5M", "5分鐘升跌 · TWAP"),
        ("15M", "15分鐘升跌 · TWAP"),
        ("1H", "1小時 Binance 收線 · 只掃唔入場"),
    ):
        mark = "✅" if tag in cur else "⬜️"
        rows.append([InlineKeyboardButton(f"{mark} {tag} {hint}", callback_data=f"tag:{tag}")])
    rows.append([InlineKeyboardButton("↩️ 返設定", callback_data="set")])
    return InlineKeyboardMarkup(rows)


def _label(key: str) -> str:
    return {
        "max_usd_per_trade": "單筆上限$",
        "min_shares": "最少股數",
        "daily_loss_limit_usd": "日虧熔斷$",
        "max_open_markets": "最多市場",
        "poll_seconds": "掃描秒",
        "paper_slip_ticks": "紙盤滑點tick",
        "paper_starting_cash": "紙盤本金$",
        "scan_limit": "每圈最多盤",
        "twap_max_left": "TWAP剩餘上限s",
        "twap_min_lead_bps": "TWAP最少lead bps",
        "clob_rtt_ms": "CLOB RTT ms",
    }.get(key, key)


def home_text(rt: Runtime) -> str:
    s = rt.settings()
    p = rt.store.paper_state()
    st = rt.store.stats()
    limit = float(s.get("daily_loss_limit_usd") or 0)
    if s.get("killed"):
        state = "🆘 已緊急停機"
    elif not s.get("engine_running"):
        state = "⏸ 暫停緊"
    elif limit > 0 and p["today_pnl"] <= -abs(limit):
        state = "🧊 日虧熔斷（停新倉）"
    else:
        state = "🟢 全自動運行中" if s.get("auto_execute") else "🟡 只掃描，唔落單"
    mode = "🧪 紙盤" if rt.mode() == "paper" else "🔴 實盤"
    keys = "匙已備" if live_keys_ready(rt.env) else "未交實盤匙"
    if rt.env.force_paper:
        keys = "FORCE_PAPER 鎖住實盤"
    geo_line = telegram_line(rt.geo)
    last = rt.last_loop or {}
    return (
        f"🏄 衝浪套利 Bot\n"
        f"{state} · {mode}\n"
        f"{_paper_block(rt)}\n"
        f"今日掃描 {st['scans_24h']} · 成交 {st['trades_24h']}"
        + (f" · 對沖 {st['hedges_24h']}" if st.get("hedges_24h") else "")
        + "\n"
        f"開倉市場 {st['open_markets']} · {keys}\n"
        f"上一圈：{last.get('status','—')} 市場{last.get('markets','—')} 信號{last.get('signals','—')} 成交{last.get('fills','—')} WS {last.get('ws_status') or rt.ws_status}"
        f"{_tape_line(last)}"
        f"{geo_line}\n\n"
        + _rev_blurb(s)
        + "\n未交匙／FORCE_PAPER 開住永遠紙盤。真金要撳兩次確認。"
    )


def _tape_line(last: dict) -> str:
    tape = last.get("tape") or {}
    if not tape.get("n"):
        bits = []
        if tape.get("ws_status"):
            bits.append(f"WS {tape['ws_status']}")
        if tape.get("chainlink_status"):
            bits.append(f"CL {tape['chainlink_status']}")
        if tape.get("stale_pairs"):
            bits.append(f"過期 {int(tape['stale_pairs'])}")
        if tape.get("empty_ask_legs"):
            bits.append(f"單邊空簿 {int(tape['empty_ask_legs'])}")
        err = tape.get("book_errors")
        if err:
            bits.append(f"拉簿失敗 {err}")
        if bits:
            return "\n盤口：" + " · ".join(bits)
        return ""
    bits = [f"盤口 {tape['n']} 盤"]
    if tape.get("ws_status"):
        bits.append(f"WS {tape['ws_status']}")
    if tape.get("chainlink_status"):
        bits.append(f"CL {tape['chainlink_status']}")
    gate = tape.get("twap_gate") or {}
    if gate.get("reason"):
        lead = gate.get("lead_bps")
        ask = gate.get("ask")
        left = gate.get("left")
        bits.append(
            "TWAP "
            + (f"{int(left)}s " if left is not None else "")
            + (f"lead {lead:+.1f}bps " if lead is not None else "")
            + (f"ask {ask} " if ask is not None else "")
            + str(gate.get("reason"))
        )
    if tape.get("ws_pairs") is not None:
        bits.append(f"WS盤 {int(tape.get('ws_pairs') or 0)}")
    if tape.get("http_pairs"):
        bits.append(f"HTTP {int(tape['http_pairs'])}")
    if tape.get("stale_pairs"):
        bits.append(f"過期 {int(tape['stale_pairs'])}")
    if tape.get("empty_ask_legs"):
        bits.append(f"單邊空簿 {int(tape['empty_ask_legs'])}")
    if tape.get("taker_fok") and (tape.get("snapshot_signals") or tape.get("fok_kills") or tape.get("fok_fills")):
        bits.append(
            f"FOK 影{int(tape.get('snapshot_signals') or 0)}/"
            f"成{int(tape.get('fok_fills') or 0)}/"
            f"殺{int(tape.get('fok_kills') or 0)}"
        )
    if tape.get("nearest_s") is not None:
        slug = str(tape.get("nearest_slug") or "")[:28]
        bits.append(f"最近 {int(tape['nearest_s'])}s {slug}".rstrip())
    names = [str(x) for x in (tape.get("slugs") or []) if x][:4]
    if names:
        bits.append("掃 " + ", ".join(names))
    extra = f" · 拉簿失敗 {tape['book_errors']}" if tape.get("book_errors") else ""
    return "\n" + " · ".join(bits) + extra


def _status_text(rt: Runtime) -> str:
    s = rt.settings()
    assets = ", ".join(a.upper() for a in (s.get("assets") or []))
    return (
        home_text(rt)
        + "\n\n高階參數\n"
        + f"策略 rev {int(s.get('strategy_rev') or 0)} · WS {rt.ws_status} · Chainlink {rt.chainlink_status}\n"
        + f"{_strategy_label(s)}（鎖定，唔做互補／大熱）\n"
        + f"單筆 ≤ ${s['max_usd_per_trade']} · 日虧熔斷 ${s['daily_loss_limit_usd']} · 掃描 {s['poll_seconds']}s\n"
        + f"TWAP lead≥{float(s.get('twap_min_lead_bps') or 6):.0f}bps · 剩餘 {float(s.get('twap_min_left') or 12):.0f}–{float(s.get('twap_max_left') or 280):.0f}s · 缺口 ≥{float(s.get('twap_min_edge') or 0.04):.2f}\n"
        + f"FOK {'開' if s.get('taker_fok', True) else '關'} · RTT {float(s.get('clob_rtt_ms') or 150):.0f}ms · redeem {'開' if s.get('auto_redeem', True) else '關'}\n"
        + f"週期 {', '.join(s.get('tags') or [s.get('tag') or '5M'])} · 每圈 ≤ {s.get('scan_limit') or 16}\n"
        + f"幣：{assets or '全部'}（TWAP 入場＝有 Chainlink 嘅 5m／15m）"
    )


def _pos_text(rt: Runtime) -> str:
    inv = rt.store.inventory_open()
    rest = rt.store.resting_open()
    lines = [_paper_block(rt), "", "📦 倉位"]
    if rest:
        lines.append("掛單（未碰到盤口唔入 PnL）")
        for row in rest[:10]:
            up_f = "✓" if row.get("up_filled") else "…"
            dn_f = "✓" if row.get("down_filled") else "…"
            lines.append(
                f"{row.get('slug') or row['condition_id'][:8]}  {format_leg_prices(row['up_price'], row['down_price'], leg=(row.get('payload') or {}).get('leg'))} × {row['shares']:.1f}"
                f"\n  Up {up_f} · Down {dn_f} · 鎖 ${float(row.get('reserved') or 0):.2f}"
            )
    if not inv and not rest:
        lines.append("而家無倉、無掛單。")
        return "\n".join(lines)
    for row in inv[:15]:
        kind = str(row.get("kind") or "pair")
        tag = " TWAP" if str(kind).startswith("twap") else (" 大熱" if is_favorite_inventory(kind) else "")
        cost = float(row.get("cost") or 0)
        cost_txt = f" · 成本 ${cost:.2f}" if is_directional_inventory(kind) and cost > 0 else ""
        lines.append(
            f"{row['slug'] or row['condition_id'][:8]}{tag}\n  Up {row['up']:.1f} · Down {row['down']:.1f}{cost_txt}"
        )
    return "\n".join(lines)


def _log_text(rt: Runtime) -> str:
    trades = [t for t in rt.store.recent_trades(24) if t.get("status") not in NOISE_TRADE][:8]
    if not trades:
        return "近期無成交／對沖／結算。掛單同單邊碰到唔再當成問題單顯示。"
    lines = ["📜 最近紀錄（隱藏掛單／單邊碰到）"]
    for t in trades:
        stamp = _fmt_ts(t.get("ts"))
        status = STATUS_ZH.get(t["status"], t["status"])
        kind = KIND_ZH.get(t["kind"], t["kind"])
        net = float(t.get("net") or 0)
        sign = "+" if net >= 0 else ""
        lines.append(
            f"{stamp} {status} · {kind}\n"
            f"{t['slug']}\n"
            f"{format_leg_prices(t['up_price'], t['down_price'], leg=(t.get('payload') or {}).get('leg'))} × {t['shares']}  {sign}${net:.2f}"
        )
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt: Runtime = context.application.bot_data["rt"]
    if not _owner(update, rt):
        await update.effective_message.reply_text("呢個 bot 已經有主人，唔好意思。")
        return
    await update.effective_message.reply_text(_clip(home_text(rt)), reply_markup=home_kb(rt))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt: Runtime = context.application.bot_data["rt"]
    q = update.callback_query
    if q is None:
        return
    if not _owner(update, rt):
        await q.answer("唔係主人", show_alert=True)
        return
    data = q.data or ""
    try:
        await _handle_callback(rt, q, data)
    except Exception as exc:
        rt.store.add_event("warn", f"tg callback {data}: {type(exc).__name__}: {exc}"[:220])
        try:
            await q.answer("Bot 出錯，試下 /start", show_alert=True)
        except Exception:
            pass


async def _handle_callback(rt: Runtime, q, data: str) -> None:
    s = rt.settings()

    if data == "home":
        await q.answer()
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "status":
        await q.answer()
        await _safe_edit(q, _status_text(rt), reply_markup=home_kb(rt))
        return
    if data == "pos":
        await q.answer()
        await _safe_edit(q, _pos_text(rt), reply_markup=home_kb(rt))
        return
    if data == "log":
        await q.answer()
        await _safe_edit(q, _log_text(rt), reply_markup=home_kb(rt))
        return
    if data == "pause":
        rt.store.patch_settings(engine_running=False)
        await q.answer("已暫停")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "resume":
        rt.store.patch_settings(engine_running=True, killed=False)
        await q.answer("繼續跑")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "circuit1":
        if not rt.circuit_tripped():
            await q.answer("而家冇熔斷")
            await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
            return
        p = rt.store.paper_state()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("確認：今日 PnL 由 0 再計", callback_data="circuit2")],
                [InlineKeyboardButton("算吧", callback_data="home")],
            ]
        )
        await q.answer()
        await _safe_edit(
            q,
            "解除今日熔斷唔會清倉、唔會改本金。\n"
            f"而家權益 ${p['equity']:.2f} · 今日 {_signed(p['today_pnl'])}。\n"
            "撳確認之後今日 PnL 由 0 再計，會再開新倉。每盤仍然 ≤ 單筆上限。",
            reply_markup=kb,
        )
        return
    if data == "circuit2":
        book = rt.store.reset_today_pnl()
        rt._circuit_latch = False
        rt.store.add_event("warn", f"cleared daily circuit equity=${book['equity']:.2f} today=${book['today_pnl']:.2f}")
        await q.answer("已解除熔斷")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "kill":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("確認停機，全部停", callback_data="kill2")],
                [InlineKeyboardButton("算吧", callback_data="home")],
            ]
        )
        await q.answer()
        await _safe_edit(q, "緊急停機會停掃描同落單。確定？", reply_markup=kb)
        return
    if data == "kill2":
        rt.store.patch_settings(killed=True, engine_running=False, live_trading=False)
        n = rt.store.cancel_all_resting("kill")
        live_n = 0
        try:
            live_n = await rt.broker().cancel_open_orders()
        except Exception as exc:
            rt.store.add_event("warn", f"tg kill cancel_live {type(exc).__name__}: {exc}"[:180])
        rt.store.add_event("warn", f"kill switch cancelled_resting={n} live={live_n}")
        await q.answer("已停機")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "mode":
        await q.answer()
        await _safe_edit(q, 
            f"而家：{'紙盤' if rt.mode()=='paper' else '實盤'}\n"
            f"策略鎖定 Chainlink 5m／15m TWAP（1H 唔入場）。紙盤用同一套 CLOB FAK dry-run。下次重置本金 ${rt.paper_bankroll():.0f}。\n"
            "實盤要環境變數有 POLYMARKET_PRIVATE_KEY，關 FORCE_PAPER，再撳兩次確認。",
            reply_markup=mode_kb(rt),
        )
        return
    if data == "paper":
        rt.store.patch_settings(live_trading=False)
        await q.answer("返紙盤")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "bank":
        await q.answer()
        await _safe_edit(q, bank_text(rt), reply_markup=bank_kb(rt))
        return
    if data.startswith("bank:") or data in {"bankinc", "bankdec"}:
        from app.config import clamp_paper_cash
        cur = rt.paper_bankroll()
        if data == "bankinc":
            nxt = clamp_paper_cash(cur + 100)
        elif data == "bankdec":
            nxt = clamp_paper_cash(cur - 100)
        else:
            nxt = clamp_paper_cash(float(data.split(":", 1)[1]))
        rt.store.patch_settings(paper_starting_cash=nxt)
        await q.answer(f"下次重置 ${nxt:.0f}")
        await _safe_edit(q, bank_text(rt), reply_markup=bank_kb(rt))
        return
    if data == "reset1":
        amt = rt.paper_bankroll()
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton(f"確認重置為 ${amt:.0f}，清倉", callback_data="reset2")],
                [InlineKeyboardButton("算吧", callback_data="bank")],
            ]
        )
        await q.answer()
        await _safe_edit(q, 
            f"會清紙盤倉位同掛單，現金同權益打返 ${amt:.0f}。成交紀錄會留低。確定？",
            reply_markup=kb,
        )
        return
    if data == "reset2":
        amt = rt.paper_bankroll()
        rt.store.patch_settings(paper_starting_cash=amt)
        book = rt.store.reset_paper(amt)
        rt.store.add_event("warn", f"paper reset starting=${book['starting']:.2f}")
        await q.answer("紙盤已重置")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "live1":
        if rt.env.force_paper:
            await q.answer("FORCE_PAPER 開緊", show_alert=True)
            return
        if not live_keys_ready(rt.env):
            await q.answer("未設定 POLYMARKET_PRIVATE_KEY", show_alert=True)
            return
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("我明白，轉實盤", callback_data="live2")],
                [InlineKeyboardButton("返去", callback_data="mode")],
            ]
        )
        await q.answer()
        await _safe_edit(q, 
            "實盤會用你把匙簽名落單。\n"
            "而家只做 Chainlink 5m／15m TWAP。1 小時 Binance 盤唔入場。紙盤同真錢同一套 CLOB FAK。\n"
            "紙盤係真錢 dry-run，但 queue／部分成交／延遲仍然會差一截。\n"
            "全自動模式下唔會逐單確認。FORCE_PAPER 開住永遠紙盤。確定轉？",
            reply_markup=kb,
        )
        return
    if data == "live2":
        if rt.env.force_paper:
            await q.answer("FORCE_PAPER 開緊，未轉實盤", show_alert=True)
            await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
            return
        if not live_keys_ready(rt.env):
            await q.answer("未設定 POLYMARKET_PRIVATE_KEY", show_alert=True)
            return
        rt.store.patch_settings(live_trading=True, killed=False, engine_running=True)
        rt.store.add_event("warn", "live trading enabled")
        await q.answer("已轉實盤")
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "set":
        await q.answer()
        await _safe_edit(
            q,
            "高階設定。策略鎖定 Chainlink 5m／15m TWAP，唔會切去互補或大熱。\n"
            "加減只改倉位／風控／掃描。1 小時週期只掃、永遠唔抬。",
            reply_markup=settings_kb(rt),
        )
        return
    if data in {"smode", "fwin", "fdir"}:
        await q.answer("策略已鎖定 TWAP", show_alert=True)
        await _safe_edit(
            q,
            "高階設定。策略鎖定 Chainlink 5m／15m TWAP，唔會切去互補或大熱。",
            reply_markup=settings_kb(rt),
        )
        return
    if data == "assets":
        await q.answer()
        await _safe_edit(q, "揀要掃嘅幣。有 Chainlink TWAP-60 嘅 5m／15m 會入場。最少留一個。", reply_markup=assets_kb(rt))
        return
    if data.startswith("asset:"):
        coin = data.split(":", 1)[1]
        cur = list(s.get("assets") or [])
        if coin in cur:
            if len(cur) == 1:
                await q.answer("至少留一種", show_alert=True)
                return
            cur.remove(coin)
        else:
            cur.append(coin)
        rt.store.patch_settings(assets=cur)
        await q.answer()
        await _safe_edit(q, "揀要掃嘅幣。有 Chainlink TWAP-60 嘅 5m／15m 會入場。最少留一個。", reply_markup=assets_kb(rt))
        return
    if data == "tags":
        await q.answer()
        await _safe_edit(q, "揀要掃嘅完場週期。5M／15M 會 TWAP；1H 係 Binance 收線，只掃唔入場。最少留一個。", reply_markup=tags_kb(rt))
        return
    if data.startswith("tag:"):
        tag = data.split(":", 1)[1]
        cur = list(s.get("tags") or [s.get("tag") or "5M"])
        if tag in cur:
            if len(cur) == 1:
                await q.answer("至少留一種週期", show_alert=True)
                return
            cur.remove(tag)
        else:
            cur.append(tag)
        rt.store.patch_settings(tags=cur, tag=cur[0])
        await q.answer()
        await _safe_edit(q, "揀要掃嘅完場週期。5M／15M 會 TWAP；1H 係 Binance 收線，只掃唔入場。最少留一個。", reply_markup=tags_kb(rt))
        return
    if data.startswith("tog:"):
        key = data.split(":", 1)[1]
        if key in TOGGLES:
            rt.store.patch_settings(**{key: not bool(s.get(key))})
        await q.answer("已更新")
        await _safe_edit(q, "高階設定。策略鎖定 Chainlink 5m／15m TWAP。", reply_markup=settings_kb(rt))
        return
    if data.startswith("inc:") or data.startswith("dec:"):
        key = data.split(":", 1)[1]
        if key not in SETTING_STEPS:
            await q.answer()
            return
        step, lo, hi = SETTING_STEPS[key]
        cur = float(s.get(key) or 0)
        nxt = cur + step if data.startswith("inc:") else cur - step
        nxt = min(hi, max(lo, nxt))
        if key in {"max_open_markets", "paper_slip_ticks", "paper_starting_cash", "scan_limit"}:
            rt.store.patch_settings(**{key: int(round(nxt))})
        else:
            rt.store.patch_settings(**{key: round(nxt, 4)})
        await q.answer()
        await _safe_edit(q, "高階設定。策略鎖定 Chainlink 5m／15m TWAP。", reply_markup=settings_kb(rt))
        return
    await q.answer()


async def run_telegram(rt: Runtime) -> None:
    token = rt.env.telegram_token
    if not token:
        rt.store.add_event("warn", "無 TELEGRAM_BOT_TOKEN，跳過 Telegram")
        while True:
            await asyncio_sleep_forever()
    application = Application.builder().token(token).build()
    application.bot_data["rt"] = rt
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CallbackQueryHandler(on_callback))
    await application.initialize()
    await application.start()
    await application.updater.start_polling(drop_pending_updates=True)
    rt.store.add_event("info", "telegram polling")
    owner = rt.env.telegram_owner_id or rt.store.owner_id()
    if owner is not None:
        try:
            await application.bot.send_message(
                chat_id=owner,
                text=_clip(home_text(rt) + f"\n\n紙盤下次重置本金 ${rt.paper_bankroll():.0f}。現金／權益／PnL 睇上面同 dashboard。"),
                reply_markup=home_kb(rt),
            )
        except Exception:
            pass
    try:
        while True:
            note = await rt.notices.get()
            owner = rt.env.telegram_owner_id or rt.store.owner_id()
            if owner is None:
                continue
            try:
                await application.bot.send_message(chat_id=owner, text=_clip(note["text"]), reply_markup=home_kb(rt))
            except Exception:
                pass
    finally:
        await application.updater.stop()
        await application.stop()
        await application.shutdown()


async def asyncio_sleep_forever() -> None:
    import asyncio

    while True:
        await asyncio.sleep(3600)
