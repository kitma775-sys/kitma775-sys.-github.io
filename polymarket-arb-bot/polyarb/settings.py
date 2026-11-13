"""Persistent, hot-reloadable runtime settings shared by Engine, Web Dashboard, and Telegram Bot.

Settings hierarchy (later overrides earlier):
    1. ``config/default.yaml`` (or ``-c`` file) + ``POLYARB__*`` environment variables
    2. ``<POLYARB_DATA_DIR>/runtime_settings.json`` (live operator changes from Telegram / Web)

Secrets (private keys, API tokens, passwords) are NEVER stored here or exposed through this module.
"""

from __future__ import annotations

import copy
import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from polyarb.config import Config

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SettingSpec:
    code: str            # compact <=5-char id for Telegram 64-byte callback_data
    key: str             # dotted Config path, e.g. "risk.max_trade_usd"
    category: str        # "risk" | "convert" | "metar" | "maker" | "baskets" | "exec" | "tg"
    label: str           # Traditional Chinese name
    desc: str            # concise explanation shown in detail view & web UI
    kind: str            # "bool" | "float" | "int"
    unit: str = ""       # "usd" | "pct" | "s" | "d" | "price" | "x" | "int" | "h" | ""
    min_val: float = 0.0
    max_val: float = 1e9
    presets: tuple[Any, ...] = ()


CATEGORIES: tuple[tuple[str, str, str], ...] = (
    ("risk", "🛡️ 風控與資金", "本金、單筆上限、持倉額度、流動預備金與熔斷"),
    ("metar", "🌤️ 實測預言機鎖定", "NOAA METAR 氣象站與幣安收盤 K 線 100% 已定局鎖定及 0.995 閃電掛賣"),
    ("maker", "🎯 零費掛單對沖", "postOnly 零手續費主導腿、零對沖保底搶單、回贈折算、池化預留與動態移倉"),
    ("convert", "🔄 轉換套利", "NegRisk 多結果 NO 籃子深度分層轉換與免費 YES 變現"),
    ("baskets", "🪜 雙生・籃子與階梯", "跨事件雙生／蘊含套利、窮舉 YES 籃子與階梯門檻及鎖倉期限"),
    ("exec", "⚡ 執行與引擎", "滑點、複查、中間帶提前收割、自動鏈上贖回、掃描與 WS 容量"),
    ("tg", "🔔 Telegram 通知", "成交推送、閃電／贖回通知、熔斷警報與定時戰報"),
)

