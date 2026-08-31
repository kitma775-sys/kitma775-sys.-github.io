from __future__ import annotations

import time
from urllib.parse import quote

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import BadRequest
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import LIVE_BLOCKER_ZH, SETTING_STEPS, TRADE_USD_STEPS, format_fill_headline, format_leg_prices, format_share_qty, is_directional_inventory, is_favorite_inventory, live_keys_ready, live_switch_blockers, nudge_trade_usd
from app.runtime import Runtime, arm_live_wallet, leftover_paper_inventory, mode_inventory, operator_board, refresh_live_usdc
from app.twap import hunt_assets, hunt_horizons
from app.universe import DEFAULT_ASSETS
from app.wall import format_tape_lines

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
        f"Rev {rev}：Chainlink 60s TWAP vs 窗開價，只做 5 分鐘 up/down，多幣種。"
        "45–55¢，剩餘 12–280s，lead ≥6 bps，弱倉 scratch。"
        "全幣各開一條 Chainlink socket。CLOB 兩條 socket 各最多 8 token，開盤前 45 秒預熱下一窗；仙價未到預熱唔甩槽，避免 WS 狂重連；唔做 initial_dump。"
        "15 分鐘同 5 分鐘搶槽，已砍。1 小時 Binance 收線盤永遠唔入場。唔做 YES+NO 互補，唔做大熱 97–98。"
        "FORCE_PAPER／兩步確認仍然鎖真錢。開實盤前要 Zeabur 填 POLYMARKET_PRIVATE_KEY、關 FORCE_PAPER，再撳兩次。"
        "CLOB 503／trading is disabled 係 Polymarket 全站暫停，唔係錢包問題；只通知一次，交易所開返先再試。"
        "實盤唔再彈轉倉前嘅紙盤 redeem；舊紙單完場靜默入紙盤帳。"
        "主頁／而家狀況／Dashboard 跟盤口模式：實盤只睇可用 USDC 同實盤倉，紙盤只睇紙盤帳。"
        "Dashboard 係霓虹監察牆：電腦三欄、手機直版自動疊；掃描日誌同運行日誌跟 bot 同一份。"
    )


TG_SET_HINT = (
    "高階設定。策略鎖定 Chainlink 5 分鐘 TWAP（多幣種）。"
    "15 分鐘同 1 小時已砍：15m 搶 14 個 CLOB 槽而且冇獨立盤帶；1H 係 Binance 收線。"
    "唔會切去互補或大熱。"
    "單筆最低 $3：5 分鐘盤 CLOB 最少 5 股，45–55¢ 連 fee 約 $2.84，$2 買唔入。"
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
        ]
    )
    if rt.mode() == "paper":
        rows.append(
            [InlineKeyboardButton("💵 紙盤本金", callback_data="bank"), InlineKeyboardButton("♻️ 重置紙盤", callback_data="reset1")]
        )
    rows.extend(
        [
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
    """Paper ledger only. Live surfaces use operator_board() instead."""
    p = rt.store.paper_state()
    planned = rt.paper_bankroll()
    extra = ""
    if abs(planned - float(p["starting"])) > 0.009:
        extra = f" · 下次重置 ${planned:.0f}"
    return (
        f"本金 ${p['starting']:.2f} · 現金 ${p['cash']:.2f} · 凍結 ${p.get('reserved') or 0:.2f} · 權益 ${p['equity']:.2f}{extra}\n"
        f"累計 PnL {_signed(p['total_pnl'])} · 今日 {_signed(p['today_pnl'])} · 掛單 {int(p.get('resting') or 0)}"
    )


def _money_line(rt: Runtime) -> str:
    b = operator_board(rt)
    if b["mode"] == "live":
        usdc = "—" if b["cash"] is None else f"${b['cash']:.2f}"
        return f"可用 USDC {usdc} · 單筆 ${b['stake']:.0f} · 今日 {_signed(b['today_pnl'])}"
    return (
        f"現金 ${b['cash']:.2f} · 權益 ${b['equity']:.2f} · 今日 {_signed(b['today_pnl'])}\n"
        f"本金 ${b['starting']:.2f} · 單筆 ${b['stake']:.0f}"
    )


def settings_text(rt: Runtime) -> str:
    """Long strategy copy — shown once when opening 設定, not on every status tap."""
    return "\n".join(
        [
            "⚙️ 設定（改完即時生效）",
            "",
            _rev_blurb(rt.settings()),
            "",
            TG_SET_HINT,
            "加減只改倉位／風控／掃描。",
        ]
    )


def mode_text(rt: Runtime) -> str:
    b = operator_board(rt)
    if b["mode"] == "live":
        usdc = "—" if b["cash"] is None else f"${b['cash']:.2f}"
        return (
            "而家：🔴 實盤\n"
            f"可用 USDC {usdc} · 單筆 ${b['stake']:.0f} · 今日 {_signed(b['today_pnl'])}\n"
            f"開倉 {b['open_n']} · 持倉成本 ${b['open_cost']:.2f}\n"
            "轉返紙盤會只睇紙盤帳，唔會改錢包。"
        )
    return (
        "而家：🧪 紙盤\n"
        f"{_money_line(rt)}\n"
        f"下次重置本金 ${rt.paper_bankroll():.0f}。\n"
        "實盤要 POLYMARKET_PRIVATE_KEY、關 FORCE_PAPER，再撳兩次。"
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
    rows.append([InlineKeyboardButton("週期：5分鐘（鎖定）", callback_data="tags")])
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
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("✅ 5M 5分鐘升跌 · TWAP（鎖定）", callback_data="tags")],
            [InlineKeyboardButton("↩️ 返設定", callback_data="set")],
        ]
    )


