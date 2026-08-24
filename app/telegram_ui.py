from __future__ import annotations

import asyncio

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

from app.config import SETTING_STEPS, live_keys_ready
from app.geo import telegram_line
from app.runtime import Runtime

TOGGLES = {
    "auto_execute": ("全自動落單", "開咗就唔會逐單問你"),
    "prefer_tail": ("尾盤優先", "近完場、一邊好貴一邊好平先掃"),
    "maker_first": ("掛單（尾盤先，易中毒）", "15m 兩邊掛單會買死邊；預設關"),
    "auto_merge": ("自動 merge", "兩邊齊就換返現金"),
    "notify_signals": ("成交通知", "有紙盤／實盤動作即時彈"),
    "notify_rejects": ("跳過通知", "風控擋咗都會話你知（會嘈）"),
}


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


def home_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    run = "⏸ 暫停" if s.get("engine_running") else "▶️ 繼續跑"
    run_cb = "pause" if s.get("engine_running") else "resume"
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📊 而家狀況", callback_data="status"), InlineKeyboardButton("📦 倉位", callback_data="pos")],
            [InlineKeyboardButton(run, callback_data=run_cb), InlineKeyboardButton("📜 最近紀錄", callback_data="log")],
            [InlineKeyboardButton("💵 紙盤本金", callback_data="bank"), InlineKeyboardButton("♻️ 重置紙盤", callback_data="reset1")],
            [InlineKeyboardButton("⚙️ 高階設定", callback_data="set"), InlineKeyboardButton("🧪／🔴 盤口模式", callback_data="mode")],
            [InlineKeyboardButton("🆘 緊急停機", callback_data="kill")],
        ]
    )


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
    rows = []
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
    rows.append([InlineKeyboardButton("↩️ 返主頁", callback_data="home")])
    return InlineKeyboardMarkup(rows)


def assets_kb(rt: Runtime) -> InlineKeyboardMarkup:
    s = rt.settings()
    cur = set(s.get("assets") or [])
    coins = ["btc", "eth", "sol", "xrp", "doge", "bnb"]
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