SPECS: tuple[SettingSpec, ...] = (
    # --- risk ---
    SettingSpec("r_bk", "risk.bankroll_usd", "risk", "本金規模", "計算回報率與理論可用資金上限（實盤同時受鏈上真實餘額封頂）", "float", "usd", 10.0, 1_000_000.0, (50.0, 70.0, 100.0, 200.0, 500.0, 1000.0)),
    SettingSpec("r_tr", "risk.max_trade_usd", "risk", "單筆交易上限", "每次套利或掛單最多動用的 pUSD 金額", "float", "usd", 2.0, 50_000.0, (10.0, 15.0, 25.0, 50.0, 100.0)),
    SettingSpec("r_hd", "risk.max_hold_exposure_usd", "risk", "總持倉額度上限", "所有待結算持倉合計最多鎖定的 pUSD", "float", "usd", 0.0, 500_000.0, (25.0, 50.0, 100.0, 200.0, 500.0)),
    SettingSpec("r_ev", "risk.max_event_exposure_usd", "risk", "單一事件持倉上限", "同一市場事件最多鎖定的待結算資金", "float", "usd", 0.0, 100_000.0, (20.0, 50.0, 100.0, 250.0)),
    SettingSpec("r_dl", "risk.max_daily_loss_usd", "risk", "單日虧損熔斷", "當日已實現虧損達此金額即觸發熔斷停機", "float", "usd", 1.0, 50_000.0, (5.0, 10.0, 25.0, 50.0, 100.0)),
    SettingSpec("r_lr", "risk.liquid_reserve_usd", "risk", "閃電策略流動預備金", "慢速鎖倉策略不得動用的現金，專留給即時轉換與 METAR 閃電平倉", "float", "usd", 0.0, 100_000.0, (0.0, 10.0, 20.0, 30.0, 50.0)),
    SettingSpec("r_cf", "risk.max_consecutive_failures", "risk", "連續失敗熔斷次數", "連續出現不完整執行的最大容忍次數", "int", "int", 1, 20, (2, 3, 5, 8)),
    SettingSpec("r_cd", "risk.cooldown_s", "risk", "同市場冷卻秒數", "同一事件嘗試交易後的冷卻間隔", "float", "s", 0.0, 3600.0, (10.0, 30.0, 60.0, 120.0)),
    # --- metar ---
    SettingSpec("m_on", "metar.enabled", "metar", "METAR 氣象鎖定開關", "實時追蹤全球 48 個機場 METAR 實測報文捕捉已定局區間", "bool"),
    SettingSpec("m_mp", "metar.min_profit_usd", "metar", "單腿鎖定最低利潤", "METAR 單一區間鎖定單的最低淨利潤門檻", "float", "usd", 0.02, 500.0, (0.08, 0.15, 0.25, 0.50, 1.00)),
    SettingSpec("m_me", "metar.min_edge", "metar", "單腿鎖定最低邊際", "扣除 taker 手續費後的最低淨回報率", "float", "pct", 0.0005, 0.50, (0.002, 0.004, 0.008, 0.015)),
    SettingSpec("m_bp", "metar.basket_min_profit_usd", "metar", "存活籃子最低利潤", "剔除已出局溫度區間後，買齊存活 YES 籃子的最低利潤", "float", "usd", 0.05, 500.0, (0.15, 0.25, 0.50, 1.00)),
    SettingSpec("m_md", "metar.max_days", "metar", "最長距離結算天數", "只交易距離結算不超過此天數的氣象市場", "float", "d", 0.2, 14.0, (1.5, 2.0, 3.0, 5.0)),
    SettingSpec("m_fp", "metar.fast_poll_s", "metar", "發報窗口輪詢秒數", "每小時 :00/:20/:30/:50 機場發報窗口的輪詢間隔", "float", "s", 2.0, 60.0, (3.0, 4.0, 6.0, 10.0)),
    SettingSpec("m_fl", "metar.flip_enabled", "metar", "0.99+ 閃電止盈開關", "買入鎖定倉位後自動以 0.99+ 秒賣或掛 postOnly 賣單快速回籠資金", "bool"),
    SettingSpec("m_fa", "metar.flip_ask_price", "metar", "閃電掛賣目標價", "休息限價賣單的掛單價（0 maker 手續費）", "float", "price", 0.95, 0.999, (0.992, 0.995, 0.997, 0.998)),
    SettingSpec("m_fb", "metar.flip_min_instant_bid", "metar", "即時秒賣最低買盤價", "盤口已有此價位或更高買單時直接秒賣平倉", "float", "price", 0.95, 0.999, (0.988, 0.990, 0.993, 0.995)),
    SettingSpec("m_fm", "metar.flip_min_profit_usd", "metar", "閃電平倉最低利潤", "提早平倉必須鎖定的最低淨利潤", "float", "usd", 0.01, 100.0, (0.05, 0.08, 0.15, 0.30)),
    SettingSpec("b_on", "binance.enabled", "metar", "幣安收盤鎖定開關", "追蹤幣安已收盤 1h/1m 現貨 K 線，毫秒級捕捉每小時／正午／觸價已定局盤口", "bool"),
    SettingSpec("b_mp", "binance.min_profit_usd", "metar", "幣安鎖定最低利潤", "幣安確定性鎖定單的最低美元淨利潤", "float", "usd", 0.02, 500.0, (0.08, 0.12, 0.25, 0.50)),
    SettingSpec("b_bp", "binance.burst_poll_s", "metar", "整點收盤衝刺輪詢秒數", "每小時 :00 整點及正午收盤窗口的高頻輪詢間隔", "float", "s", 0.2, 10.0, (0.3, 0.5, 1.0, 2.0)),
    SettingSpec("b_fl", "binance.flip_enabled", "metar", "幣安鎖定閃電掛賣", "幣安單腿鎖定倉位自動掛 0.995 零費賣單秒速翻倉", "bool"),
    # --- maker ---
    SettingSpec("k_on", "maker.enabled", "maker", "零費掛單對沖開關", "於主導腿掛 postOnly 零費買單，成交即 FOK 對沖（關閉會即撤現有掛單）", "bool"),
    SettingSpec("k_mq", "maker.max_open_quotes", "maker", "同時最多掛單數", "同時在市場上維持的零費限價單上限", "int", "int", 1, 20, (2, 4, 6, 8, 12)),
    SettingSpec("k_jb", "maker.allow_join_bid", "maker", "1-Tick 貼買一掛單", "在 1 tick 窄價差高成交市場允許貼住買一價掛零費 postOnly 單", "bool"),
    SettingSpec("k_qe", "maker.max_quotes_per_event", "maker", "同事件互補雙掛上限", "轉換／保底鎖定事件最多同時掛幾張零費買單", "int", "int", 1, 4, (1, 2, 3)),
    SettingSpec("k_ps", "maker.pool_hedge_slots", "maker", "對沖現金池化峰值數", "除鎖定全部掛單本金外，額外全額預留幾筆同時成交的對沖現金", "int", "int", 1, 10, (1, 2, 3, 6)),
    SettingSpec("k_rp", "maker.repeg_after_s", "maker", "被搶價動態移倉秒數", "盤口有人出更高買價排在前面超過此秒數即自動撤單重掛（0=關閉）", "float", "s", 0.0, 300.0, (0.0, 2.0, 3.0, 5.0, 15.0)),
    SettingSpec("k_de", "maker.deterministic_min_edge", "maker", "零對沖保底掛單邊際", "已定局 METAR／幣安單腿零風險搶單要求的最低邊際", "float", "pct", 0.001, 0.20, (0.003, 0.004, 0.006, 0.010)),
    SettingSpec("k_rb", "maker.rebate_credit_ratio", "maker", "Maker 回贈折算比例", "將 Polymarket 每日 USDC Maker Rebate 計入邊際評分的保守折算率（仍強制當日現金正利潤）", "float", "pct", 0.0, 1.0, (0.0, 0.50, 0.75, 1.00)),
    SettingSpec("k_mp", "maker.min_profit_usd", "maker", "掛單最低預期利潤", "主導腿成交並完成對沖後的最低淨利潤", "float", "usd", 0.05, 500.0, (0.15, 0.25, 0.50, 1.00)),
    SettingSpec("k_me", "maker.min_edge", "maker", "掛單最低開倉邊際", "開立掛單要求的最低淨回報率", "float", "pct", 0.001, 0.50, (0.004, 0.006, 0.010, 0.015)),
    SettingSpec("k_ke", "maker.min_keep_edge", "maker", "盤口偏移撤單底線", "對沖腿價格變動令邊際跌穿此值時立即撤單", "float", "pct", 0.0, 0.20, (0.001, 0.002, 0.004)),
    SettingSpec("k_vl", "maker.min_volume_24h", "maker", "最低 24h 市場成交額", "只揀選有真實交投活躍度的事件掛單", "float", "usd", 0.0, 1_000_000.0, (100.0, 300.0, 500.0, 1000.0, 3000.0)),
    SettingSpec("k_dr", "maker.min_hedge_depth_ratio", "maker", "對沖腿深度安全倍數", "對沖腿頂部掛單量必須達到計劃股數的此倍數", "float", "x", 1.1, 10.0, (1.5, 2.0, 2.5, 3.0)),
    SettingSpec("k_ld", "maker.max_ladder_days", "maker", "掛單鎖倉最長天數", "掛單階梯／籃子／雙生策略允許的最長結算期限", "float", "d", 1.0, 120.0, (7.0, 14.0, 21.0, 30.0)),
    SettingSpec("k_tt", "maker.quote_ttl_s", "maker", "掛單重整存活秒數", "掛單超過此秒數未成交即自動撤單重新選優", "float", "s", 30.0, 3600.0, (120.0, 300.0, 600.0)),
    # --- convert ---
    SettingSpec("c_on", "convert.enabled", "convert", "轉換套利開關", "NegRisk 多結果買 NO 即時鏈上換 pUSD（無結算風險）", "bool"),
    SettingSpec("c_mp", "convert.min_profit_usd", "convert", "轉換最低淨利潤", "扣除 taker 手續費後的最低美元利潤", "float", "usd", 0.02, 500.0, (0.10, 0.15, 0.25, 0.50, 1.00)),
    SettingSpec("c_me", "convert.min_edge", "convert", "轉換最低回報邊際", "利潤除以投入成本的最低比率", "float", "pct", 0.0005, 0.50, (0.0015, 0.003, 0.005, 0.010)),
    SettingSpec("c_ml", "convert.max_legs", "convert", "轉換最多腿數", "單次轉換最多同時買入的結果數（每 15 張一批）", "int", "int", 2, 30, (6, 12, 15, 25)),
    SettingSpec("c_sh", "convert.surplus_haircut", "convert", "免費 YES 殘值折算率", "濃縮轉換時對深度買盤之免費鑄造 YES 計入保底收益的折算率（0=只作額外純利）", "float", "pct", 0.0, 0.98, (0.0, 0.70, 0.85, 0.92)),
    # --- baskets (twin + yes_basket + ladder) ---
    SettingSpec("w_on", "twin.enabled", "baskets", "跨事件雙生套利開關", "配對同預言機之區間盤與累積盤（每日加密幣／每週美股）捕捉零邊界風險差價", "bool"),
    SettingSpec("w_mp", "twin.min_profit_usd", "baskets", "雙生套利最低利潤", "跨事件雙生鎖定單的最低美元淨利潤", "float", "usd", 0.05, 500.0, (0.10, 0.20, 0.40, 1.00)),
    SettingSpec("w_md", "twin.max_days", "baskets", "雙生套利最長天數", "雙生跨盤最長允許鎖倉等待結算的天數", "float", "d", 0.5, 90.0, (2.0, 7.0, 14.0, 30.0)),
    SettingSpec("y_on", "yes_basket.enabled", "baskets", "窮舉 YES 籃子開關", "結果清單完整時買入全部 YES 持有至結算", "bool"),
    SettingSpec("y_mp", "yes_basket.min_profit_usd", "baskets", "YES 籃子最低利潤", "單筆最低鎖定利潤", "float", "usd", 0.05, 500.0, (0.25, 0.50, 1.00, 2.00)),
    SettingSpec("y_ap", "yes_basket.min_apr", "baskets", "YES 籃子最低年化", "按鎖倉天數折算的最低年化回報率", "float", "pct", 0.0, 10.0, (0.15, 0.25, 0.40, 0.60)),
    SettingSpec("y_md", "yes_basket.max_days", "baskets", "YES 籃子最長天數", "最長允許鎖倉等待結算的天數", "float", "d", 1.0, 365.0, (7.0, 14.0, 21.0, 45.0)),
    SettingSpec("l_on", "ladder.enabled", "baskets", "蘊含階梯套利開關", "強弱命題價格倒掛時買弱 YES + 強 NO", "bool"),
    SettingSpec("l_mp", "ladder.min_profit_usd", "baskets", "階梯最低利潤", "單筆最低鎖定利潤", "float", "usd", 0.05, 500.0, (0.25, 0.50, 1.00, 2.00)),
    SettingSpec("l_ap", "ladder.min_apr", "baskets", "階梯最低年化", "按鎖倉天數折算的最低年化回報率", "float", "pct", 0.0, 10.0, (0.15, 0.20, 0.35, 0.50)),
    SettingSpec("l_md", "ladder.max_days", "baskets", "階梯最長天數", "最長允許鎖倉等待結算的天數", "float", "d", 1.0, 365.0, (7.0, 14.0, 21.0, 45.0, 90.0)),
    # --- exec & engine ---
    SettingSpec("e_sl", "execution.slippage_ticks", "exec", "首次送單滑點空間", "FOK 限價額外放寬的 tick 數（0 最嚴格保護價格）", "int", "int", 0, 5, (0, 1, 2)),
    SettingSpec("e_rc", "execution.recheck_books", "exec", "送單前 REST 複查", "送單前重新拉取訂單簿確認機會仍在並重新定量", "bool"),
    SettingSpec("e_fw", "execution.fast_ws_skip_recheck_s", "exec", "閃電單免複查秒數", "閃電策略若 WS 訂單簿在此秒數內更新則直接送單搶速", "float", "s", 0.0, 10.0, (0.0, 1.0, 1.5, 3.0)),
    SettingSpec("e_ar", "execution.auto_redeem", "exec", "到期自動鏈上贖回", "持倉結算勝出後自動經 Relayer 免 gas 贖回成 pUSD", "bool"),
    SettingSpec("e_ee", "execution.early_exit_enabled", "exec", "中間帶暴利／提前收割", "多腿持倉一旦單腿買盤 ≥0.992 鎖定回本即自動止盈，並同步變現存活副腿賺取雙重派彩", "bool"),
    SettingSpec("e_eb", "execution.early_exit_min_bid", "exec", "提前收割主腿最低買價", "觸發無風險提前收割要求的主導腿最低買盤價", "float", "price", 0.95, 0.999, (0.988, 0.992, 0.995, 0.997)),
    SettingSpec("e_uw", "execution.max_unwind_loss_usd", "exec", "平倉最大容忍虧損", "部分成交平倉若虧損超過此數則保留倉位並熔斷", "float", "usd", 0.5, 500.0, (2.0, 5.0, 10.0, 25.0)),
    SettingSpec("e_sw", "engine.rest_sweep_s", "exec", "全量掃描間隔", "全市場 35,000+ 訂單簿 REST 掃描週期", "float", "s", 3.0, 60.0, (6.0, 10.0, 15.0, 20.0)),
    SettingSpec("e_ws", "engine.ws_max_targets", "exec", "WS 即時監控目標數", "納入毫秒級 WebSocket 串流的熱門目標上限", "int", "int", 50, 2000, (300, 600, 900, 1200)),
    # --- tg notifications ---
    SettingSpec("t_ex", "tg.notify_executions", "tg", "成交通知推送", "每次套利或掛單對沖成交時即時推送到 Telegram", "bool"),
    SettingSpec("t_fr", "tg.notify_flips_redeems", "tg", "閃電平倉／贖回推送", "0.995 閃電止盈成交或自動鏈上贖回完成時即時推送", "bool"),
    SettingSpec("t_rk", "tg.notify_risk", "tg", "風控與狀態警報", "熔斷觸發、模式切換或暫停／恢復時即時推送", "bool"),
    SettingSpec("t_op", "tg.notify_opportunities", "tg", "達標機會即時推送", "發現高利潤套利機會時推送（預設關閉以免洗版）", "bool"),
    SettingSpec("t_om", "tg.opportunity_min_profit_usd", "tg", "機會推送最低利潤", "開啟機會推送時，只推送預期利潤達此金額的機會", "float", "usd", 0.1, 500.0, (0.25, 0.50, 1.00, 3.00, 5.00)),
    SettingSpec("t_sh", "tg.summary_hours", "tg", "定時匯總戰報間隔", "每隔幾小時主動發送一次盈虧與運行戰報（0 = 關閉）", "float", "h", 0.0, 48.0, (0.0, 1.0, 4.0, 8.0, 24.0)),
)

