"""Comprehensive Traditional Chinese Telegram Bot synced in real time with Engine & Web Dashboard.

Capabilities:
* Strict user-ID allowlist (``POLYARB_TG_ALLOWED_USERS``; messages from anyone else are silently ignored).
* Live interactive command center via Inline Keyboards + slash commands:
  - ``/start`` / ``/status`` : Real-time PnL, capital, exposure, engine health, wallet & mode status
  - ``/quotes``              : Active zero-fee maker quotes + 1-tap cancel all
  - ``/holds``               : Held positions, resting 0.995 flip orders + 1-tap redeem/reconcile check
  - ``/trades``              : Recent executions & detected opportunities
  - ``/near``                : Top near-miss markets per strategy
  - ``/metar``               : Global 48-station NOAA METAR live highs/lows + 1-tap poll
  - ``/settings``            : Full 7-category, 40+ parameter interactive editor (1-tap toggles, presets, custom numeric input, per-category & global reset)
  - ``/mode``                : Safe hot-switch between Paper and Live modes (with 7-check readiness display & explicit confirmation)
  - ``/pause`` / ``/resume`` : Instant trading pause & resume synced with Web Dashboard
  - ``/resetkill``           : Guarded kill-switch reset
* Proactive push alerts (configurable in ``/settings`` -> 🔔 Telegram 通知):
  - Startup greeting, executions, 0.995 fast-flips, auto-redemptions, kill-switch / risk / mode changes, high-profit opportunities, and periodic summary reports.
"""

from __future__ import annotations

import asyncio
import html
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from polyarb import __version__
from polyarb.monitor import Monitor
from polyarb.settings import (
    BY_CATEGORY,
    BY_CODE,
    CATEGORIES,
    SKIN_LABELS,
    VALID_SKINS,
    SettingSpec,
    SettingsManager,
    format_setting_value,
    get_cfg_attr,
    input_hint,
    parse_human_value,
)
from polyarb.web.server import Controls

HKT = timezone(timedelta(hours=8))
TG_API_BASE = "https://api.telegram.org"
log = logging.getLogger(__name__)

STRAT_NAMES = {
    "negrisk_convert": "轉換套利",
    "metar_lock": "METAR 氣象鎖定",
    "binance_lock": "幣安收盤鎖定",
    "crypto_short": "5m/15m/1h 短週期套利",
    "twin_arb": "雙生跨盤套利",
    "maker_convert": "零費掛單轉換",
    "maker_metar": "零費 METAR 保底",
    "maker_binance": "零費幣安保底",
    "maker_crypto_short": "零費 5m/15m/1h 保底",
    "maker_crypto_pair": "零費 5m/15m/1h 跨盤",
    "maker_ladder": "零費掛單階梯",
    "maker_yes_basket": "零費掛單籃子",
    "maker_twin": "零費掛單雙生",
    "negrisk_yes_basket": "YES 籃子",
    "ladder": "階梯套利",
}

BOT_COMMANDS = [
    {"command": "start", "description": "開啟 POLYARB 指揮中心主面板"},
    {"command": "status", "description": "即時盈虧、資金與引擎總覽"},
    {"command": "settings", "description": "全參數設定中心（7 大分類即時熱更新）"},
    {"command": "quotes", "description": "查看或撤銷零費掛單（Maker）"},
    {"command": "holds", "description": "待結算持倉、0.995 掛賣與立即對帳贖回"},
    {"command": "trades", "description": "最近成交與套利機會紀錄"},
    {"command": "near", "description": "各策略最接近套利排行榜"},
    {"command": "metar", "description": "全球 48 機場 METAR 實測氣溫狀態"},
    {"command": "mode", "description": "紙盤 / 實盤安全熱切換與就緒檢查"},
    {"command": "skin", "description": "切換網頁控制台 5 款旗艦外觀主題"},
    {"command": "pause", "description": "立即暫停所有新交易並撤銷掛單"},
    {"command": "resume", "description": "恢復自動交易"},
]


def _esc(val: Any) -> str:
    return html.escape(str(val if val is not None else ""))


def _usd(val: float | None, signed: bool = False) -> str:
    if val is None:
        return "—"
    if signed:
        return f"{'+' if val >= 0 else '-'}${abs(val):,.2f}"
    return f"${val:,.2f}"


def _hkt(ts: float | str | None) -> str:
    if not ts:
        return "—"
    try:
        if isinstance(ts, (int, float)):
            dt = datetime.fromtimestamp(float(ts), HKT)
        else:
            dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(HKT)
        return dt.strftime("%m-%d %H:%M:%S HKT")
    except Exception:
        return str(ts)


def _dur(sec: float | None) -> str:
    if not sec or sec < 0:
        return "0秒"
    s = int(sec)
    if s < 60:
        return f"{s}秒"
    if s < 3600:
        return f"{s // 60}分 {s % 60}秒"
    if s < 86400:
        return f"{s // 3600}小時 {(s % 3600) // 60}分"
    return f"{s // 86400}日 {(s % 86400) // 3600}小時"


def parse_allowed_users(raw: str | None) -> set[int]:
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").split(","):
        p = part.strip()
        if p.lstrip("-").isdigit():
            out.add(int(p))
    return out