def _label(key: str) -> str:
    return {
        "max_usd_per_trade": "單筆上限$（最低$3）",
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
    b = operator_board(rt)
    mode = "🔴 實盤" if b["mode"] == "live" else "🧪 紙盤"
    lines = [
        "🏄 衝浪套利 Bot",
        f"{b['state']} · {mode}",
        _money_line(rt),
        f"開倉 {b['open_n']} · 持倉成本 ${b['open_cost']:.2f} · WS {b['ws']} · CL {b['chainlink']}",
    ]
    lines.extend(b["notes"])
    return "\n".join(lines)


def _status_text(rt: Runtime) -> str:
    """Same short board as home — strategy essay lives in 設定."""
    return home_text(rt)


def _pos_text(rt: Runtime) -> str:
    inv = mode_inventory(rt)
    rest = rt.store.resting_open() if rt.mode() == "paper" else []
    leftover = leftover_paper_inventory(rt)
    lines = [_money_line(rt), "", "📦 倉位"]
    if leftover:
        lines.append(f"紙盤剩倉 {len(leftover)} 檔（完場入紙盤帳，唔彈 Telegram）")
    if rest:
        lines.append("掛單（未碰到盤口唔入 PnL）")
        for row in rest[:10]:
            up_f = "✓" if row.get("up_filled") else "…"
            dn_f = "✓" if row.get("down_filled") else "…"
            lines.append(
                f"{row.get('slug') or row['condition_id'][:8]}  {format_leg_prices(row['up_price'], row['down_price'], leg=(row.get('payload') or {}).get('leg'))} × {format_share_qty(row['shares'])}"
                f"\n  Up {up_f} · Down {dn_f} · 鎖 ${float(row.get('reserved') or 0):.2f}"
            )
    if not inv and not rest:
        if leftover:
            return "\n".join(lines)
        lines.append("而家無倉、無掛單。")
        return "\n".join(lines)
    for row in inv[:15]:
        kind = str(row.get("kind") or "pair")
        tag = " TWAP" if str(kind).startswith("twap") else (" 大熱" if is_favorite_inventory(kind) else "")
        cost = float(row.get("cost") or 0)
        cost_txt = f" · 成本 ${cost:.2f}" if is_directional_inventory(kind) and cost > 0 else ""
        up = float(row.get("up") or 0)
        down = float(row.get("down") or 0)
        legs = []
        if up > 0.01:
            legs.append(f"Up {format_share_qty(up)}")
        if down > 0.01:
            legs.append(f"Down {format_share_qty(down)}")
        if not legs:
            legs.append("空")
        lines.append(
            f"{row['slug'] or row['condition_id'][:8]}{tag}\n  {' · '.join(legs)}{cost_txt}"
        )
    return "\n".join(lines)


def _log_text(rt: Runtime) -> str:
    trades = [t for t in rt.store.recent_trades(24) if t.get("status") not in NOISE_TRADE]
    if rt.mode() == "live":
        trades = [t for t in trades if t.get("mode") != "paper"]
    trades = trades[:8]
    tape = format_tape_lines(rt, n=6)
    lines = ["📜 日誌"]
    if tape:
        lines.append("掃描")
        lines.extend(tape)
    if trades:
        if tape:
            lines.append("")
        lines.append("成交／結算")
        for t in trades:
            stamp = _fmt_ts(t.get("ts"))
            status = STATUS_ZH.get(t["status"], t["status"])
            kind = KIND_ZH.get(t["kind"], t["kind"])
            net = float(t.get("net") or 0)
            sign = "+" if net >= 0 else ""
            pl = t.get("payload") or {}
            cost = pl.get("cost") if t.get("status") in {"paper_filled", "filled"} else None
            lines.append(
                f"{stamp} {status} · {kind}\n"
                f"{t['slug']}\n"
                f"{format_fill_headline(up=t['up_price'], down=t['down_price'], shares=t['shares'], cost=cost, leg=pl.get('leg'))}  {sign}${net:.2f}"
            )
    elif not tape:
        return "近期無掃描日誌、成交或結算。"
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
        await refresh_live_usdc(rt)
        await q.answer()
        await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "status":
        await refresh_live_usdc(rt, force=True)
        await q.answer("已更新")
        await _safe_edit(q, _status_text(rt), reply_markup=home_kb(rt))
        return
    if data == "pos":
        await refresh_live_usdc(rt)
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
        b = operator_board(rt)
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("確認：今日 PnL 由 0 再計", callback_data="circuit2")],
                [InlineKeyboardButton("算吧", callback_data="home")],
            ]
        )
        await q.answer()
        if b["mode"] == "live":
            usdc = "—" if b["cash"] is None else f"${b['cash']:.2f}"
            now = f"而家可用 USDC {usdc} · 實盤今日 {_signed(b['today_pnl'])}。"
        else:
            now = f"而家權益 ${b['equity']:.2f} · 今日 {_signed(b['today_pnl'])}。"
        await _safe_edit(
            q,
            "解除今日熔斷唔會清倉、唔會改本金。\n"
            f"{now}\n"
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
        await refresh_live_usdc(rt)
        await q.answer()
        await _safe_edit(q, mode_text(rt), reply_markup=mode_kb(rt))
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
        blockers = live_switch_blockers(rt.env, rt.geo)
        if blockers:
            await q.answer("；".join(LIVE_BLOCKER_ZH.get(b, b) for b in blockers), show_alert=True)
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
            "轉咗之後主頁同 Dashboard 只顯示錢包可用 USDC、實盤倉同實盤今日 PnL。\n"
            "全自動模式下唔會逐單確認。FORCE_PAPER 開住永遠紙盤。確定轉？",
            reply_markup=kb,
        )
        return
    if data == "live2":
        blockers = live_switch_blockers(rt.env, rt.geo)
        if blockers:
            await q.answer("；".join(LIVE_BLOCKER_ZH.get(b, b) for b in blockers) + "，未轉實盤", show_alert=True)
            await _safe_edit(q, home_text(rt), reply_markup=home_kb(rt))
            return
        err = await arm_live_wallet(rt)
        if err:
            rt.store.add_event("warn", f"live preflight {err}"[:220])
            await q.answer(err[:180], show_alert=True)
            await _safe_edit(q, home_text(rt) + f"\n\n實盤預檢失敗：{err}", reply_markup=home_kb(rt))
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
            settings_text(rt),
            reply_markup=settings_kb(rt),
        )
        return
    if data in {"smode", "fwin", "fdir"}:
        await q.answer("策略已鎖定 TWAP", show_alert=True)
        await _safe_edit(
            q,
            settings_text(rt),
            reply_markup=settings_kb(rt),
        )
        return
    if data == "assets":
        await q.answer()
        await _safe_edit(q, "揀要掃嘅幣。有 Chainlink TWAP-60 嘅 5 分鐘盤會入場。最少留一個。", reply_markup=assets_kb(rt))
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
        await _safe_edit(q, "揀要掃嘅幣。有 Chainlink TWAP-60 嘅 5 分鐘盤會入場。最少留一個。", reply_markup=assets_kb(rt))
        return
    if data == "tags" or data.startswith("tag:"):
        rt.store.patch_settings(tags=["5M"], tag="5M", twap_horizons=["5m"])
        await q.answer("週期已鎖定 5 分鐘", show_alert=True)
        await _safe_edit(
            q,
            "只做 5 分鐘 Chainlink TWAP，多幣種平行。\n"
            "15 分鐘同 5 分鐘搶 14 個 CLOB 槽，又冇獨立 15m 盤帶。\n"
            "1 小時係 Binance 收線，用 TWAP vs PTB 會食錯結算。",
            reply_markup=settings_kb(rt),
        )
        return
    if data.startswith("tog:"):
        key = data.split(":", 1)[1]
        if key in TOGGLES:
            rt.store.patch_settings(**{key: not bool(s.get(key))})
        await q.answer("已更新")
        await _safe_edit(q, settings_text(rt), reply_markup=settings_kb(rt))
        return
    if data.startswith("inc:") or data.startswith("dec:"):
        key = data.split(":", 1)[1]
        if key not in SETTING_STEPS:
            await q.answer()
            return
        step, lo, hi = SETTING_STEPS[key]
        cur = float(s.get(key) or 0)
        if key == "max_usd_per_trade":
            nxt = nudge_trade_usd(cur, up=data.startswith("inc:"))
            note = ""
            if (not data.startswith("inc:")) and abs(nxt - cur) < 1e-9 and nxt <= TRADE_USD_STEPS[0] + 1e-9:
                note = "5分鐘最少5股，45–55¢連fee約$2.84，單筆最低$3。$2買唔入。"
            rt.store.patch_settings(max_usd_per_trade=round(float(nxt), 4))
            if note:
                await q.answer(note)
            else:
                await q.answer()
            await _safe_edit(q, settings_text(rt), reply_markup=settings_kb(rt))
            return
        nxt = cur + step if data.startswith("inc:") else cur - step
        nxt = min(hi, max(lo, nxt))
        if key in {"max_open_markets", "paper_slip_ticks", "paper_starting_cash", "scan_limit"}:
            rt.store.patch_settings(**{key: int(round(nxt))})
        else:
            rt.store.patch_settings(**{key: round(nxt, 4)})
        await q.answer()
        await _safe_edit(q, settings_text(rt), reply_markup=settings_kb(rt))
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
                text=_clip(home_text(rt)),
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