def _label(key: str) -> str:
    return {
        "min_edge": "最小缺口",
        "max_usd_per_trade": "單筆上限$",
        "min_shares": "最少股數",
        "daily_loss_limit_usd": "日虧熔斷$",
        "max_open_markets": "最多市場",
        "max_imbalance_shares": "裸倉上限",
        "poll_seconds": "掃描秒",
        "tail_confirm": "尾盤門檻",
        "stale_leg": "過期單門檻",
        "fee_rate": "taker費率",
        "paper_slip_ticks": "紙盤滑點tick",
        "paper_starting_cash": "紙盤本金$",
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
    geo_line = telegram_line(rt.geo)
    last = rt.last_loop or {}
    return (
        f"🏄 衝浪套利 Bot\n"
        f"{state} · {mode}\n"
        f"{_paper_block(rt)}\n"
        f"今日掃描 {st['scans_24h']} · 成交 {st['trades_24h']}\n"
        f"開倉市場 {st['open_markets']} · {keys}\n"
        f"上一圈：{last.get('status','—')} 市場{last.get('markets','—')} 信號{last.get('signals','—')} 成交{last.get('fills','—')}"
        f"{geo_line}\n\n"
        "預設係全自動：見到合規缺口就自己做，唔會逐單問。\n"
        "未交匙之前永遠紙盤。真金要撳兩次確認。"
    )


def _status_text(rt: Runtime) -> str:
    s = rt.settings()
    assets = ", ".join(a.upper() for a in (s.get("assets") or []))
    return (
        home_text(rt)
        + "\n\n高階參數\n"
        + f"缺口 ≥ {s['min_edge']} · 單筆 ≤ ${s['max_usd_per_trade']}\n"
        + f"日虧熔斷 ${s['daily_loss_limit_usd']} · 掃描 {s['poll_seconds']}s\n"
        + f"尾盤優先 {'開' if s['prefer_tail'] else '關'} · 掛單優先 {'開' if s['maker_first'] else '關'}\n"
        + f"幣：{assets or '全部'}"
    )


def _pos_text(rt: Runtime) -> str:
    inv = rt.store.inventory()
    rest = rt.store.resting_open()
    lines = [_paper_block(rt), "", "📦 倉位"]
    if rest:
        lines.append("掛單（未碰到盤口唔入 PnL）")
        for row in rest[:10]:
            up_f = "✓" if row.get("up_filled") else "…"
            dn_f = "✓" if row.get("down_filled") else "…"
            lines.append(
                f"{row.get('slug') or row['condition_id'][:8]}  {row['up_price']}+{row['down_price']} × {row['shares']:.1f}"
                f"\n  Up {up_f} · Down {dn_f} · 鎖 ${float(row.get('reserved') or 0):.2f}"
            )
    if not inv and not rest:
        lines.append("而家無倉、無掛單。Maker 只會掛住等盤口碰到先成交。")
        return "\n".join(lines)
    if inv:
        for row in inv[:15]:
            lines.append(f"{row['slug'] or row['condition_id'][:8]}\n  Up {row['up']:.1f} · Down {row['down']:.1f}")
    return "\n".join(lines)


def _log_text(rt: Runtime) -> str:
    trades = rt.store.recent_trades(8)
    if not trades:
        return "未有成交紀錄。"
    lines = ["📜 最近成交"]
    for t in trades:
        lines.append(
            f"{t['status']} {t['mode']} {t['kind']}\n"
            f"{t['slug']}\n"
            f"{t['up_price']}+{t['down_price']} × {t['shares']}  net ${t['net']:.2f}"
        )
    return "\n".join(lines)


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt: Runtime = context.application.bot_data["rt"]
    if not _owner(update, rt):
        await update.effective_message.reply_text("呢個 bot 已經有主人，唔好意思。")
        return
    await update.effective_message.reply_text(home_text(rt), reply_markup=home_kb(rt))


async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt: Runtime = context.application.bot_data["rt"]
    q = update.callback_query
    if q is None:
        return
    if not _owner(update, rt):
        await q.answer("唔係主人", show_alert=True)
        return
    data = q.data or ""
    s = rt.settings()

    if data == "home":
        await q.answer()
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "status":
        await q.answer()
        await q.edit_message_text(_status_text(rt), reply_markup=home_kb(rt))
        return
    if data == "pos":
        await q.answer()
        await q.edit_message_text(_pos_text(rt), reply_markup=home_kb(rt))
        return
    if data == "log":
        await q.answer()
        await q.edit_message_text(_log_text(rt), reply_markup=home_kb(rt))
        return
    if data == "pause":
        rt.store.patch_settings(engine_running=False)
        await q.answer("已暫停")
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "resume":
        rt.store.patch_settings(engine_running=True, killed=False)
        await q.answer("繼續跑")
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "kill":
        kb = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("確認停機，全部停", callback_data="kill2")],
                [InlineKeyboardButton("算吧", callback_data="home")],
            ]
        )
        await q.answer()
        await q.edit_message_text("緊急停機會停掃描同落單。確定？", reply_markup=kb)
        return
    if data == "kill2":
        rt.store.patch_settings(killed=True, engine_running=False, live_trading=False)
        n = rt.store.cancel_all_resting("kill")
        rt.store.add_event("warn", f"kill switch cancelled_resting={n}")
        await q.answer("已停機")
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "mode":
        await q.answer()
        await q.edit_message_text(
            f"而家：{'紙盤' if rt.mode()=='paper' else '實盤'}\n"
            f"紙盤跟真錢規則：taker 先按盤口成交；maker 只掛單，要盤口真係碰到先入帳。下次重置本金 ${rt.paper_bankroll():.0f}。\n"
            "實盤要環境變數有 POLYMARKET_PRIVATE_KEY，再撳兩次確認。",
            reply_markup=mode_kb(rt),
        )
        return
    if data == "paper":
        rt.store.patch_settings(live_trading=False)
        await q.answer("返紙盤")
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "bank":
        await q.answer()
        await q.edit_message_text(bank_text(rt), reply_markup=bank_kb(rt))
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
        await q.edit_message_text(bank_text(rt), reply_markup=bank_kb(rt))
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
        await q.edit_message_text(
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
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
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
        await q.edit_message_text(
            "實盤會用你把匙簽名落單。\n"
            "全自動模式下唔會逐單確認。\n"
            "建議先紙盤睇一日先。確定轉？",
            reply_markup=kb,
        )
        return
    if data == "live2":
        rt.store.patch_settings(live_trading=True, killed=False, engine_running=True)
        rt.store.add_event("warn", "live trading enabled")
        await q.answer("已轉實盤")
        await q.edit_message_text(home_text(rt), reply_markup=home_kb(rt))
        return
    if data == "set":
        await q.answer()
        await q.edit_message_text("高階設定。撳開關或者加減。預設已經係全自動紙盤。", reply_markup=settings_kb(rt))
        return
    if data == "assets":
        await q.answer()
        await q.edit_message_text("揀要掃嘅幣。最少留一個。", reply_markup=assets_kb(rt))
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
        await q.edit_message_text("揀要掃嘅幣。最少留一個。", reply_markup=assets_kb(rt))
        return
    if data.startswith("tog:"):
        key = data.split(":", 1)[1]
        if key in TOGGLES:
            rt.store.patch_settings(**{key: not bool(s.get(key))})
        await q.answer("已更新")
        await q.edit_message_text("高階設定。撳開關或者加減。", reply_markup=settings_kb(rt))
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
        if key in {"max_open_markets", "paper_slip_ticks", "paper_starting_cash"}:
            rt.store.patch_settings(**{key: int(round(nxt))})
        else:
            rt.store.patch_settings(**{key: round(nxt, 4)})
        await q.answer()
        await q.edit_message_text("高階設定。撳開關或者加減。", reply_markup=settings_kb(rt))
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
                text=home_text(rt) + f"\n\n紙盤下次重置本金 ${rt.paper_bankroll():.0f}。現金／權益／PnL 睇上面同 dashboard。",
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
                await application.bot.send_message(chat_id=owner, text=note["text"], reply_markup=home_kb(rt))
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