class TelegramBot:
    def __init__(
        self,
        token: str,
        allowed_users: set[int],
        monitor: Monitor,
        controls: Controls,
        settings: SettingsManager,
        *,
        dashboard_url: str = "",
        http: httpx.AsyncClient | None = None,
        api_base: str = TG_API_BASE,
    ) -> None:
        self.token = token.strip()
        self.allowed_users = set(allowed_users)
        self.monitor = monitor
        self.controls = controls
        self.settings = settings
        self.dashboard_url = dashboard_url.strip()
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=40.0)
        self.api_url = f"{api_base.rstrip('/')}/bot{self.token}"
        self.username: str = ""
        self.running: bool = False
        self.offset: int = 0
        self.messages_handled: int = 0
        self.alerts_sent: int = 0
        self.unauthorized_blocked: int = 0
        self.last_poll_at: float = 0.0
        self._awaiting_input: dict[int, tuple[str, float]] = {}  # user_id -> (spec.code, expires_at)
        self._tasks: list[asyncio.Task] = []
        self._push_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=200)
        self._last_kill_alert: str = ""

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> bool:
        if not self.token or not self.allowed_users:
            return False
        me = await self._call("getMe")
        if not me or not isinstance(me, dict):
            log.error("Telegram getMe failed; TG bot disabled")
            return False
        self.username = str(me.get("username") or "")
        await self._call("deleteWebhook", drop_pending_updates=False)
        await self._call("setMyCommands", commands=BOT_COMMANDS)
        self.running = True
        if self._on_monitor_event not in self.monitor.event_hooks:
            self.monitor.event_hooks.append(self._on_monitor_event)
        self._tasks = [
            asyncio.create_task(self._poll_loop(), name="tg-poll"),
            asyncio.create_task(self._push_loop(), name="tg-push"),
            asyncio.create_task(self._summary_loop(), name="tg-summary"),
        ]
        log.info("Telegram bot @%s started for %d authorized user(s)", self.username or "bot", len(self.allowed_users))
        mode_zh = "🔴 實盤 LIVE" if self.monitor.info.mode == "live" else "🔵 紙盤 PAPER"
        paused_zh = "⏸ 已暫停" if self.controls.is_paused() else "▶️ 運行中"
        self.enqueue_alert(
            f"🚀 <b>POLYARB v{_esc(__version__)} 指揮中心已連線</b>\n"
            f"模式：{mode_zh}｜狀態：{paused_zh}\n"
            f"輸入 /start 或撳下方按鈕即可查看即時數據與調校全部參數。",
            reply_markup=self._home_Summary_kb(),
        )
        return True

    async def stop(self) -> None:
        self.running = False
        if self._on_monitor_event in self.monitor.event_hooks:
            self.monitor.event_hooks.remove(self._on_monitor_event)
        for t in self._tasks:
            t.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._own_http:
            await self._http.aclose()

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": bool(self.token and self.allowed_users),
            "running": self.running,
            "username": self.username,
            "authorized_users": len(self.allowed_users),
            "handled": self.messages_handled,
            "alerts_sent": self.alerts_sent,
            "last_poll_at": round(self.last_poll_at, 1),
        }

    # ------------------------------------------------------------------ low-level API

    async def _call(self, method: str, **payload: Any) -> Any:
        try:
            resp = await self._http.post(f"{self.api_url}/{method}", json=payload)
            data = resp.json()
            if not data.get("ok"):
                desc = str(data.get("description") or "")
                if "message is not modified" in desc.lower():
                    return True
                log.debug("TG %s rejected: %s", method, desc)
                return None
            return data.get("result")
        except Exception as exc:
            log.debug("TG %s transport error: %s", method, exc)
            return None

    async def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return await self._call("sendMessage", **payload)

    async def edit_or_send(
        self, chat_id: int, message_id: int | None, text: str, reply_markup: dict | None = None
    ) -> Any:
        if message_id is not None:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            res = await self._call("editMessageText", **payload)
            if res is not None:
                return res
        return await self.send_message(chat_id, text, reply_markup=reply_markup)

    # ------------------------------------------------------------------ loops & push notifications

    async def _poll_loop(self) -> None:
        backoff = 1.0
        while self.running:
            try:
                resp = await self._http.post(
                    f"{self.api_url}/getUpdates",
                    json={
                        "offset": self.offset,
                        "timeout": 25,
                        "allowed_updates": ["message", "callback_query"],
                    },
                    timeout=35.0,
                )
                self.last_poll_at = time.time()
                data = resp.json()
                if not data.get("ok"):
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 20.0)
                    continue
                backoff = 1.0
                for upd in data.get("result") or []:
                    uid = int(upd.get("update_id", 0))
                    if uid >= self.offset:
                        self.offset = uid + 1
                    await self.handle_update(upd)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.debug("TG poll error: %s", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 20.0)

    def enqueue_alert(self, text: str, reply_markup: dict | None = None) -> None:
        item = (text, reply_markup)
        try:
            self._push_queue.put_nowait(item)  # type: ignore[arg-type]
        except asyncio.QueueFull:
            pass

    async def _push_loop(self) -> None:
        while self.running:
            item = await self._push_queue.get()
            text, markup = item if isinstance(item, tuple) else (str(item), None)
            for uid in list(self.allowed_users):
                res = await self.send_message(uid, text, reply_markup=markup)
                if res is not None:
                    self.alerts_sent += 1
            await asyncio.sleep(0.15)

    async def _summary_loop(self) -> None:
        while self.running:
            await asyncio.sleep(60.0)
            # Check newly tripped kill switch even if triggered asynchronously
            rk = self.monitor.engine.risk.state
            if rk.killed and rk.kill_reason != self._last_kill_alert:
                self._last_kill_alert = rk.kill_reason
                if self.monitor.engine.cfg.tg.notify_risk:
                    self.enqueue_alert(
                        f"🚨 <b>熔斷已觸發，所有交易已停止！</b>\n原因：<code>{_esc(rk.kill_reason)}</code>\n請檢查持倉後於主面板解除熔斷。",
                        reply_markup=self._home_Summary_kb(),
                    )
            elif not rk.killed:
                self._last_kill_alert = ""

            hours = float(self.monitor.engine.cfg.tg.summary_hours or 0.0)
            if hours <= 0:
                continue
            now = time.time()
            last = getattr(self, "_last_summary_at", 0.0)
            if last == 0.0:
                self._last_summary_at = now
                continue
            if now - last >= hours * 3600.0:
                self._last_summary_at = now
                txt, kb = self.render_home()
                self.enqueue_alert(f"📊 <b>定時戰報（每 {hours:g} 小時）</b>\n\n" + txt, reply_markup=kb)

    def _on_monitor_event(self, kind: str, record: dict) -> None:
        tcfg = self.monitor.engine.cfg.tg
        if kind == "execution" and tcfg.notify_executions:
            strat = STRAT_NAMES.get(str(record.get("strategy")), str(record.get("strategy")))
            mode_zh = "🔴實盤" if record.get("mode") == "live" else "🔵紙盤"
            icon = "⚠️" if record.get("orphaned") else ("✅" if record.get("complete") else "◐")
            self.enqueue_alert(
                f"{icon} <b>{mode_zh}成交｜{_esc(strat)}</b>\n"
                f"市場：<b>{_esc(record.get('title'))}</b>\n"
                f"動作：<code>{_esc(record.get('action'))}</code>\n"
                f"投入：<b>{_usd(record.get('spent'))}</b>｜預期利潤：<b>{_usd(record.get('expected_profit'), True)}</b>\n"
                f"已實現：<b>{_usd(record.get('realized_pnl'), True)}</b>｜鎖定：<b>{_usd(record.get('locked_cost'))}</b>\n"
                f"延遲：{record.get('latency_ms', '—')} ms",
                reply_markup=self._home_Summary_kb(),
            )
        elif kind == "flip_closed" and tcfg.notify_flips_redeems:
            self.enqueue_alert(
                f"⚡ <b>閃電止盈成交（0.99+ 免手續費）</b>\n"
                f"市場：<b>{_esc(record.get('title'))}</b>\n"
                f"賣出：{record.get('shares')} 股 @ {record.get('price')}\n"
                f"收回：<b>{_usd(record.get('proceeds'))}</b>｜淨利潤：<b>{_usd(record.get('pnl'), True)}</b>"
            )
        elif kind == "early_harvest" and tcfg.notify_flips_redeems:
            self.enqueue_alert(
                f"💎 <b>提前收割／中間帶雙重派彩完成！</b>\n"
                f"市場：<b>{_esc(record.get('title'))}</b>\n"
                f"主腿鎖定收回：<b>{_usd(record.get('primary_usd'))}</b>｜副腿額外紅利：<b>+{_usd(record.get('bonus_usd'))}</b>\n"
                f"總收回：<b>{_usd(record.get('proceeds'))}</b>｜淨利潤：<b>{_usd(record.get('pnl'), True)}</b>"
            )
        elif kind == "redeemed" and tcfg.notify_flips_redeems:
            self.enqueue_alert(
                f"💰 <b>已自動鏈上贖回派彩</b>\n"
                f"市場：<b>{_esc(record.get('title'))}</b>\n"
                f"收回：<b>{_usd(record.get('proceeds'))}</b>｜淨利潤：<b>{_usd(record.get('pnl'), True)}</b>"
            )
        elif kind == "opportunity" and tcfg.notify_opportunities:
            prof = float(record.get("profit") or 0.0)
            if prof >= tcfg.opportunity_min_profit_usd:
                strat = STRAT_NAMES.get(str(record.get("strategy")), str(record.get("strategy")))
                self.enqueue_alert(
                    f"🔎 <b>發現達標機會｜{_esc(strat)}</b>\n"
                    f"市場：<b>{_esc(record.get('title'))}</b>\n"
                    f"預期利潤：<b>{_usd(prof, True)}</b>（{record.get('edge_pct')}%）｜成本：{_usd(record.get('cost'))}"
                )
        elif kind == "control" and tcfg.notify_risk and record.get("source") == "dashboard":
            act = record.get("action")
            zh = {"pause": "⏸ 網頁控制台已暫停交易", "resume": "▶️ 網頁控制台已恢復交易", "reset_kill": "🟢 網頁控制台已重置熔斷"}.get(str(act))
            if zh:
                self.enqueue_alert(f"🖥️ <b>{zh}</b>（TG 與網頁已同步）")
        elif kind == "mode_switch" and tcfg.notify_risk:
            m_zh = "🔴 實盤 LIVE" if record.get("mode") == "live" else "🔵 紙盤 PAPER"
            self.enqueue_alert(f"🔀 <b>交易模式已切換為：{m_zh}</b>（來源：{_esc(record.get('source'))}）")

    # ------------------------------------------------------------------ authorization & routing

    def _authorized(self, user_id: int | None, chat_id: int | None) -> bool:
        if user_id is None or user_id not in self.allowed_users:
            self.unauthorized_blocked += 1
            return False
        if chat_id is not None and chat_id != user_id and chat_id not in self.allowed_users:
            self.unauthorized_blocked += 1
            return False
        return True

    async def handle_update(self, upd: dict[str, Any]) -> None:
        if "callback_query" in upd:
            cq = upd["callback_query"]
            user = cq.get("from") or {}
            msg = cq.get("message") or {}
            uid = user.get("id")
            cid = (msg.get("chat") or {}).get("id", uid)
            if not self._authorized(uid, cid):
                return
            self.messages_handled += 1
            await self._handle_callback(int(uid), int(cid), msg.get("message_id"), str(cq.get("id") or ""), str(cq.get("data") or ""))
            return

        msg = upd.get("message")
        if not isinstance(msg, dict):
            return
        user = msg.get("from") or {}
        uid = user.get("id")
        cid = (msg.get("chat") or {}).get("id")
        if not self._authorized(uid, cid):
            return
        text = str(msg.get("text") or "").strip()
        if not text:
            return
        self.messages_handled += 1
        await self._handle_message(int(uid), int(cid), text)

    # ------------------------------------------------------------------ text commands & custom value input

    async def _handle_message(self, uid: int, cid: int, text: str) -> None:
        # Handle pending custom value input if not a slash command (or /cancel)
        pending = self._awaiting_input.get(uid)
        if pending is not None:
            code, exp = pending
            if text.lower() in ("/cancel", "cancel", "取消"):
                self._awaiting_input.pop(uid, None)
                spec = BY_CODE.get(code)
                if spec:
                    t_out, kb = self.render_setting_item(spec)
                    await self.send_message(cid, "✅ 已取消輸入。\n\n" + t_out, reply_markup=kb)
                else:
                    t_out, kb = self.render_home()
                    await self.send_message(cid, t_out, reply_markup=kb)
                return
            if time.time() > exp:
                self._awaiting_input.pop(uid, None)
            elif not text.startswith("/"):
                spec = BY_CODE.get(code)
                if spec is not None:
                    try:
                        val = parse_human_value(spec, text)
                        label, formatted = await self.controls.update_setting(spec.key, val, source="telegram")
                        self._awaiting_input.pop(uid, None)
                        t_out, kb = self.render_setting_item(spec)
                        await self.send_message(
                            cid,
                            f"✅ <b>已即時更新並儲存：{_esc(label)} = {_esc(formatted)}</b>\n（引擎與網頁控制台已同步生效）\n\n" + t_out,
                            reply_markup=kb,
                        )
                    except ValueError as exc:
                        await self.send_message(
                            cid,
                            f"⚠️ 輸入格式或範圍不正確：<b>{_esc(exc)}</b>\n{_esc(input_hint(spec))}\n（輸入 /cancel 可取消）",
                        )
                    return

        cmd_raw, _, arg = text.partition(" ")
        cmd = cmd_raw.split("@", 1)[0].lower()
        arg = arg.strip()

        if cmd in ("/start", "/status", "/menu", "/home"):
            t_out, kb = self.render_home()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/quotes":
            t_out, kb = self.render_quotes()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/holds":
            t_out, kb = self.render_holds()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/trades":
            t_out, kb = self.render_trades()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/near":
            t_out, kb = self.render_near("negrisk_convert")
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/metar":
            t_out, kb = self.render_metar()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/settings":
            t_out, kb = self.render_settings_categories()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/mode":
            t_out, kb = self.render_mode()
            await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/skin":
            if arg.strip().lower() in VALID_SKINS:
                clean = self.controls.set_skin(arg.strip().lower(), source="telegram")
                t_out, kb = self.render_skin()
                await self.send_message(cid, f"🎨 已同步網頁外觀為：<b>{_esc(SKIN_LABELS[clean])}</b>\n\n{t_out}", reply_markup=kb)
            else:
                t_out, kb = self.render_skin()
                await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/pause":
            err = await self.controls.pause_async(source="telegram")
            note = f"⚠️ 無法暫停：{_esc(err)}" if err else "⏸ <b>已暫停交易，並已自動撤銷所有未成交掛單。</b>"
            t_out, kb = self.render_home()
            await self.send_message(cid, f"{note}\n\n{t_out}", reply_markup=kb)
        elif cmd == "/resume":
            await self.controls.resume_async(source="telegram")
            t_out, kb = self.render_home()
            await self.send_message(cid, f"▶️ <b>已恢復自動交易！</b>\n\n{t_out}", reply_markup=kb)
        elif cmd == "/resetkill":
            if arg.upper() == "RESET":
                await self.controls.reset_kill_async(source="telegram")
                t_out, kb = self.render_home()
                await self.send_message(cid, f"🟢 <b>熔斷已重置！</b>\n\n{t_out}", reply_markup=kb)
            else:
                t_out, kb = self.render_kill_confirm()
                await self.send_message(cid, t_out, reply_markup=kb)
        elif cmd == "/set" and arg:
            k, _, v_str = arg.partition("=")
            if not v_str:
                k, _, v_str = arg.partition(" ")
            spec = BY_CODE.get(k.strip()) or next((s for s in BY_CODE.values() if s.key == k.strip()), None)
            if spec is None or not v_str.strip():
                await self.send_message(cid, "用法：<code>/set 參數鍵 值</code>（建議直接用 /settings 互動選單）")
                return
            try:
                val = parse_human_value(spec, v_str)
                label, formatted = await self.controls.update_setting(spec.key, val, source="telegram")
                await self.send_message(cid, f"✅ <b>{_esc(label)}</b> 已設為 <b>{_esc(formatted)}</b>")
            except ValueError as exc:
                await self.send_message(cid, f"⚠️ 設定失敗：{_esc(exc)}")
        else:
            t_out, kb = self.render_home()
            await self.send_message(cid, t_out, reply_markup=kb)

    # ------------------------------------------------------------------ callback queries

    async def _handle_callback(self, uid: int, cid: int, mid: int | None, cqid: str, data: str) -> None:
        toast_msg = ""
        try:
            parts = data.split(":")
            head = parts[0]

            if head == "nav":
                target = parts[1] if len(parts) > 1 else "home"
                self._awaiting_input.pop(uid, None)
                if target == "home":
                    t_out, kb = self.render_home()
                elif target == "quotes":
                    t_out, kb = self.render_quotes()
                elif target == "holds":
                    t_out, kb = self.render_holds()
                elif target == "trades":
                    t_out, kb = self.render_trades()
                elif target == "near":
                    st_name = parts[2] if len(parts) > 2 else "negrisk_convert"
                    t_out, kb = self.render_near(st_name)
                elif target == "metar":
                    t_out, kb = self.render_metar()
                elif target == "settings":
                    t_out, kb = self.render_settings_categories()
                elif target == "mode":
                    t_out, kb = self.render_mode()
                elif target == "skin":
                    t_out, kb = self.render_skin()
                else:
                    t_out, kb = self.render_home()
                await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "act":
                op = parts[1] if len(parts) > 1 else ""
                if op == "pause":
                    err = await self.controls.pause_async(source="telegram")
                    toast_msg = err or "⏸ 已暫停交易並撤銷掛單"
                    t_out, kb = self.render_home()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "resume":
                    await self.controls.resume_async(source="telegram")
                    toast_msg = "▶️ 已恢復自動交易"
                    t_out, kb = self.render_home()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "cancel_q":
                    n = await self.controls.cancel_quotes(source="telegram")
                    toast_msg = f"已撤銷 {n} 張掛單"
                    t_out, kb = self.render_quotes()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "sync_lc":
                    await self.controls.trigger_lifecycle(source="telegram")
                    toast_msg = "已完成鏈上贖回與持倉對帳檢查"
                    t_out, kb = self.render_holds()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "poll_metar":
                    n = await self.controls.trigger_metar_poll(source="telegram")
                    toast_msg = f"已輪詢 48 機場 METAR（{n} 站更新）"
                    t_out, kb = self.render_metar()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "kill_ask":
                    t_out, kb = self.render_kill_confirm()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif op == "kill_ok":
                    await self.controls.reset_kill_async(source="telegram")
                    toast_msg = "🟢 熔斷已重置"
                    t_out, kb = self.render_home()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "sc":
                cat = parts[1] if len(parts) > 1 else "risk"
                self._awaiting_input.pop(uid, None)
                t_out, kb = self.render_settings_category(cat)
                await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "si":
                code = parts[1] if len(parts) > 1 else ""
                spec = BY_CODE.get(code)
                if spec:
                    self._awaiting_input.pop(uid, None)
                    t_out, kb = self.render_setting_item(spec)
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "st":
                # Instant boolean toggle from category or item screen!
                code = parts[1] if len(parts) > 1 else ""
                origin = parts[2] if len(parts) > 2 else "item"
                spec = BY_CODE.get(code)
                if spec and spec.kind == "bool":
                    cur = bool(get_cfg_attr(self.monitor.engine.cfg, spec.key))
                    label, formatted = await self.controls.update_setting(spec.key, not cur, source="telegram")
                    toast_msg = f"{label}：{formatted}"
                    t_out, kb = (
                        self.render_settings_category(spec.category)
                        if origin == "cat"
                        else self.render_setting_item(spec)
                    )
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "sp":
                code = parts[1] if len(parts) > 1 else ""
                idx = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else -1
                spec = BY_CODE.get(code)
                if spec and 0 <= idx < len(spec.presets):
                    label, formatted = await self.controls.update_setting(spec.key, spec.presets[idx], source="telegram")
                    toast_msg = f"✅ {label} = {formatted}"
                    t_out, kb = self.render_setting_item(spec)
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "sa":
                code = parts[1] if len(parts) > 1 else ""
                spec = BY_CODE.get(code)
                if spec:
                    self._awaiting_input[uid] = (code, time.time() + 300.0)
                    cur = format_setting_value(spec, get_cfg_attr(self.monitor.engine.cfg, spec.key))
                    txt = (
                        f"✏️ <b>自訂輸入：{_esc(spec.label)}</b>\n"
                        f"目前數值：<b>{_esc(cur)}</b>\n\n"
                        f"{_esc(input_hint(spec))}\n"
                        f"請直接於對話框輸入新數字發送（5 分鐘內有效，或撳下方返回取消）："
                    )
                    kb = {"inline_keyboard": [[{"text": "⬅️ 取消並返回", "callback_data": f"si:{spec.code}"}]]}
                    await self.edit_or_send(cid, mid, txt, reply_markup=kb)

            elif head == "sr":
                scope = parts[1] if len(parts) > 1 else ""
                if scope == "all":
                    n = await self.controls.reset_settings(source="telegram")
                    toast_msg = f"已還原 {n} 項自訂設定至預設值"
                    t_out, kb = self.render_settings_categories()
                elif scope in dict((c[0], c[1]) for c in CATEGORIES):
                    n = await self.controls.reset_settings(category=scope, source="telegram")
                    toast_msg = f"已還原此分類 {n} 項設定"
                    t_out, kb = self.render_settings_category(scope)
                elif scope in BY_CODE:
                    spec = BY_CODE[scope]
                    await self.controls.reset_settings(key=spec.key, source="telegram")
                    toast_msg = f"已還原 {spec.label}"
                    t_out, kb = self.render_setting_item(spec)
                else:
                    t_out, kb = self.render_settings_categories()
                await self.edit_or_send(cid, mid, t_out, reply_markup=kb)

            elif head == "md":
                sub = parts[1] if len(parts) > 1 else ""
                if sub == "ask_live":
                    t_out, kb = self.render_mode(confirm_live=True)
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
                elif sub == "go_paper":
                    ok, msg = await self.controls.switch_mode("paper", pause_first=False, source="telegram")
                    toast_msg = "✅ 已切換至紙盤" if ok else f"切換失敗：{msg}"
                    t_out, kb = self.render_mode()
                    await self.edit_or_send(cid, mid, f"ℹ️ {_esc(msg)}\n\n" + t_out, reply_markup=kb)
                elif sub in ("go_live_paused", "go_live_run"):
                    pause_first = sub == "go_live_paused"
                    ok, msg = await self.controls.switch_mode("live", pause_first=pause_first, source="telegram")
                    toast_msg = "🔴 已切換至實盤" if ok else f"無法啟動實盤：{msg}"
                    t_out, kb = self.render_mode()
                    await self.edit_or_send(cid, mid, f"{'✅' if ok else '⚠️'} {_esc(msg)}\n\n" + t_out, reply_markup=kb)

            elif head == "sk":
                sk = parts[1] if len(parts) > 1 else ""
                if sk in VALID_SKINS:
                    clean = self.controls.set_skin(sk, source="telegram")
                    toast_msg = f"🎨 已套用：{SKIN_LABELS[clean]}"
                    t_out, kb = self.render_skin()
                    await self.edit_or_send(cid, mid, t_out, reply_markup=kb)
        finally:
            await self._call("answerCallbackQuery", callback_query_id=cqid, text=toast_msg[:190] if toast_msg else None)

    # ------------------------------------------------------------------ rich Traditional Chinese views

    def _home_Summary_kb(self) -> dict:
        paused = self.controls.is_paused()
        toggle_btn = (
            {"text": "▶️ 恢復交易", "callback_data": "act:resume"}
            if paused
            else {"text": "⏸ 暫停交易", "callback_data": "act:pause"}
        )
        rows: list[list[dict[str, Any]]] = [
            [
                {"text": "🔄 即時刷新", "callback_data": "nav:home"},
                toggle_btn,
            ],
            [
                {"text": "🎯 零費掛單", "callback_data": "nav:quotes"},
                {"text": "📦 持倉贖回", "callback_data": "nav:holds"},
                {"text": "🌤️ METAR 氣象", "callback_data": "nav:metar"},
            ],
            [
                {"text": "📜 成交與機會", "callback_data": "nav:trades"},
                {"text": "📡 最接近套利", "callback_data": "nav:near:negrisk_convert"},
            ],
            [
                {"text": "⚙️ 設定中心（全參數）", "callback_data": "nav:settings"},
                {"text": "🎨 5 款主題", "callback_data": "nav:skin"},
                {"text": "🔀 紙盤 / 實盤", "callback_data": "nav:mode"},
            ],
        ]
        if self.monitor.engine.risk.state.killed:
            rows.insert(1, [{"text": "🚨 解除熔斷（需確認）", "callback_data": "act:kill_ask"}])
        if self.dashboard_url:
            rows.append([{"text": "🌐 開啟網頁版控制台", "url": self.dashboard_url}])
        return {"inline_keyboard": rows}

    def render_home(self) -> tuple[str, dict]:
        t = self.monitor.tick()
        k = t["kpi"]
        e = t["engine"]
        r = t["risk"]
        w = t.get("wallet") or {}
        mt = e.get("metar") or {}
        mk = e.get("maker") or {}
        lc = e.get("lifecycle") or {}

        mode_badge = "🔴 <b>實盤 LIVE</b>" if t["mode"] == "live" else "🔵 <b>紙盤 PAPER</b>"
        if r["killed"]:
            state_badge = f"🚨 <b>已熔斷</b>（{_esc(r['kill_reason'])}）"
        elif r["paused"]:
            state_badge = "⏸ <b>已暫停</b>"
        else:
            state_badge = "🟢 <b>全速運行中</b>"

        total_pnl = (k["pnl_total"] or 0.0) + (k["locked_profit"] or 0.0)
        wallet_line = ""
        if t["mode"] == "live" and w:
            wallet_line = (
                f"\n💳 <b>實盤錢包</b>：<code>{_esc(w.get('funder', '?'))}</code>\n"
                f"   鏈上 pUSD：<b>{_usd(k.get('wallet_balance'))}</b>｜掛單預留：<b>{_usd(k.get('reserved_usd'))}</b>"
            )
        else:
            wallet_line = (
                f"\n💼 <b>模擬本金</b>：<b>{_usd(k.get('bankroll'))}</b>｜掛單預留：<b>{_usd(k.get('reserved_usd'))}</b>"
            )

        ov_n = len(self.settings.overrides)
        lines = [
            f"⚡ <b>POLYARB v{_esc(__version__)} 指揮中心</b>",
            f"模式：{mode_badge}｜狀態：{state_badge}",
            f"時間：<code>{_hkt(t['now'])}</code>（運行 {_dur(e.get('uptime_s'))}）",
            "",
            "💰 <b>盈虧與資金</b>",
            f"• 累計總利潤：<b>{_usd(total_pnl, True)}</b>（已實現 <b>{_usd(k['pnl_total'], True)}</b>｜今日 <b>{_usd(k['pnl_today'], True)}</b>）",
            f"• 待結算鎖定利潤：<b>{_usd(k['locked_profit'], True)}</b>（持倉成本 {_usd(k['hold_exposure'])} / 上限 {_usd(r['max_hold_exposure_usd'])}）",
            f"• 立即可用資金：<b>{_usd(k['available'])}</b>（單筆上限 {_usd(r['max_trade_usd'])}｜閃電預備金 {_usd(self.monitor.engine.cfg.risk.liquid_reserve_usd)}）"
            + wallet_line,
            "",
            "⚙️ <b>策略與周轉引擎</b>",
            f"• 監控範圍：<b>{e['events']:,}</b> 事件｜<b>{e['fresh']:,}/{e['books']:,}</b> 新鮮簿｜WS <b>{int(e.get('ws_rate') or 0)}/秒</b>",
            f"• 零費掛單（Maker）：<b>{mk.get('open_quotes', 0)}</b> 張掛單中（已掛 {mk.get('placed', 0)}｜成交 {mk.get('filled', 0)}｜撤單 {mk.get('canceled', 0)}）",
            f"• 預言機實測源：METAR <b>{mt.get('stations', 0)}</b> 站（{mt.get('polls', 0)} 輪）｜幣安 K 線 <b>{(e.get('binance') or {}).get('symbols', 0)}</b> 幣種（{(e.get('binance') or {}).get('polls', 0)} 輪）",
            f"• 資金回收：閃電止盈 <b>{lc.get('flips_completed', 0)}</b> 次｜提前收割 <b>{lc.get('early_harvests', 0)}</b> 次｜自動贖回 <b>{lc.get('redemptions', 0)}</b> 次｜成交 <b>{k['trades']}</b> 筆",
            f"• 資源與自訂設定：CPU <b>{e.get('cpu_pct', 0):.1f}%</b>｜RAM <b>{e.get('rss_mb', 0):.0f} MB</b>｜自訂覆蓋 <b>{ov_n}</b> 項",
        ]
        return "\n".join(lines), self._home_Summary_kb()

    def render_quotes(self) -> tuple[str, dict]:
        mk = (self.monitor.tick()["engine"].get("maker")) or {}
        quotes = mk.get("quotes") or []
        lines = [
            "🎯 <b>零費掛單對沖（Maker-First）</b>",
            f"狀態：{'🟢 開啟' if mk.get('enabled') else '⚪ 關閉'}｜掛單中：<b>{mk.get('open_quotes', 0)}</b> / {self.monitor.engine.cfg.maker.max_open_quotes}",
            f"預留資金：<b>{_usd(mk.get('reserved_usd', 0.0))}</b>｜預估回贈：<b>+{_usd(mk.get('rebates_est_usd', 0.0))}</b>\n"
            f"累計：已掛 {mk.get('placed', 0)}・成交 {mk.get('filled', 0)}・移倉 {mk.get('repegged', 0)}・撤單 {mk.get('canceled', 0)}",
            "",
        ]
        if not quotes:
            lines.append("<i>目前沒有掛單中的限價單（若處於暫停狀態或市場暫無闊價差目標會自動空倉待命）。</i>")
        else:
            for i, q in enumerate(quotes, 1):
                st_zh = STRAT_NAMES.get(q.get("strategy"), q.get("strategy"))
                lines.append(
                    f"<b>{i}. [{_esc(st_zh)}] {_esc(q.get('title'))}</b>\n"
                    f"   掛買：<code>{_esc(q.get('lead'))}</code> @ <b>{q.get('price')}</b> × {q.get('shares')} 股\n"
                    f"   預期鎖定：<b>+{_usd(q.get('profit'))}</b>（+{q.get('edge_pct')}%）｜已掛 {int(q.get('age_s') or 0)} 秒"
                )
        kb_rows: list[list[dict[str, Any]]] = [
            [
                {"text": "🔄 刷新掛單", "callback_data": "nav:quotes"},
                {"text": "🗑️ 一鍵撤銷全部掛單", "callback_data": "act:cancel_q"},
            ],
            [
                {"text": "⚙️ 調校掛單參數", "callback_data": "sc:maker"},
                {"text": "🏠 主面板", "callback_data": "nav:home"},
            ],
        ]
        return "\n".join(lines), {"inline_keyboard": kb_rows}

    def render_holds(self) -> tuple[str, dict]:
        t = self.monitor.tick()
        holds = t.get("holds") or []
        lc = (t["engine"].get("lifecycle")) or {}
        lines = [
            "📦 <b>待結算持倉與自動贖回</b>",
            f"持倉筆數：<b>{len(holds)}</b>｜總成本：<b>{_usd(t['kpi']['hold_exposure'])}</b>｜鎖定利潤：<b>{_usd(t['kpi']['locked_profit'], True)}</b>",
            f"0.995 掛賣中：<b>{lc.get('open_flips', 0)}</b>｜已完成閃電平倉：<b>{lc.get('flips_completed', 0)}</b>｜已自動贖回：<b>{lc.get('redemptions', 0)}</b>",
            "",
        ]
        if not holds:
            lines.append("<i>目前沒有鎖定中的待結算持倉，全部資金處於流動狀態。</i>")
        else:
            for i, h in enumerate(holds[:15], 1):
                st_zh = STRAT_NAMES.get(h.get("strategy"), h.get("strategy"))
                exit_tag = f"｜掛賣 @{h['exit_price']:.3f}" if h.get("exit_price") else f"｜結算 {_hkt(h.get('resolves_at'))}"
                lines.append(
                    f"<b>{i}. [{_esc(st_zh)}] {_esc(h.get('title'))}</b>\n"
                    f"   成本 {_usd(h.get('cost'))} → 保證派彩 {_usd(h.get('payout'))}（<b>{_usd(h.get('profit'), True)}</b>）{_esc(exit_tag)}"
                )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🔄 刷新持倉", "callback_data": "nav:holds"},
                    {"text": "⚡ 立即檢查贖回 / 對帳", "callback_data": "act:sync_lc"},
                ],
                [
                    {"text": "⚙️ 風控與額度設定", "callback_data": "sc:risk"},
                    {"text": "🏠 主面板", "callback_data": "nav:home"},
                ],
            ]
        }
        return "\n".join(lines), kb

    def render_trades(self) -> tuple[str, dict]:
        execs = list(self.monitor.executions)[-8:]
        opps = list(self.monitor.opportunities)[-6:]
        lines = [f"📜 <b>最近成交紀錄（共 {self.monitor.exec_counts['sent']} 次）</b>"]
        if not execs:
            lines.append("<i>暫時未有成交紀錄。</i>")
        else:
            for x in reversed(execs):
                st_zh = STRAT_NAMES.get(x.get("strategy"), x.get("strategy"))
                icon = "⚠️" if x.get("orphaned") else ("✅" if x.get("complete") else "◐")
                lines.append(
                    f"{icon} <code>{_hkt(x.get('ts'))[6:]}</code> [{_esc(st_zh)}] <b>{_esc(x.get('title')[:42])}</b>\n"
                    f"   投入 {_usd(x.get('spent'))}｜已實現 <b>{_usd(x.get('realized_pnl'), True)}</b>｜預期 {_usd(x.get('expected_profit'), True)}｜<code>{_esc(x.get('action'))}</code>"
                )
        lines.append("")
        lines.append(f"🔎 <b>最近達標機會（累計去重 {len(self.monitor.opps_seen)} 個）</b>")
        if not opps:
            lines.append("<i>暫時未有觸發正式門檻的純 Taker 機會。</i>")
        else:
            for o in reversed(opps):
                st_zh = STRAT_NAMES.get(o.get("strategy"), o.get("strategy"))
                lines.append(
                    f"• <code>{_hkt(o.get('ts'))[6:]}</code> [{_esc(st_zh)}] {_esc(o.get('title')[:38])} "
                    f"（<b>{_usd(o.get('profit'), True)}</b> / {o.get('edge_pct')}%）"
                )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🔄 刷新紀錄", "callback_data": "nav:trades"},
                    {"text": "📡 最接近套利排行", "callback_data": "nav:near:negrisk_convert"},
                ],
                [{"text": "🏠 返回主面板", "callback_data": "nav:home"}],
            ]
        }
        return "\n".join(lines), kb

    def render_near(self, strat_key: str) -> tuple[str, dict]:
        st_map = (self.monitor.ranking or {}).get("strategies") or {}
        if strat_key not in st_map and st_map:
            strat_key = next(iter(st_map))
        data = st_map.get(strat_key) or {}
        st_zh = STRAT_NAMES.get(strat_key, strat_key)
        lines = [
            f"📡 <b>最接近套利排行｜{_esc(st_zh)}</b>",
            f"已定價目標：<b>{data.get('priced', 0)}</b>｜正邊際數目：<b>{data.get('positive', 0)}</b>",
            "",
        ]
        top = data.get("top") or []
        if not top:
            lines.append("<i>正在等待首次排行計算（每 20 秒更新）…</i>")
        else:
            for i, row in enumerate(top[:10], 1):
                edge_c = (row.get("edge") or 0.0) * 100.0
                mark = "🟢" if edge_c > 0 else "▫️"
                lines.append(f"{mark} <b>#{i}</b> <code>{edge_c:+6.2f}¢</code>（{row.get('legs')}腿）{_esc(row.get('title')[:44])}")
        tabs = [
            {"text": ("✅ " if k == strat_key else "") + label, "callback_data": f"nav:near:{k}"}
            for k, label in (
                ("negrisk_convert", "轉換"),
                ("metar_lock", "METAR"),
                ("binance_lock", "幣安"),
                ("twin_arb", "雙生"),
                ("ladder", "階梯"),
            )
        ]
        kb = {
            "inline_keyboard": [
                tabs,
                [
                    {"text": "🔄 刷新排行", "callback_data": f"nav:near:{strat_key}"},
                    {"text": "🏠 主面板", "callback_data": "nav:home"},
                ],
            ]
        }
        return "\n".join(lines), kb

    def render_metar(self) -> tuple[str, dict]:
        feed = self.monitor.engine.metar_feed
        if feed is None:
            return (
                "🌤️ <b>METAR 氣象源未啟用</b>",
                {"inline_keyboard": [[{"text": "⚙️ 前往開啟", "callback_data": "sc:metar"}, {"text": "🏠 主面板", "callback_data": "nav:home"}]]},
            )
        st = feed.stats()
        today_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        summaries = sorted(
            [s for s in feed._summaries.values() if s.obs_date >= today_utc],
            key=lambda s: -s.last_obs_ts,
        )
        lines = [
            "🌤️ <b>NOAA / AWC 全球機場 METAR 實測氣象站</b>",
            f"追蹤機場：<b>{st['stations']}</b> 站｜觀測日：<b>{st['days_tracked']}</b>｜輪詢：<b>{st['polls']}</b> 次",
            f"最後更新：<code>{_hkt(st['last_poll_at'])}</code>",
            "",
            "<b>最新實測極值（前 14 個活躍機場站）：</b>",
        ]
        for s in summaries[:14]:
            fin = "✅已完日" if s.day_finished else f"{s.count}報"
            lines.append(
                f"• <code>{s.icao}</code> ({s.obs_date[5:]})：最高 <b>{s.max_c_whole}°C / {s.max_f}°F</b>｜最低 <b>{s.min_c_whole}°C / {s.min_f}°F</b> [{fin}]"
            )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "⚡ 立即輪詢最新 METAR", "callback_data": "act:poll_metar"},
                    {"text": "⚙️ METAR 參數設定", "callback_data": "sc:metar"},
                ],
                [{"text": "🏠 返回主面板", "callback_data": "nav:home"}],
            ]
        }
        return "\n".join(lines), kb

    def render_settings_categories(self) -> tuple[str, dict]:
        ov_count = len(self.settings.overrides)
        lines = [
            "⚙️ <b>POLYARB 全參數設定中心</b>",
            f"所有修改<b>即時熱更新生效</b>並自動寫入永久磁碟 <code>runtime_settings.json</code>，同時與網頁版控制台同步（目前已自訂覆蓋 <b>{ov_count}</b> 項）。",
            "",
            "請選擇要調校的設定分類：",
        ]
        rows: list[list[dict[str, Any]]] = []
        pair: list[dict[str, Any]] = []
        for cid, title, desc in CATEGORIES:
            specs = BY_CATEGORY.get(cid, [])
            c_ov = sum(1 for s in specs if s.key in self.settings.overrides)
            tag = f" ({c_ov}改)" if c_ov else ""
            lines.append(f"• <b>{_esc(title)}</b>：{_esc(desc)}")
            pair.append({"text": f"{title}{tag}", "callback_data": f"sc:{cid}"})
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        if ov_count > 0:
            rows.append([{"text": f"↩️ 一鍵還原全部自訂設定（{ov_count} 項）", "callback_data": "sr:all"}])
        rows.append([{"text": "🏠 返回主面板", "callback_data": "nav:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def render_settings_category(self, category: str) -> tuple[str, dict]:
        cat_meta = next((c for c in CATEGORIES if c[0] == category), CATEGORIES[0])
        cid, title, desc = cat_meta
        cfg = self.monitor.engine.cfg
        specs = BY_CATEGORY.get(cid, [])
        lines = [
            f"⚙️ <b>{_esc(title)}</b>",
            f"<i>{_esc(desc)}</i>",
            "（點擊開關可一鍵切換；點擊數值項目可揀預設檔位或輸入任意數字；✏️ 代表已自訂）",
            "",
        ]
        rows: list[list[dict[str, Any]]] = []
        for s in specs:
            val = get_cfg_attr(cfg, s.key)
            fmt = format_setting_value(s, val)
            mark = "✏️ " if s.key in self.settings.overrides else ""
            lines.append(f"• {mark}<b>{_esc(s.label)}</b>：<code>{_esc(fmt)}</code>")
            if s.kind == "bool":
                rows.append([{"text": f"{mark}{s.label}：{fmt}", "callback_data": f"st:{s.code}:cat"}])
            else:
                rows.append([{"text": f"{mark}{s.label}：{fmt}", "callback_data": f"si:{s.code}"}])
        c_ov = sum(1 for s in specs if s.key in self.settings.overrides)
        nav_row: list[dict[str, Any]] = [{"text": "⬅️ 返回設定分類", "callback_data": "nav:settings"}]
        if c_ov > 0:
            nav_row.append({"text": f"↩️ 還原此類 ({c_ov})", "callback_data": f"sr:{cid}"})
        rows.append(nav_row)
        rows.append([{"text": "🏠 主面板", "callback_data": "nav:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def render_setting_item(self, spec: SettingSpec) -> tuple[str, dict]:
        cfg = self.monitor.engine.cfg
        cur = get_cfg_attr(cfg, spec.key)
        default_val = get_cfg_attr(self.settings.base_cfg, spec.key)
        lines = [
            f"🔧 <b>{_esc(spec.label)}</b>",
            f"說明：{_esc(spec.desc)}",
            f"設定鍵：<code>{_esc(spec.key)}</code>",
            f"目前數值：<b>{_esc(format_setting_value(spec, cur))}</b>",
            f"系統預設：<code>{_esc(format_setting_value(spec, default_val))}</code>",
        ]
        rows: list[list[dict[str, Any]]] = []
        if spec.kind == "bool":
            rows.append([{"text": f"切換開關（目前：{format_setting_value(spec, cur)}）", "callback_data": f"st:{spec.code}:item"}])
        else:
            preset_btns: list[dict[str, Any]] = []
            for idx, p in enumerate(spec.presets):
                active = abs(float(cur) - float(p)) < 1e-9
                lbl = ("✅ " if active else "") + format_setting_value(spec, p)
                preset_btns.append({"text": lbl, "callback_data": f"sp:{spec.code}:{idx}"})
                if len(preset_btns) == 3:
                    rows.append(preset_btns)
                    preset_btns = []
            if preset_btns:
                rows.append(preset_btns)
            rows.append([{"text": "⌨️ 輸入自訂數值…", "callback_data": f"sa:{spec.code}"}])
        bottom: list[dict[str, Any]] = [{"text": "⬅️ 返回分類", "callback_data": f"sc:{spec.category}"}]
        if spec.key in self.settings.overrides:
            bottom.append({"text": "↩️ 還原預設值", "callback_data": f"sr:{spec.code}"})
        rows.append(bottom)
        return "\n".join(lines), {"inline_keyboard": rows}

    def render_mode(self, confirm_live: bool = False) -> tuple[str, dict]:
        t = self.monitor.tick()
        cur_mode = t["mode"]
        checks = t.get("readiness") or []
        crit_ok = all(c["ok"] for c in checks if c.get("critical"))
        lines = [
            "🔀 <b>交易模式與實盤就緒檢查</b>",
            f"目前運行模式：{'🔴 <b>實盤 LIVE</b>' if cur_mode == 'live' else '🔵 <b>紙盤 PAPER</b>'}",
            "（紙盤與實盤帳本獨立分開存放於 <code>/data/paper</code> 及 <code>/data/live</code>，切換模式會同時更新永久設定跨重啟保留）",
            "",
            "<b>實盤安全條件清單：</b>",
        ]
        for c in checks:
            if not c.get("critical"):
                continue
            ic = "✅" if c["ok"] else "❌"
            det = f"（{c['detail']}）" if c.get("detail") else ""
            lines.append(f"  {ic} <code>{_esc(c['id'])}</code>{_esc(det)}")

        rows: list[list[dict[str, Any]]] = []
        if cur_mode == "live":
            rows.append([{"text": "🔵 立即切換回「紙盤模式 PAPER」", "callback_data": "md:go_paper"}])
        else:
            if not crit_ok:
                lines.append("\n⚠️ <i>尚有必要條件未通過（請先於 Zeabur 環境變數補齊），暫不可切換至實盤。</i>")
            elif confirm_live:
                lines.append(
                    "\n⚠️ <b>請確認切換至實盤 LIVE：</b>\n"
                    "你可以選擇「轉實盤但先保持暫停」（安全核對錢包餘額後再手動按恢復），或「轉實盤並立即開跑」："
                )
                rows.append([{"text": "🔴 確認轉實盤（先保持暫停，最安全）", "callback_data": "md:go_live_paused"}])
                rows.append([{"text": "🔴 確認轉實盤（立即開始自動交易）", "callback_data": "md:go_live_run"}])
            else:
                rows.append([{"text": "🔴 申請切換至「實盤模式 LIVE」…", "callback_data": "md:ask_live"}])
        rows.append([{"text": "🏠 返回主面板", "callback_data": "nav:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def render_kill_confirm(self) -> tuple[str, dict]:
        rk = self.monitor.engine.risk.state
        txt = (
            "🚨 <b>確認重置熔斷開關？</b>\n\n"
            f"目前熔斷狀態：{'已觸發' if rk.killed else '未觸發'}\n"
            f"原因：<code>{_esc(rk.kill_reason or '無')}</code>\n\n"
            "請確認你已檢查 Polymarket 錢包持倉無異常後，再撳下方確認按鈕："
        )
        kb = {
            "inline_keyboard": [
                [{"text": "✅ 我已確認持倉正常，立即重置熔斷", "callback_data": "act:kill_ok"}],
                [{"text": "⬅️ 取消並返回", "callback_data": "nav:home"}],
            ]
        }
        return txt, kb

    def render_skin(self) -> tuple[str, dict]:
        cur = self.settings.preferred_skin
        lines = [
            "🎨 <b>網頁控制台 5 款旗艦外觀主題（Skin Studio）</b>",
            f"目前預設主題：<b>{_esc(SKIN_LABELS.get(cur, cur))}</b>",
            "點擊下方任一主題即可毫秒級即時推送至所有已開啟的桌面版與手機版瀏覽器：",
        ]
        rows = [
            [{"text": ("✅ " if k == cur else "✨ ") + SKIN_LABELS[k], "callback_data": f"sk:{k}"}]
            for k in VALID_SKINS
        ]
        rows.append([{"text": "🏠 返回主面板", "callback_data": "nav:home"}])
        return "\n".join(lines), {"inline_keyboard": rows}