BY_CODE: dict[str, SettingSpec] = {s.code: s for s in SPECS}
BY_KEY: dict[str, SettingSpec] = {s.key: s for s in SPECS}
BY_CATEGORY: dict[str, list[SettingSpec]] = {}
for _s in SPECS:
    BY_CATEGORY.setdefault(_s.category, []).append(_s)


def format_setting_value(spec: SettingSpec, value: Any) -> str:
    if spec.kind == "bool":
        return "🟢 開啟" if bool(value) else "⚪ 關閉"
    if value is None:
        return "—"
    v = float(value)
    if spec.unit == "usd":
        return f"${v:,.2f}" if v < 10 else f"${v:,.0f}" if v.is_integer() else f"${v:,.2f}"
    if spec.unit == "pct":
        pct = v * 100.0
        return f"{pct:.2f}%" if pct < 1.0 else f"{pct:.1f}%" if not pct.is_integer() else f"{int(pct)}%"
    if spec.unit == "price":
        return f"{v:.3f}"
    if spec.unit == "s":
        return f"{int(v)} 秒" if v.is_integer() else f"{v:.1f} 秒"
    if spec.unit == "d":
        return f"{int(v)} 日" if v.is_integer() else f"{v:.1f} 日"
    if spec.unit == "h":
        return "關閉" if v <= 0 else (f"{int(v)} 小時" if v.is_integer() else f"{v:.1f} 小時")
    if spec.unit == "x":
        return f"{v:.1f}x"
    if spec.kind == "int":
        return str(int(round(v)))
    return f"{v:g}"


def input_hint(spec: SettingSpec) -> str:
    if spec.unit == "pct":
        lo, hi = spec.min_val * 100.0, spec.max_val * 100.0
        ex = spec.presets[1] * 100.0 if len(spec.presets) > 1 else lo * 2
        return f"請輸入百分比數字（範圍 {lo:g}% ~ {hi:g}%，例如輸入 {ex:g} 代表 {ex:g}%）"
    if spec.unit == "usd":
        return f"請輸入美元數字（範圍 ${spec.min_val:g} ~ ${spec.max_val:g}）"
    if spec.unit == "price":
        return f"請輸入價格（範圍 {spec.min_val:.3f} ~ {spec.max_val:.3f}，例如 0.995）"
    if spec.kind == "int":
        return f"請輸入整數（範圍 {int(spec.min_val)} ~ {int(spec.max_val)}）"
    return f"請輸入數值（範圍 {spec.min_val:g} ~ {spec.max_val:g}）"


def parse_human_value(spec: SettingSpec, text: str) -> Any:
    raw = text.strip().replace(",", "").replace("$", "").replace("x", "").replace("X", "")
    if spec.kind == "bool":
        low = raw.lower()
        if low in ("1", "true", "yes", "on", "開", "開啟"):
            return True
        if low in ("0", "false", "no", "off", "關", "關閉"):
            return False
        raise ValueError("請輸入 開 或 關")
    had_pct = raw.endswith("%")
    if had_pct:
        raw = raw[:-1].strip()
    num = float(raw)
    if spec.unit == "pct":
        # Human input "0.6" or "0.6%" on a pct field means 0.6% (= 0.006).
        num = num / 100.0
    if spec.kind == "int":
        if not num.is_integer():
            raise ValueError("必須為整數")
        return int(num)
    return num


def get_cfg_attr(cfg: Config, key: str) -> Any:
    section, field_name = key.split(".", 1)
    return getattr(getattr(cfg, section), field_name)


def set_cfg_attr(cfg: Config, key: str, value: Any) -> None:
    section, field_name = key.split(".", 1)
    setattr(getattr(cfg, section), field_name, value)


VALID_SKINS = ("spatial", "obsidian", "cyber", "titanium", "orbital")
SKIN_LABELS = {
    "spatial": "量子空間玻璃 · Spatial Vision OS",
    "obsidian": "黑曜石鎏金私銀 · Obsidian Gold",
    "cyber": "超頻脈衝矩陣 · Cybernetic Pulse",
    "titanium": "極晝鈦銀瑞士白 · Titanium Pro",
    "orbital": "星際極光方舟 · Orbital Bento",
}


class SettingsManager:
    """Persists runtime overrides, mode & UI skin preference in ``<data_dir>/runtime_settings.json``."""

    def __init__(self, base_cfg: Config, path: str | Path | None = None) -> None:
        self.base_cfg = copy.deepcopy(base_cfg)
        self.path = Path(path) if path else None
        self.preferred_mode: str | None = None
        self.preferred_skin: str = "spatial"
        self.overrides: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        if not self.path or not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning("failed reading %s (%s); starting with empty overrides", self.path, exc)
            return
        if not isinstance(raw, dict):
            return
        pm = raw.get("preferred_mode")
        if pm in ("paper", "live"):
            self.preferred_mode = pm
        ps = raw.get("preferred_skin")
        if ps in VALID_SKINS:
            self.preferred_skin = ps
        ov = raw.get("overrides")
        if isinstance(ov, dict):
            for k, v in ov.items():
                spec = BY_KEY.get(k)
                if spec is None:
                    continue
                try:
                    self.overrides[k] = self._validate(spec, v)
                except ValueError:
                    continue

    def save(self) -> None:
        if not self.path:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        payload = {
            "preferred_mode": self.preferred_mode,
            "preferred_skin": self.preferred_skin,
            "overrides": self.overrides,
        }
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def _validate(self, spec: SettingSpec, value: Any) -> Any:
        if spec.kind == "bool":
            if isinstance(value, bool):
                return value
            raise ValueError(f"{spec.label} 必須為布林值")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{spec.label} 必須為數值")
        num = float(value)
        if not (spec.min_val - 1e-12 <= num <= spec.max_val + 1e-12):
            lo = format_setting_value(spec, spec.min_val)
            hi = format_setting_value(spec, spec.max_val)
            raise ValueError(f"{spec.label} 超出允許範圍（{lo} ~ {hi}）")
        if spec.kind == "int":
            return int(round(num))
        return round(num, 6)

    def apply_to_config(self, cfg: Config) -> Config:
        """Reset all managed fields on ``cfg`` to ``base_cfg`` then overlay ``self.overrides``."""
        for spec in SPECS:
            base_val = get_cfg_attr(self.base_cfg, spec.key)
            set_cfg_attr(cfg, spec.key, self.overrides.get(spec.key, base_val))
        return cfg

    def build_fresh_config(self) -> Config:
        cfg = copy.deepcopy(self.base_cfg)
        return self.apply_to_config(cfg)

    def set_value(self, key: str, value: Any, cfg: Config | None = None) -> tuple[SettingSpec, Any]:
        spec = BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"未知設定項 {key!r}")
        clean = self._validate(spec, value)
        base_val = get_cfg_attr(self.base_cfg, key)
        if clean == base_val:
            self.overrides.pop(key, None)
        else:
            self.overrides[key] = clean
        self.save()
        if cfg is not None:
            set_cfg_attr(cfg, key, clean)
        return spec, clean

    def reset(self, category: str | None = None, key: str | None = None, cfg: Config | None = None) -> int:
        removed = 0
        if key is not None:
            if key in self.overrides:
                self.overrides.pop(key, None)
                removed = 1
        elif category is not None:
            keys = {s.key for s in BY_CATEGORY.get(category, ())}
            for k in list(self.overrides):
                if k in keys:
                    self.overrides.pop(k, None)
                    removed += 1
        else:
            removed = len(self.overrides)
            self.overrides.clear()
        self.save()
        if cfg is not None:
            self.apply_to_config(cfg)
        return removed

    def set_preferred_mode(self, mode: str | None) -> None:
        if mode not in (None, "paper", "live"):
            raise ValueError(f"invalid mode {mode!r}")
        self.preferred_mode = mode
        self.save()

    def set_preferred_skin(self, skin: str) -> str:
        clean = str(skin or "").strip().lower()
        if clean not in VALID_SKINS:
            raise ValueError(f"invalid skin {skin!r}")
        self.preferred_skin = clean
        self.save()
        return clean

    def public_snapshot(self, cfg: Config) -> dict[str, Any]:
        return {
            "preferred_mode": self.preferred_mode,
            "preferred_skin": self.preferred_skin,
            "overrides_count": len(self.overrides),
            "overrides": dict(self.overrides),
            "categories": [
                {
                    "id": cid,
                    "title": title,
                    "desc": desc,
                    "items": [
                        {
                            "code": s.code,
                            "key": s.key,
                            "label": s.label,
                            "desc": s.desc,
                            "kind": s.kind,
                            "unit": s.unit,
                            "min": s.min_val,
                            "max": s.max_val,
                            "presets": list(s.presets),
                            "value": get_cfg_attr(cfg, s.key),
                            "default": get_cfg_attr(self.base_cfg, s.key),
                            "formatted": format_setting_value(s, get_cfg_attr(cfg, s.key)),
                            "overridden": s.key in self.overrides,
                        }
                        for s in BY_CATEGORY.get(cid, ())
                    ],
                }
                for cid, title, desc in CATEGORIES
            ],
        }
