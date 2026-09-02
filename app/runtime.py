from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

import httpx

from app.broker import FillResult, LiveBroker, PaperBroker, redeem_not_ready, sell_size_dust, setup_buy_orders
from app.fees import taker_cash, taker_fee
from app.config import Env, LIVE_BLOCKER_ZH, clamp_paper_cash, favorite_window_of, format_fill_headline, format_leg_prices, format_share_qty, format_signed_usd, inventory_matches_mode, is_directional_inventory, is_favorite_inventory, is_live_inventory_kind, live_keys_ready, live_switch_blockers, setting_num, strategy_mode_of
from app.hunter import book_quote, favorite_window_key, favorite_lock_reason, favorite_ws_ok, hunt, is_favorite_setup, is_one_leg_setup, is_twap_setup, parse_favorite_dir, summarize_quotes, _top
from app.chainlink import RTDS_RECYCLE_COOLDOWN, RTDS_URL, ChainlinkTape, should_recycle_rtds
from app.twap import chainlink_symbols_for, cheaper_than_first, default_params, future_listing, hunt_assets, hunt_horizons, parse_window, should_scratch, slug_allowed, take_profit_px, trade_leg, twap_entry_reason
from app.wall import note_wall_gate, operator_wall, performance_today
from app.markets import MarketData
from app.paper_sim import TakerSim, asks_cross_bid, confirm_pair, fak_one, market_expired, seconds_left
from app.rescue import RescuePlan, is_redeemable_market, parse_outcome_prices, plan_rescue, walk_dump
from app.risk import approve
from app.store import Store
from app.universe import DEFAULT_ASSETS, DEFAULT_TAGS
from app.ws_books import WS_MARKET, BookCache


def fmt_exc(exc: BaseException) -> str:
    text = str(exc).strip() or repr(exc)
    return f"{type(exc).__name__}: {text}"[:300]


def mode_inventory(rt: Runtime, *, open_only: bool = True) -> list[dict]:
    live = rt.mode() == "live"
    rows = rt.store.inventory_open() if open_only else rt.store.inventory()
    return [r for r in rows if inventory_matches_mode(r.get("kind"), live=live)]


def leftover_paper_inventory(rt: Runtime) -> list[dict]:
    """Paper rows still open after a live flip. They only settle into the paper book."""
    if rt.mode() != "live":
        return []
    return [r for r in rt.store.inventory_open() if not is_live_inventory_kind(r.get("kind"))]


def store_live_usdc(rt: Runtime, usdc: float) -> None:
    rt.live_usdc = round(float(usdc), 6)
    rt._live_usdc_at = time.time()
    rt.store.kv_set("live_usdc", json.dumps({"usdc": rt.live_usdc, "ts": rt._live_usdc_at}))


def load_live_usdc(rt: Runtime) -> None:
    if rt.live_usdc is not None:
        return
    raw = rt.store.kv_get("live_usdc")
    if not raw:
        return
    try:
        row = json.loads(raw)
        rt.live_usdc = float(row["usdc"] if isinstance(row, dict) else row)
        rt._live_usdc_at = float((row or {}).get("ts") or 0) if isinstance(row, dict) else 0.0
    except (TypeError, ValueError, json.JSONDecodeError, KeyError):
        return


async def refresh_live_usdc(rt: Runtime, *, force: bool = False) -> float | None:
    if rt.mode() != "live":
        return None
    now = time.time()
    if not force and now - float(getattr(rt, "_live_usdc_at", 0) or 0) < 45:
        return rt.live_usdc
    fn = getattr(rt.broker(), "collateral_usdc", None)
    if not callable(fn):
        return rt.live_usdc
    try:
        usdc = await fn()
    except Exception as exc:
        rt.store.add_event("warn", f"live usdc {fmt_exc(exc)}"[:180])
        return rt.live_usdc
    if usdc is None:
        return rt.live_usdc
    store_live_usdc(rt, float(usdc))
    return rt.live_usdc


def operator_board(rt: Runtime) -> dict[str, Any]:
    """Mode-aware operator snapshot. Telegram home and the dashboard share this."""
    s = rt.settings()
    live = rt.mode() == "live"
    inv = mode_inventory(rt)
    leftover_n = len(leftover_paper_inventory(rt))
    last = rt.last_loop or {}
    if s.get("killed"):
        state = "🆘 已緊急停機"
    elif not s.get("engine_running"):
        state = "⏸ 暫停緊"
    elif rt.circuit_tripped():
        state = "🧊 日虧熔斷（停新倉）"
    else:
        state = "🟢 全自動運行中" if s.get("auto_execute") else "🟡 只掃描，唔落單"
    notes: list[str] = []
    halted = bool(live and rt.clob_halted())
    if halted:
        notes.append("⏸ Polymarket CLOB 全站暫停 · https://status.polymarket.com")
    if s.get("twap_reverse"):
        notes.append("🔄 逆向思維開緊：買 TWAP lead 對家，持有到結算")
    else:
        tp = setting_num(s, "twap_tp_bid", 0.87)
        if tp > 1e-12:
            notes.append(f"💰 止賺 {int(round(tp * 100))}¢：全倉 bid 夠價先走，弱倉 scratch 照舊")
        confirm_px = setting_num(s, "twap_confirm_px", 0.62)
        confirm_left = setting_num(s, "twap_confirm_left", 90.0)
        confirm_fair = setting_num(s, "twap_confirm_fair", 0.60)
        dump_bits = ["第一下 6bps 唔追平"]
        if confirm_px > 1e-12 and confirm_left > 1e-12:
            dump_bits.append(f"{int(round(confirm_left))}s 未印 {int(round(confirm_px * 100))}¢ dump")
        if confirm_fair > 1e-12:
            dump_bits.append(f"oracle <{confirm_fair:.2f} 都 dump")
        notes.append("🔒 " + "；".join(dump_bits))
    hunted = [str(a).upper() for a in hunt_assets(s) if str(a).strip()]
    if hunted and set(a.lower() for a in hunted) <= {"btc", "eth"}:
        notes.append("🎯 只hunt BTC+ETH")
    stake = float(s.get("max_usd_per_trade") or 5)
    open_cost = round(sum(float(r.get("cost") or 0) for r in inv), 2)
    perf = performance_today(rt)
    base = {
        "mode": "live" if live else "paper",
        "state": state,
        "stake": stake,
        "open_n": len(inv),
        "open_cost": open_cost,
        "ws": rt.ws_status,
        "chainlink": rt.chainlink_status,
        "halted": halted,
        "notes": notes,
        "signals": last.get("signals"),
        "fills": last.get("fills"),
        "rev": int(s.get("strategy_rev") or 0),
        "leftover_paper_n": leftover_n,
        "hit_rate": perf["hit_rate"],
        "hit_wins": perf["wins"],
        "hit_losses": perf["losses"],
        "hit_held": perf["held"],
        "scratch_n": perf["scratch_n"],
        "hit_label": perf["hit_label"],
    }
    if live:
        usdc = rt.live_usdc
        base.update(
            {
                "cash_label": "可用 USDC",
                "cash": None if usdc is None else round(float(usdc), 2),
                "today_pnl": round(float(rt.store.today_pnl(mode="live")), 2),
                "equity": None,
                "starting": None,
                "reserved": None,
                "total_pnl": None,
                "resting_n": 0,
            }
        )
        return base
    p = rt.store.paper_state()
    base.update(
        {
            "cash_label": "紙盤現金",
            "cash": round(float(p["cash"]), 2),
            "today_pnl": round(float(p["today_pnl"]), 2),
            "equity": round(float(p["equity"]), 2),
            "starting": round(float(p["starting"]), 2),
            "reserved": round(float(p.get("reserved") or 0), 2),
            "total_pnl": round(float(p["total_pnl"]), 2),
            "resting_n": int(p.get("resting") or 0),
        }
    )
    return base


def _gasless_key_error(exc: BaseException) -> bool:
    text = str(exc).lower()
    return "builder api key" in text or "relayer api key" in text


def _call_spender(call) -> str:
    data = str(getattr(call, "data", "") or "")
    if len(data) < 74:
        return ""
    return "0x" + data[34:74]


_CORE_CLOB_SPENDERS = (
    "standard_exchange",
    "neg_risk_exchange",
    "collateral_adapter",
    "neg_risk_collateral_adapter",
)


async def _missing_core_clob_approvals(client) -> list[str]:
    """Spenders the 5m CLOB actually needs. Perps / auto-redeem extras are ignored."""
    try:
        from polymarket._internal.actions.relayer.approvals import resolve_missing_trading_approval_calls
    except ImportError:
        return []
    ctx = getattr(client, "_ctx", None)
    if ctx is None:
        return []
    cfg = ctx.environment_config
    core = {
        str(getattr(cfg, name, "") or "").lower()
        for name in _CORE_CLOB_SPENDERS
        if getattr(cfg, name, None)
    }
    calls = await resolve_missing_trading_approval_calls(ctx.rpc, wallet=ctx.wallet, config=cfg)
    hit: list[str] = []
    for call in calls:
        spender = _call_spender(call).lower()
        if spender in core and spender not in hit:
            hit.append(spender)
    return hit


async def arm_live_wallet(rt: Runtime) -> str | None:
    """Create the CLOB client and run trading approvals. Returns an error string or None.

    Does not set live_trading. Tests may set rt.skip_live_preflight = True.
    Gnosis Safe extras (perps, auto-redeem operator) need a Builder/Relayer API
    key. CLOB FAK does not, so a gasless-only failure is not a live-trading block
    when core exchange allowances already exist.
    """
    if getattr(rt, "skip_live_preflight", False):
        return None
    blockers = live_switch_blockers(rt.env, rt.geo)
    if blockers:
        return "；".join(LIVE_BLOCKER_ZH.get(b, b) for b in blockers)
    rt.live_onchain_limited = False
    try:
        broker = LiveBroker(rt.env.private_key, wallet=rt.env.wallet)
        client = await broker._client_ready()
        try:
            await client.setup_trading_approvals()
        except Exception as exc:
            if not _gasless_key_error(exc):
                return f"錢包授權失敗：{fmt_exc(exc)}"[:180]
            missing = await _missing_core_clob_approvals(client)
            if missing:
                return "Gnosis Safe 未授權 CLOB 交易所；補授權要 Builder／Relayer API key"[:180]
            rt.live_onchain_limited = True
            rt.store.add_event(
                "warn",
                "live preflight: CLOB 核心授權已有；perps／auto-redeem 要 gasless，完場可喺網站 redeem",
            )
        try:
            bal = await client.get_balance_allowance(asset_type="COLLATERAL")
            usdc = int(getattr(bal, "balance", 0) or 0) / 1_000_000
        except Exception as exc:
            return f"讀唔到 USDC 餘額：{fmt_exc(exc)}"[:180]
        rt.live_usdc = usdc
        store_live_usdc(rt, usdc)
        if usdc + 1e-9 < 5.0:
            return f"錢包 USDC ≈ ${usdc:.2f}，少過最低一注 $5"
        try:
            closed = await client.get_closed_only_mode()
            if closed:
                return "CLOB 帳戶 close-only，唔開新倉"
        except Exception:
            pass
    except Exception as exc:
        return f"實盤匙／錢包起唔到：{fmt_exc(exc)}"[:180]
    return None


def http_book_due(*, missing: bool, flicker: bool) -> bool:
    """HTTP only when WS has no pair, or last-3-min books are one-sided empty.

    Polling every 1s across 24 near-expiry markets stalled the CLOB socket
    (1013 slow consumer) and missed 97–99¢ asks.
    """
    return bool(missing or flicker)


def favorite_budget(max_usd: float, inv: dict | None) -> float:
    """Room left under max_usd_per_trade for an existing favorite position."""
    cap = float(max_usd)
    if not inv or not is_directional_inventory(inv.get("kind")):
        return cap
    if float(inv.get("up") or 0) <= 0.01 and float(inv.get("down") or 0) <= 0.01:
        return cap
    spent = float(inv.get("cost") or 0)
    return max(0.0, round(cap - spent, 6))


def favorite_same_window_open(rt: Runtime, slug: str) -> bool:
    """BTC and ETH 5m books with the same start timestamp dump together."""
    key = favorite_window_key(slug)
    if not key:
        return False
    for row in mode_inventory(rt):
        other = str(row.get("slug") or "")
        if not other or other == slug:
            continue
        if favorite_window_key(other) == key:
            return True
    return False


# Seconds before T0 to subscribe the next 5m book. Hunt still skips future_listing.
# +5s matches future_listing slack so a 45.1s-to-open print is not missed.
WS_HUNT_BUFFER_S = 45.0
WS_MAX_TOKENS = 14
WS_TOKENS_PER_SOCKET = 8
WS_SOCKETS = 2
_GATE_RANK = {
    "signal": 0,
    "ready": 1,
    "twap_lead": 2,
    "twap_lead_wild": 2,
    "twap_edge": 3,
    "twap_no_fair": 4,
    "twap_wide": 5,
    "twap_crossed": 6,
    "twap_no_bid": 7,
    "twap_stale": 8,
    "twap_thin": 9,
    "twap_band": 10,
    "twap_late_cheap": 10,
    "twap_no_cheaper": 10,
    "twap_window": 11,
    "twap_no_ptb": 12,
    "twap_no_feed": 13,
    "twap_horizon": 14,
    "twap_asset": 15,
    "twap_oracle": 16,
    "twap_ws_slot": 16,
    "future_listing": 17,
    "twap_conflict": 18,
    "twap_budget": 19,
}


def ws_band_rank(ev: dict) -> int:
    """Rank a book for scarce CLOB WS slots.

    0 = either leg in 45–55, 1 = 40–60, 2 = unknown, 3 = locked/off-band.
    Gamma bestAsk is a stale mid (live 5m pennies still print 0.50). Prefer
    outcomePrices; fall back to bestAsk only when outcomes are missing.
    """
    prices: list[float] = []
    raw = ev.get("outcome_prices")
    if raw is None:
        parsed = parse_outcome_prices(ev.get("outcomePrices"))
        raw = None if parsed is None else [parsed[0], parsed[1]]
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                p = float(x)
            except (TypeError, ValueError):
                continue
            if 0.0 < p < 1.5:
                prices.append(p)
    if not prices:
        try:
            px = ev.get("best_ask")
            if px is not None and px != "":
                p = float(px)
                if 0.0 < p < 1.5:
                    prices.append(p)
        except (TypeError, ValueError):
            pass
    if not prices:
        return 2
    if any(0.45 - 1e-12 <= p <= 0.55 + 1e-12 for p in prices):
        return 0
    if any(0.40 - 1e-12 <= p <= 0.60 + 1e-12 for p in prices):
        return 1
    return 3


def ws_prewarm_future(
    left: float | None,
    window_seconds: int,
    buffer_s: float = WS_HUNT_BUFFER_S,
) -> bool:
    """Subscribe the next window in the last `buffer_s` before T0.

    `left` is seconds until END, so time-to-open is `left - window`.
    Hunt still skips `future_listing`; this is WS pre-warm only.
    """
    if not future_listing(left, window_seconds):
        return False
    return float(left) <= float(window_seconds) + float(buffer_s) + 5.0


def ws_wanted_tokens(
    events: list[dict],
    *,
    params,
    hold_condition_ids: set[str] | None = None,
    extra_tokens: list[str] | None = None,
    ptb_slugs: set[str] | None = None,
    buffer_s: float = WS_HUNT_BUFFER_S,
    max_tokens: int = WS_MAX_TOKENS,
) -> list[str]:
    """CLOB market WS: scarce slots for books we can actually lift.

    14 tokens on two sockets. Inventory always.
    Next 5m: last 45s before T0, no PTB (open print does not exist yet).
    Current: need PTB. Keep locked pennies on the socket so we do not
    reconnect all cycle — drop them only when pre-warm needs the cap.
    Hunt still skips future_listing / twap_no_ptb. 15m/1H are not in the hunt set.
    """
    hold = {str(x) for x in (hold_condition_ids or ()) if x}
    ptb = None if ptb_slugs is None else {str(x) for x in ptb_slugs if x}
    seen: set[str] = set()
    out: list[str] = []
    cap = max(2, int(max_tokens))

    def add_ev(ev: dict) -> bool:
        needed: list[str] = []
        for key in ("up_token", "down_token"):
            tok = str(ev.get(key) or "")
            if tok and tok not in seen:
                needed.append(tok)
        if not needed:
            return False
        if len(out) + len(needed) > cap:
            return False
        for tok in needed:
            seen.add(tok)
            out.append(tok)
        return True

    must: list[dict] = []
    rest: list[tuple[int, int, int, dict]] = []
    for ev in events:
        cid = str(ev.get("condition_id") or "")
        if cid and cid in hold:
            must.append(ev)
            continue
        slug = str(ev.get("slug") or "")
        if not slug_allowed(slug, params):
            continue
        parsed = parse_window(slug)
        left = seconds_left(ev.get("end"))
        if left is None:
            continue
        rank = ws_band_rank(ev)
        hz = 1 if parsed is not None and parsed.horizon == "15m" else 0
        win = parsed.window_seconds if parsed is not None else 300
        if parsed is not None and future_listing(left, win):
            if not ws_prewarm_future(left, win, buffer_s):
                continue
            if rank == 3:
                continue
            # Pre-warm first so locked current pennies cannot fill the cap.
            rest.append((0, rank, hz, ev))
            continue
        if float(left) > float(params.max_left) + float(buffer_s):
            continue
        if ptb is not None:
            key = parsed.slug if parsed else ""
            if key not in ptb:
                continue
        # Keep off-band current for socket hysteresis. Pre-warm (tier 0)
        # fills the cap first at T-45, so pennies only drop when slots are needed.
        rest.append((1 if rank != 3 else 2, rank, hz, ev))

    for ev in must:
        add_ev(ev)
    rest.sort(key=lambda row: (row[0], row[1], row[2]))
    for _tier, _rank, _hz, ev in rest:
        add_ev(ev)
    for tok in extra_tokens or []:
        t = str(tok or "")
        if t and t not in seen and len(out) < cap:
            seen.add(t)
            out.append(t)
    return out


def ws_token_shards(
    tokens: list[str] | tuple[str, ...],
    *,
    per_socket: int = WS_TOKENS_PER_SOCKET,
    sockets: int = WS_SOCKETS,
) -> list[list[str]]:
    """Split CLOB asset ids across sockets so one 14-token dump cannot 1013."""
    toks = [t for t in tokens if t]
    n = max(1, int(per_socket))
    return [toks[i * n : (i + 1) * n] for i in range(max(1, int(sockets)))]


def ws_sub_plan(old_chunk: list[str] | tuple[str, ...], new_chunk: list[str] | tuple[str, ...]) -> dict[str, Any]:
    """Keep vs in-place resub vs idle when a shard's token list changes.

    Polymarket market WS supports ``operation`` subscribe/unsubscribe without
    reconnect. Breaking the socket blanks every book (``initial_dump`` is off)
    until the next delta — that is the 45–55¢ flash miss on 5m prewarm.
    """
    old = [t for t in old_chunk if t]
    new = [t for t in new_chunk if t]
    if not new:
        return {"action": "idle", "add": [], "drop": list(old)}
    old_set = set(old)
    new_set = set(new)
    if old_set == new_set:
        return {"action": "keep", "add": [], "drop": []}
    add = [t for t in new if t not in old_set]
    drop = [t for t in old if t not in new_set]
    return {"action": "resub", "add": add, "drop": drop}


def ws_sub_frames(plan: dict[str, Any]) -> list[str]:
    """Unsubscribe first so a prewarm never briefly holds 8+new on one socket."""
    frames: list[str] = []
    drop = [t for t in (plan.get("drop") or []) if t]
    add = [t for t in (plan.get("add") or []) if t]
    if drop:
        frames.append(json.dumps({"assets_ids": drop, "operation": "unsubscribe"}))
    if add:
        frames.append(
            json.dumps(
                {
                    "assets_ids": add,
                    "operation": "subscribe",
                    "custom_feature_enabled": True,
                    "initial_dump": False,
                }
            )
        )
    return frames


def _event_ask_hint(ev: dict) -> float | None:
    """Cheap ask from Gamma outcomePrices / bestAsk when CLOB is not on WS yet."""
    prices: list[float] = []
    raw = ev.get("outcome_prices")
    if raw is None:
        parsed = parse_outcome_prices(ev.get("outcomePrices"))
        raw = None if parsed is None else [parsed[0], parsed[1]]
    if isinstance(raw, (list, tuple)):
        for x in raw:
            try:
                p = float(x)
            except (TypeError, ValueError):
                continue
            if 0.0 < p < 1.5:
                prices.append(p)
    if not prices:
        try:
            px = ev.get("best_ask")
            if px is not None and px != "":
                p = float(px)
                if 0.0 < p < 1.5:
                    prices.append(p)
        except (TypeError, ValueError):
            pass
    if not prices:
        return None
    return round(min(prices), 4)


def _gate_in_band(gate: dict) -> bool:
    ask = gate.get("ask")
    if ask is None:
        return False
    try:
        px = float(ask)
    except (TypeError, ValueError):
        return False
    return 0.45 - 1e-12 <= px <= 0.55 + 1e-12


def _gate_lead_abs(gate: dict) -> float:
    lead = gate.get("lead_bps")
    if lead is None:
        return -1.0
    try:
        return abs(float(lead))
    except (TypeError, ValueError):
        return -1.0


def gate_better(cur: dict | None, nxt: dict | None) -> bool:
    """Prefer an actionable mid-band book over the nearest locked 1.00 / no-PTB."""
    if nxt is None:
        return False
    if cur is None:
        return True
    r_n = _GATE_RANK.get(str(nxt.get("reason") or ""), 50)
    r_c = _GATE_RANK.get(str(cur.get("reason") or ""), 50)
    if r_n != r_c:
        return r_n < r_c
    n_band, c_band = _gate_in_band(nxt), _gate_in_band(cur)
    if n_band != c_band:
        return n_band
    n_lead, c_lead = _gate_lead_abs(nxt), _gate_lead_abs(cur)
    if abs(n_lead - c_lead) > 1e-9:
        return n_lead > c_lead
    ln, lc = nxt.get("left"), cur.get("left")
    if ln is not None and lc is not None:
        try:
            return float(ln) < float(lc)
        except (TypeError, ValueError):
            return False
    return False


def _twap_clock_key(parsed) -> tuple[int, str]:
    return (int(parsed.start), parsed.asset)


def _remember_twap_clock(rt: Runtime, slug: str) -> None:
    parsed = parse_window(slug)
    if not parsed or parsed.horizon != "5m":
        return
    rt._twap_clocks_taken[_twap_clock_key(parsed)] = float(parsed.start + parsed.window_seconds)


def _trade_leg_px(row: dict) -> float | None:
    try:
        up = float(row.get("up_price") or 0)
        dn = float(row.get("down_price") or 0)
    except (TypeError, ValueError):
        return None
    px = up if up > 0.01 else dn
    if 0.01 < px < 1.0:
        return px
    return None


def _expire_twap_maps(rt: Runtime, now: float | None = None) -> None:
    ts = time.time() if now is None else float(now)
    for slug, (_px, exp) in list(rt._twap_first_px.items()):
        if ts >= float(exp):
            rt._twap_first_px.pop(slug, None)
    open_cids = {str(r.get("condition_id") or "") for r in mode_inventory(rt) if r.get("condition_id")}
    for cid in list(rt._twap_high_bid):
        if cid not in open_cids:
            rt._twap_high_bid.pop(cid, None)


def _hydrate_twap_first_px(rt: Runtime) -> None:
    """Restart-safe: leftover 45¢ after a FOK-kill still counts as second look."""
    if rt._twap_first_hydrated:
        return
    rt._twap_first_hydrated = True
    now = time.time()
    try:
        rows = rt.store.trades_since(
            now - 900.0,
            mode=rt.mode(),
            limit=400,
            statuses=("filled", "dumped", "paper_filled", "paper_dumped", "fok_killed", "paper_fok_killed"),
        )
    except Exception:
        rows = []
    for t in rows:
        slug = str(t.get("slug") or "")
        parsed = parse_window(slug)
        if parsed is None or float(parsed.start + parsed.window_seconds) <= now:
            continue
        px = _trade_leg_px(t)
        if px is None or slug in rt._twap_first_px:
            continue
        rt._twap_first_px[slug] = (float(px), float(parsed.start + parsed.window_seconds))
    for row in mode_inventory(rt):
        slug = str(row.get("slug") or "")
        parsed = parse_window(slug)
        if parsed is None or slug in rt._twap_first_px:
            continue
        shares = float(row.get("up") or 0) + float(row.get("down") or 0)
        cost = float(row.get("cost") or 0)
        if shares <= 0.01 or cost <= 0:
            continue
        rt._twap_first_px[slug] = (cost / shares, float(parsed.start + parsed.window_seconds))


def _twap_first_px_of(rt: Runtime, slug: str) -> float | None:
    _hydrate_twap_first_px(rt)
    _expire_twap_maps(rt)
    row = rt._twap_first_px.get(slug)
    return None if row is None else float(row[0])


def _lock_twap_first_px(rt: Runtime, slug: str, px: float | None) -> None:
    parsed = parse_window(slug)
    if parsed is None or px is None:
        return
    try:
        val = float(px)
    except (TypeError, ValueError):
        return
    if val <= 0 or slug in rt._twap_first_px:
        return
    rt._twap_first_px[slug] = (val, float(parsed.start + parsed.window_seconds))


def _touch_twap_high_bid(rt: Runtime, cid: str, bid: float | None) -> None:
    if not cid or bid is None:
        return
    try:
        px = float(bid)
    except (TypeError, ValueError):
        return
    prev = float(rt._twap_high_bid.get(cid) or 0.0)
    if px > prev:
        rt._twap_high_bid[cid] = px


def _touch_held_high_bid(rt: Runtime, cid: str, up_book: dict, dn_book: dict) -> None:
    row = rt.store.inventory_one(cid)
    up, down = float(row.get("up") or 0), float(row.get("down") or 0)
    if up <= 0.01 and down <= 0.01:
        return
    if up > 0.01 and down > 0.01:
        return
    book = up_book if up > down else dn_book
    _touch_twap_high_bid(rt, cid, _top((book or {}).get("bids") or [], asks=False))


def _hydrate_twap_clocks(rt: Runtime) -> None:
    """Restart-safe: a dumped 5m clock stays taken until T1."""
    if rt._twap_clocks_hydrated:
        return
    rt._twap_clocks_hydrated = True
    now = time.time()
    try:
        rows = rt.store.trades_since(now - 900.0, mode=rt.mode(), limit=400, statuses=("filled", "dumped"))
    except Exception:
        rows = []
    for t in rows:
        parsed = parse_window(str(t.get("slug") or ""))
        if parsed is not None and parsed.horizon == "5m" and float(parsed.start + parsed.window_seconds) > now:
            rt._twap_clocks_taken[_twap_clock_key(parsed)] = float(parsed.start + parsed.window_seconds)
    for row in mode_inventory(rt):
        _remember_twap_clock(rt, str(row.get("slug") or ""))


def twap_conflict_open(rt: Runtime, slug: str) -> bool:
    """Same asset never stacks 5m+15m. BTC and ETH may share a 5m unix.

    After a fill or dump that coin's unix stays taken until T1 so scratch
    cannot reverse the same slug (live SOL −$3.60). Cross-asset lock was a
    live overlay; the shipped first_dump_by90_h2 tape counted coins independently.
    Ended leftover (pending website redeem) must not brick the next 5m.
    """
    parsed = parse_window(slug)
    if not parsed:
        return favorite_same_window_open(rt, slug)
    now = time.time()
    _hydrate_twap_clocks(rt)
    for key, exp in list(rt._twap_clocks_taken.items()):
        if now >= float(exp):
            rt._twap_clocks_taken.pop(key, None)
    if parsed.horizon == "5m" and _twap_clock_key(parsed) in rt._twap_clocks_taken:
        return True
    for row in mode_inventory(rt):
        other = str(row.get("slug") or "")
        if not other or other == slug:
            continue
        peer = parse_window(other)
        if not peer:
            continue
        peer_live = now < float(peer.start + peer.window_seconds)
        if peer.asset == parsed.asset:
            if peer_live or peer.start == parsed.start:
                return True
    return False


def is_clob_unavailable(detail: str, *, http_status=None) -> bool:
    """CLOB matching engine is paused — retrying only spams Telegram."""
    try:
        if http_status is not None and int(http_status) == 503:
            return True
    except (TypeError, ValueError):
        pass
    text = str(detail or "").lower()
    return any(
        token in text
        for token in (
            "trading is disabled",
            "trading is currently disabled",
            "cancel-only",
            "post-only mode",
            "matching engine",
        )
    )


def clob_halt_seconds(detail: str, *, retry_after=None) -> float:
    try:
        if retry_after is not None:
            return max(15.0, min(1800.0, float(retry_after)))
    except (TypeError, ValueError):
        pass
    text = str(detail or "").lower()
    if "cancel-only" in text:
        return 180.0
    if "post-only" in text:
        return 90.0
    if "trading is disabled" in text or "trading is currently disabled" in text:
        return 300.0
    return 90.0


CLOB_HALT_ZH = (
    "⏸ Polymarket CLOB 而家全站暫停（官方 503 trading is disabled），"
    "唔係錢包／USDC／匙問題。"
    "官方狀態：https://status.polymarket.com"
    "Bot 實盤已開，掃描繼續；交易所開返會自動再試。"
    "網站 redeem 仍然用得。"
)


async def _maybe_halt_clob(rt: Runtime, detail: str, payload: dict | None) -> str:
    """'' if not a CLOB outage, 'first' for the opening halt, 'repeat' after that."""
    row = payload if isinstance(payload, dict) else {}
    if not is_clob_unavailable(detail, http_status=row.get("http_status")):
        return ""
    first = rt.trip_clob_halt(detail, seconds=clob_halt_seconds(detail, retry_after=row.get("retry_after")))
    if first:
        rt.store.add_event("warn", f"clob halt: {detail}"[:220])
        await rt.notify(CLOB_HALT_ZH, important=True)
        return "first"
    return "repeat"


def _holding_twap_assets(rt: Runtime) -> tuple[str, ...]:
    out: list[str] = []
    for row in mode_inventory(rt):
        parsed = parse_window(str(row.get("slug") or ""))
        if parsed and parsed.asset not in out:
            out.append(parsed.asset)
    return tuple(out)


def _twap_gate_row(ev: dict, snap, up_book: dict, dn_book: dict, fee_rate: float, params, setup, chainlink=None, first_px: float | None = None) -> dict:
    """Why this TWAP book did or did not produce a lift."""
    left = seconds_left(ev.get("end"))
    up_ask = _top(up_book.get("asks") or [], asks=True)
    if is_twap_setup(setup):
        why = "signal"
        ask = float(setup.up_price or setup.down_price)
        bid = None
    elif snap is None:
        parsed = parse_window(str(ev.get("slug") or ""))
        sym = None if parsed is None else parsed.symbol
        ticks = None if chainlink is None or not sym else chainlink.ticks.get(sym)
        if chainlink is not None and chainlink.connected and ticks:
            why = "twap_no_ptb"
        else:
            why = "twap_no_feed"
        ask, bid = up_ask, _top(up_book.get("bids") or [], asks=False)
    else:
        side = trade_leg(snap, params)
        asks = (up_book.get("asks") or []) if side == "up" else (dn_book.get("asks") or [])
        bids = (up_book.get("bids") or []) if side == "up" else (dn_book.get("bids") or [])
        ask = _top(asks, asks=True)
        bid = _top(bids, asks=False)
        why = twap_entry_reason(
            slug=str(ev.get("slug") or ""),
            snap=snap,
            ask=ask,
            bid=bid,
            left=left,
            fee_rate=fee_rate,
            params=params,
            first_px=first_px,
        ) or "ready"
    fair = None
    if snap is not None and snap.fair_p_up is not None:
        shown = trade_leg(snap, params) if setup is None or not is_twap_setup(setup) else str((setup.extra or {}).get("leg") or snap.side)
        if shown == "up":
            fair = snap.fair_p_up
        elif shown == "down":
            fair = round(1.0 - snap.fair_p_up, 4)
    return {
        "slug": ev.get("slug"),
        "left": None if left is None else round(float(left), 1),
        "lead_bps": None if snap is None else round(float(snap.lead_bps), 3),
        "ask": None if ask is None else round(float(ask), 4),
        "fair": None if fair is None else round(float(fair), 4),
        "reason": why,
        "side": None if snap is None else (str((setup.extra or {}).get("leg")) if is_twap_setup(setup) else trade_leg(snap, params)),
        "reverse": bool(getattr(params, "reverse", False)),
    }


def favorite_taker_replaces_rest(setup, rest: dict | None) -> bool:
    """A 97¢ bid must not block lifting a live 97–99¢ ask on the same slug."""
    if rest is None or setup is None:
        return False
    if setup.kind != "taker" or not is_favorite_setup(setup):
        return False
    return (rest.get("payload") or {}).get("strategy") == "favorite"


class Runtime:
    def __init__(self, store: Store, env: Env):
        self.store = store
        self.env = env
        self.started_at = time.time()
        self.last_loop: dict[str, Any] = {}
        self.geo: dict[str, Any] = {}
        self.notices: asyncio.Queue[dict] = asyncio.Queue(maxsize=200)
        self.http: httpx.AsyncClient | None = None
        self.data: MarketData | None = None
        self._broker = None
        self._broker_mode = ""
        self.cooldown: dict[str, float] = {}
        self._circuit_latch = False
        self._last_loop_error_ts = 0.0
        self._last_ws_error_ts = 0.0
        self._last_ws_info_ts = 0.0
        self.last_ws_error = ""
        self._ws_info_ts: dict[str, float] = {}
        self.books = BookCache()
        self.chainlink = ChainlinkTape()
        self.chainlink.persist_ptb = self._persist_ptb
        self._load_persisted_ptb()
        self.chainlink_status = "off"
        self._twap_scored: dict[str, float] = {}
        self._last_rtds_error_ts = 0.0
        self._last_rtds_recycle_ts = 0.0
        self.universe: list[dict] = []
        self.ws_status = "off"
        self._hunt_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._http_at: dict[str, float] = {}
        self.skip_live_preflight = False
        self.live_usdc = None
        self._live_usdc_at = 0.0
        self.live_onchain_limited = False
        self._clob_halt_until = 0.0
        self._clob_halt_reason = ""
        self._clob_halt_announced = False
        self._clob_halt_backoff = 0.0
        self.wall_tape: list[dict] = []
        self._redeem_wait_logged: set[str] = set()
        self._dump_fail_logged: set[str] = set()
        self._twap_clocks_taken: dict[tuple[int, str], float] = {}
        self._twap_clocks_hydrated = False
        self._twap_first_px: dict[str, tuple[float, float]] = {}
        self._twap_first_hydrated = False
        self._twap_high_bid: dict[str, float] = {}
        load_live_usdc(self)

    def clob_halted(self) -> bool:
        return time.time() < float(self._clob_halt_until or 0)

    def trip_clob_halt(self, reason: str, *, seconds: float = 90.0) -> bool:
        """Pause live CLOB posts. True only for the first Telegram of this outage.

        Re-trips after the pause expires double the wait (cap 30m) so a
        multi-hour `trading is disabled` does not FAK every 5 minutes.
        """
        now = time.time()
        if self.clob_halted():
            self._clob_halt_reason = str(reason or "")[:180]
            return False
        wait = max(15.0, float(seconds))
        if self._clob_halt_announced:
            prev = float(self._clob_halt_backoff or wait)
            wait = min(1800.0, max(wait, prev * 2.0))
        self._clob_halt_backoff = wait
        self._clob_halt_until = now + wait
        self._clob_halt_reason = str(reason or "")[:180]
        if self._clob_halt_announced:
            return False
        self._clob_halt_announced = True
        return True

    def clear_clob_halt(self) -> None:
        self._clob_halt_until = 0.0
        self._clob_halt_reason = ""
        self._clob_halt_announced = False
        self._clob_halt_backoff = 0.0

    def settings(self) -> dict:
        return self.store.settings()

    def paper_bankroll(self) -> float:
        raw = self.settings().get("paper_starting_cash")
        if raw is None or raw == "":
            raw = self.env.paper_starting_cash
        return clamp_paper_cash(raw)

    def mode(self) -> str:
        s = self.settings()
        if self.env.force_paper or not live_keys_ready(self.env) or not s.get("live_trading"):
            return "paper"
        return "live"

    def broker(self):
        mode = self.mode()
        if self._broker is None or self._broker_mode != mode:
            self._broker = LiveBroker(self.env.private_key, wallet=self.env.wallet) if mode == "live" else PaperBroker()
            self._broker_mode = mode
        return self._broker

    async def notify(self, text: str, *, important: bool = False) -> None:
        try:
            self.notices.put_nowait({"text": text, "important": important})
        except asyncio.QueueFull:
            pass

    def circuit_tripped(self) -> bool:
        s = self.settings()
        limit = float(s.get("daily_loss_limit_usd") or 0)
        if limit <= 0:
            return False
        pnl = self.store.paper_state()["today_pnl"] if self.mode() == "paper" else self.store.today_pnl(mode="live")
        return pnl <= -abs(limit)

    def _persist_ptb(self, slug: str, px: float) -> None:
        parsed = parse_window(slug)
        if parsed is None or float(px) <= 0:
            return
        self.store.kv_set(f"ptb:{parsed.slug}", json.dumps({"px": float(px), "ts": time.time()}))

    def _ptb_horizon_ok(self, parsed) -> bool:
        if parsed is None:
            return False
        allowed = set(hunt_horizons(self.settings()) or ("5m",))
        return parsed.horizon in allowed

    def _load_persisted_ptb(self) -> None:
        now = time.time()
        mapping: dict[str, float] = {}
        for key, raw in self.store.kv_prefix("ptb:").items():
            slug = key[4:]
            parsed = parse_window(slug)
            if parsed is None or not self._ptb_horizon_ok(parsed):
                self.store.kv_delete(key)
                continue
            end = parsed.start + parsed.window_seconds
            if end < now - 120:
                self.store.kv_delete(key)
                continue
            try:
                row = json.loads(raw)
                px = float(row["px"] if isinstance(row, dict) else row)
            except (TypeError, ValueError, json.JSONDecodeError, KeyError):
                continue
            if px > 0:
                mapping[parsed.slug] = px
        self.chainlink.load_ptb(mapping)

    def _prune_ptb(self) -> None:
        now = time.time()
        for slug in list(self.chainlink.ptb):
            parsed = parse_window(slug)
            stale = parsed is None or parsed.start + parsed.window_seconds < now - 120
            if stale or not self._ptb_horizon_ok(parsed):
                self.chainlink.ptb.pop(slug, None)
                self.store.kv_delete(f"ptb:{slug}")

    def snapshot(self) -> dict[str, Any]:
        s = self.settings()
        st = self.store.stats()
        paper = self.store.paper_state()
        noisy = {"paper_leg_fill", "paper_resting", "resting"}
        trades = [t for t in self.store.recent_trades(40) if t.get("status") not in noisy]
        if self.mode() == "live":
            trades = [t for t in trades if t.get("mode") != "paper"]
        trades = trades[:15]
        scans = [x for x in self.store.recent_scans(40) if float(x.get("ts") or 0) >= self.started_at - 2][:12]
        leftover = leftover_paper_inventory(self)
        board = operator_board(self)
        return {
            "mode": self.mode(),
            "keys_ready": live_keys_ready(self.env),
            "wallet_set": bool(self.env.wallet),
            "force_paper": self.env.force_paper,
            "live_blockers": live_switch_blockers(self.env, self.geo),
            "uptime_s": int(time.time() - self.started_at),
            "circuit": self.circuit_tripped(),
            "geo": self.geo,
            "settings": s,
            "stats": st,
            "paper": paper,
            "last_loop": self.last_loop,
            "ws_status": self.ws_status,
            "chainlink": self.chainlink.public(),
            "chainlink_status": self.chainlink_status,
            "inventory": mode_inventory(self)[:20],
            "leftover_paper_n": len(leftover),
            "resting": [] if self.mode() == "live" else self.store.resting_open()[:20],
            "trades": trades,
            "scans": scans,
            "events": [e for e in self.store.recent_events(40) if float(e.get("ts") or 0) >= self.started_at - 30][:15],
            "clob_halted": self.clob_halted(),
            "clob_halt_reason": self._clob_halt_reason if self.clob_halted() else "",
            "live_onchain_limited": bool(self.live_onchain_limited),
            "live_usdc": self.live_usdc,
            "board": board,
            "wall": operator_wall(self, board),
        }


async def engine_loop(rt: Runtime) -> None:
    await _ensure_http(rt)
    await asyncio.gather(_universe_loop(rt), _ws_loop(rt), _chainlink_loop(rt), _hunt_loop(rt))


async def _ensure_http(rt: Runtime) -> None:
    if rt.http is not None:
        return
    rt.http = httpx.AsyncClient(
        headers={"User-Agent": "surf-arb-bot/0.2"},
        timeout=httpx.Timeout(12.0, connect=6.0),
        limits=httpx.Limits(max_keepalive_connections=8, max_connections=16),
    )
    rt.data = MarketData(rt.http)
    try:
        rt.geo = await rt.data.geoblock()
        rt.store.add_event("info", f"geoblock={rt.geo}")
    except Exception as exc:
        rt.store.add_event("warn", f"geoblock {fmt_exc(exc)}")


async def _universe_loop(rt: Runtime) -> None:
    backoff = 1.0
    while True:
        s = rt.settings()
        poll = max(0.5, setting_num(s, "poll_seconds", 2.0))
        try:
            await _ensure_http(rt)
            await _refresh_universe(rt)
            await refresh_live_usdc(rt)
            backoff = 1.0
        except Exception as exc:
            detail = fmt_exc(exc)
            rt.store.add_event("error", f"universe {detail}")
            rt.last_loop = {"ts": time.time(), "status": "error", "error": detail, "where": "live_events"}
            now = time.time()
            if now - rt._last_loop_error_ts > 120:
                rt._last_loop_error_ts = now
                await rt.notify(f"⚠️ 引擎出錯：{detail}"[:220], important=True)
            await _reset_http(rt)
            backoff = min(30.0, backoff * 2)
        await asyncio.sleep(max(poll, backoff) if rt.settings().get("engine_running") else 1.0)


async def _refresh_universe(rt: Runtime) -> None:
    s = rt.settings()
    if s.get("killed"):
        n = rt.store.cancel_all_resting("kill")
        live_n = 0
        try:
            live_n = await rt.broker().cancel_open_orders()
        except Exception as exc:
            rt.store.add_event("warn", f"kill cancel_live {fmt_exc(exc)}")
        if n or live_n:
            await rt.notify(f"🛑 已撤 紙盤掛單 {n} · 實盤掛單 {live_n}")
    redeemed = 0
    try:
        redeemed = await _redeem_resolved(rt)
    except Exception as exc:
        rt.store.add_event("warn", f"redeem {fmt_exc(exc)}")
    if s.get("killed"):
        rt.last_loop = {
            "ts": time.time(),
            "status": "killed",
            "tape": (rt.last_loop or {}).get("tape") or {},
            "redeemed": redeemed,
        }
        return
    if not s.get("engine_running"):
        rt.last_loop = {
            "ts": time.time(),
            "status": "paused",
            "tape": (rt.last_loop or {}).get("tape") or {},
            "redeemed": redeemed,
        }
        return
    assert rt.data is not None
    events = await rt.data.live_events(
        s.get("tags") or [s.get("tag") or DEFAULT_TAGS[0]],
        list(s.get("assets") or DEFAULT_ASSETS),
        want=int(s.get("scan_limit") or 16),
        max_horizon=float(s.get("max_horizon_seconds") or 3600),
    )
    hold_cids = {
        str(r.get("condition_id") or "")
        for r in mode_inventory(rt)
        if r.get("condition_id")
    }
    extra_tokens: list[str] = []
    for row in rt.store.resting_open():
        extra_tokens.append(str(row.get("up_token") or ""))
        extra_tokens.append(str(row.get("down_token") or ""))
    rt._prune_ptb()
    prev_wanted = set(rt.books.wanted)
    tokens = ws_wanted_tokens(
        events,
        params=default_params(s),
        hold_condition_ids=hold_cids,
        extra_tokens=extra_tokens,
        ptb_slugs=set(rt.chainlink.ptb),
    )
    rt.universe = events
    rt.books.set_wanted(tokens)
    added = [t for t in tokens if t not in prev_wanted]
    if added:
        try:
            await _prime_ws_books(rt, events, added)
        except Exception as exc:
            rt.store.add_event("warn", f"ws prime {fmt_exc(exc)}")
    rescued = await _rescue_naked(rt, events)
    if rt.circuit_tripped():
        n = rt.store.cancel_all_resting("circuit")
        live_n = 0
        try:
            live_n = await rt.broker().cancel_open_orders()
        except Exception as exc:
            rt.store.add_event("warn", f"circuit cancel_live {fmt_exc(exc)}")
        paper = rt.store.paper_state() if rt.mode() == "paper" else None
        limit = setting_num(s, "daily_loss_limit_usd", 0)
        rt.last_loop = {
            "ts": time.time(),
            "status": "circuit_breaker",
            "markets": len(events),
            "signals": 0,
            "fills": rescued + redeemed,
            "redeemed": redeemed,
            "paper": paper,
            "ws_status": rt.ws_status,
            "tape": (rt.last_loop or {}).get("tape") or {},
            "today_pnl": None if paper is None else paper.get("today_pnl"),
            "daily_loss_limit": limit,
        }
        if not rt._circuit_latch:
            rt._circuit_latch = True
            pnl = paper["today_pnl"] if paper else rt.store.today_pnl()
            rt.store.add_event("warn", f"circuit breaker pnl={pnl:.2f} limit={limit:.2f} cancelled_resting={n} live={live_n}")
            await rt.notify(
                f"🧊 日虧熔斷：今日 PnL ${pnl:.2f} 已穿 −${limit:.0f}。\n"
                "停開新倉，掛單已撤。想繼續今日：Telegram 撳「解除今日熔斷」（今日 PnL 由 0 再計，現金／倉唔清）。"
                "或者等 UTC 零點。唔好重置紙盤除非你想由 $500 再嚟。",
                important=True,
            )
        return
    rt._circuit_latch = False
    rt.last_loop.setdefault("tape", {})
    rt.last_loop.update({"settled": redeemed, "redeemed": redeemed, "rescues": rescued, "markets": len(events)})


async def _ws_loop(rt: Runtime) -> None:
    await asyncio.gather(*(_ws_socket(rt, i) for i in range(WS_SOCKETS)))


async def _ws_socket(rt: Runtime, index: int) -> None:
    backoff = 1.0
    while True:
        if not rt.settings().get("engine_running") or rt.settings().get("killed"):
            if index == 0:
                rt.ws_status = "paused"
                rt.books.connected = False
            await asyncio.sleep(1.0)
            continue
        shards = ws_token_shards(rt.books.wanted)
        chunk = shards[index] if index < len(shards) else []
        if not chunk:
            if index == 0 and not rt.books.wanted:
                rt.ws_status = "idle"
            await asyncio.sleep(0.4)
            continue
        try:
            import websockets
        except ImportError:
            rt.ws_status = "no_lib"
            rt.books.connected = False
            await asyncio.sleep(15)
            continue
        kw: dict[str, Any] = {"ping_interval": None, "max_size": 2**22}
        params = inspect.signature(websockets.connect).parameters
        headers = {"Origin": "https://polymarket.com", "User-Agent": "surf-arb-bot/0.2"}
        if "additional_headers" in params:
            kw["additional_headers"] = headers
        elif "extra_headers" in params:
            kw["extra_headers"] = headers
        if "close_timeout" in params:
            kw["close_timeout"] = 5
        if "open_timeout" in params:
            kw["open_timeout"] = 15

        def _sub(ids: list[str]) -> str:
            # initial_dump of 14–16 books 1013'd this JP host. Deltas only.
            return json.dumps(
                {"assets_ids": ids, "type": "market", "custom_feature_enabled": True, "initial_dump": False}
            )

        try:
            async with websockets.connect(WS_MARKET, **kw) as ws:
                rt.ws_status = "connected"
                rt.books.connected = True
                backoff = 1.0
                await ws.send(_sub(chunk))
                rt.last_ws_error = ""
                now = time.time()
                stamps = getattr(rt, "_ws_info_ts", {})
                key = f"s{index}"
                if now - float(stamps.get(key) or 0) > 60:
                    rt.store.add_event("info", f"ws connected {len(chunk)} tokens shard {index}")
                    stamps[key] = now
                    rt._ws_info_ts = stamps
                ping = asyncio.create_task(_ws_ping(ws))
                try:
                    async for raw in ws:
                        now_shards = ws_token_shards(rt.books.wanted)
                        now_chunk = now_shards[index] if index < len(now_shards) else []
                        plan = ws_sub_plan(chunk, now_chunk)
                        if plan["action"] == "idle":
                            break
                        if plan["action"] == "resub":
                            try:
                                for frame in ws_sub_frames(plan):
                                    await ws.send(frame)
                            except Exception:
                                break
                            chunk = list(now_chunk)
                            now = time.time()
                            stamps = getattr(rt, "_ws_info_ts", {})
                            rkey = f"r{index}"
                            if now - float(stamps.get(rkey) or 0) > 60:
                                rt.store.add_event(
                                    "info",
                                    f"ws resub +{len(plan['add'])} -{len(plan['drop'])} shard {index}",
                                )
                                stamps[rkey] = now
                                rt._ws_info_ts = stamps
                        elif now_chunk != chunk:
                            chunk = list(now_chunk)
                        changed = rt.books.apply_message(raw)
                        if changed:
                            rt._hunt_event.set()
                finally:
                    ping.cancel()
        except Exception as exc:
            rt.ws_status = "down"
            rt.books.connected = False
            detail = fmt_exc(exc)
            rt.last_ws_error = detail
            now = time.time()
            if now - rt._last_ws_error_ts > 120:
                rt._last_ws_error_ts = now
                rt.store.add_event("warn", f"ws shard {index} {detail}")
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 2)
        else:
            if index == 0:
                rt.books.connected = False
                rt.ws_status = "reconnect"
            await asyncio.sleep(0.2)


async def _ws_ping(ws) -> None:
    while True:
        await asyncio.sleep(10)
        try:
            await ws.send("PING")
        except Exception:
            return


def _rtds_connect_kw() -> dict[str, Any]:
    import websockets

    kw: dict[str, Any] = {"ping_interval": None, "max_size": 2**20}
    params = inspect.signature(websockets.connect).parameters
    headers = {"Origin": "https://polymarket.com", "User-Agent": "surf-arb-bot/0.3"}
    if "additional_headers" in params:
        kw["additional_headers"] = headers
    elif "extra_headers" in params:
        kw["extra_headers"] = headers
    if "close_timeout" in params:
        kw["close_timeout"] = 5
    if "open_timeout" in params:
        kw["open_timeout"] = 15
    return kw


async def _chainlink_loop(rt: Runtime) -> None:
    """One RTDS websocket per Chainlink symbol.

    Seven compact subscribe frames on a single socket freeze every feed except
    one after a few minutes — the live 348s-stale BTC/ETH/SOL bug with all coins open.
    PING also keeps a *per-symbol* socket from erroring after ticks stop; recycle
    when ``age_ms`` is stale so ETH/SOL do not sit in ``chainlink_status=partial``.
    """
    workers: dict[str, asyncio.Task] = {}
    last_recycle: dict[str, float] = {}
    try:
        while True:
            s = rt.settings()
            mode = strategy_mode_of(s)
            holding_twap = any(str(x.get("kind") or "").startswith("twap") for x in mode_inventory(rt))
            if s.get("killed") or not s.get("engine_running") or (mode != "twap" and not holding_twap):
                for task in workers.values():
                    task.cancel()
                workers.clear()
                last_recycle.clear()
                rt.chainlink.connected = False
                rt.chainlink_status = "idle"
                await asyncio.sleep(1.0)
                continue
            wanted = chainlink_symbols_for(s, extra_assets=_holding_twap_assets(rt))
            rt.chainlink.symbols = wanted
            now = time.time()
            for sym in wanted:
                task = workers.get(sym)
                if task is not None and not task.done():
                    age = rt.chainlink.age_ms(sym)
                    if should_recycle_rtds(age) and now - last_recycle.get(sym, 0) >= RTDS_RECYCLE_COOLDOWN:
                        last_recycle[sym] = now
                        rt.chainlink.last_error = f"recycle {sym} age_ms={age:.0f}"
                        if now - rt._last_rtds_recycle_ts > 30:
                            rt._last_rtds_recycle_ts = now
                            rt.store.add_event("warn", f"rtds recycle {sym} age_ms={age:.0f}")
                        task.cancel()
                        workers.pop(sym, None)
                        task = None
                if task is None or task.done():
                    workers[sym] = asyncio.create_task(_chainlink_symbol_loop(rt, sym))
            for sym, task in list(workers.items()):
                if sym not in set(wanted):
                    task.cancel()
                    workers.pop(sym, None)
                    last_recycle.pop(sym, None)
            fresh = [sym for sym in wanted if rt.chainlink.age_ms(sym) < 8000]
            if fresh:
                rt.chainlink.connected = True
                rt.chainlink_status = "connected" if len(fresh) >= len(wanted) else "partial"
            else:
                rt.chainlink.connected = False
                if workers:
                    rt.chainlink_status = "connecting"
            await asyncio.sleep(1.0)
    finally:
        for task in workers.values():
            task.cancel()


async def _chainlink_symbol_loop(rt: Runtime, symbol: str) -> None:
    backoff = 1.0
    while True:
        s = rt.settings()
        wanted = chainlink_symbols_for(s, extra_assets=_holding_twap_assets(rt))
        if symbol not in wanted:
            return
        if s.get("killed") or not s.get("engine_running"):
            await asyncio.sleep(1.0)
            continue
        try:
            import websockets
        except ImportError:
            rt.chainlink_status = "no_lib"
            await asyncio.sleep(15)
            continue
        try:
            async with websockets.connect(RTDS_URL, **_rtds_connect_kw()) as ws:
                await ws.send(rt.chainlink.subscribe_frame_for(symbol))
                backoff = 1.0
                ping = asyncio.create_task(_rtds_ping(ws))
                last_check = 0.0
                connected_at = time.time()
                try:
                    async for raw in ws:
                        if rt.chainlink.apply_message(raw):
                            rt._hunt_event.set()
                        now = time.time()
                        if now - last_check < 2.0:
                            continue
                        last_check = now
                        s2 = rt.settings()
                        nxt = chainlink_symbols_for(s2, extra_assets=_holding_twap_assets(rt))
                        if s2.get("killed") or not s2.get("engine_running") or symbol not in nxt:
                            return
                        # PING/PONG keeps recv alive with no ticks. Drop this
                        # socket once the connection itself has had time to print.
                        if now - connected_at >= RTDS_RECYCLE_COOLDOWN and should_recycle_rtds(
                            rt.chainlink.age_ms(symbol)
                        ):
                            break
                finally:
                    ping.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            rt.chainlink.last_error = fmt_exc(exc)
            now = time.time()
            if now - rt._last_rtds_error_ts > 120:
                rt._last_rtds_error_ts = now
                rt.store.add_event("warn", f"rtds {symbol} {rt.chainlink.last_error}")
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 2)
        else:
            await asyncio.sleep(0.4)


async def _rtds_ping(ws) -> None:
    while True:
        await asyncio.sleep(5)
        try:
            await ws.send("PING")
        except Exception:
            return


async def _hunt_loop(rt: Runtime) -> None:
    while True:
        s = rt.settings()
        if s.get("killed") or not s.get("engine_running"):
            await asyncio.sleep(0.4)
            continue
        try:
            await asyncio.wait_for(rt._hunt_event.wait(), timeout=0.25 if rt.books.connected else max(0.5, setting_num(s, "poll_seconds", 2.0)))
        except asyncio.TimeoutError:
            pass
        rt._hunt_event.clear()
        events = list(rt.universe)
        try:
            async with rt._lock:
                await _process_resting(rt)
                if events:
                    await _scan_markets(rt, events)
        except Exception as exc:
            detail = fmt_exc(exc)
            rt.store.add_event("error", f"hunt {detail}")
            await asyncio.sleep(0.5)


async def _scratch_twap(rt: Runtime, events: list[dict]) -> int:
    """Sell weak 5m TWAP inventory. Decide on top bid; dump at bid-walk VWAP.

    Flip/weak/no_fair may dump below the 38¢ better-floor (down to dump_floor).
    Partial size is allowed when the book cannot walk full size. Never hedge.

    Paper `twap` and live `twap_live` stay on separate rows. Scratch only
    walks the current mode's inventory so a Telegram live flip cannot dump
    paper leftovers through the CLOB.
    """
    s = rt.settings()
    if rt.mode() == "live" and rt.clob_halted():
        return 0
    params = default_params(s)
    rescore = setting_num(s, "twap_rescore_seconds", 15.0)
    by_cid = {ev["condition_id"]: ev for ev in events if ev.get("condition_id")}
    n = 0
    now = time.time()
    for inv in list(mode_inventory(rt)):
        kind = str(inv.get("kind") or "")
        if not kind.startswith("twap"):
            continue
        cid = str(inv.get("condition_id") or "")
        if now - rt._twap_scored.get(cid, 0.0) < rescore:
            continue
        rt._twap_scored[cid] = now
        ev = by_cid.get(cid)
        if ev is None:
            continue
        up, down = float(inv.get("up") or 0), float(inv.get("down") or 0)
        if up > 0.01 and down > 0.01:
            continue
        leg = "up" if up > down else "down"
        shares = up if leg == "up" else down
        if shares < 0.01:
            continue
        snap = rt.chainlink.snapshot(
            str(ev.get("slug") or inv.get("slug") or ""),
            lookback=int(params.lookback),
            left=seconds_left(ev.get("end")),
        )
        fair = None if snap is None else snap.fair_p_side
        if snap is not None and snap.side != leg:
            # holding the dog vs current lead
            fair = None if snap.fair_p_up is None else (snap.fair_p_up if leg == "up" else 1.0 - snap.fair_p_up)
        signed = None
        if snap is not None:
            signed = snap.lead_bps if leg == "up" else -snap.lead_bps
        up_book, dn_book, _src = await _pair_books(rt, ev, max_age_ms=setting_num(s, "max_book_age_ms", 60000.0))
        if up_book is None or dn_book is None:
            continue
        book = up_book if leg == "up" else dn_book
        fee_rate = float(ev.get("fee_rate") or s.get("fee_rate") or 0.07)
        left = seconds_left(ev.get("end"))
        bids = book.get("bids") or []
        filled_n, dump_vwap, floor = walk_dump(bids, shares)
        top_bid = _top(bids, asks=False)
        decide_bid = float(top_bid) if top_bid is not None else None
        filled_px = float(inv.get("cost") or 0) / shares if shares else 0.5
        held = parse_window(str(inv.get("slug") or ev.get("slug") or ""))
        _touch_twap_high_bid(rt, cid, decide_bid)
        go, why = should_scratch(
            fair_p=fair,
            lead_bps_signed=signed,
            bid=decide_bid,
            shares=shares,
            fee_rate=fee_rate,
            left=left,
            params=params,
            fill_px=filled_px,
            asset=None if held is None else held.asset,
            high_water=rt._twap_high_bid.get(cid),
        )
        if not go:
            continue
        tp = take_profit_px(params)
        if why == "twap_scratch_tp" and tp is not None:
            filled_n, dump_vwap, floor = walk_dump(bids, shares, min_px=tp)
            if filled_n + 1e-9 < shares:
                continue
        dump_sh = shares if filled_n + 1e-9 >= shares else filled_n
        min_sh = float(s.get("min_shares") or 5)
        if dump_sh < 0.01:
            continue
        if dump_sh + 1e-9 < shares and dump_sh + 1e-9 < min_sh:
            continue
        if dump_vwap <= 0:
            continue
        sell_fee = taker_fee(dump_sh, dump_vwap, fee_rate)
        proceeds = round(max(0.0, dump_sh * dump_vwap - sell_fee), 6)
        plan = RescuePlan(
            "dump",
            round(dump_vwap, 4),
            sell_fee,
            proceeds,
            round(proceeds - filled_px * dump_sh, 6),
            why,
            round(float(floor), 4),
        )
        row = {
            "id": None,
            "slug": inv.get("slug") or ev["slug"],
            "condition_id": cid,
            "shares": dump_sh,
            "up_price": filled_px if leg == "up" else 0.0,
            "down_price": filled_px if leg == "down" else 0.0,
            "up_token": ev["up_token"],
            "down_token": ev["down_token"],
            "kind": str(inv.get("kind") or "twap"),
        }
        missing = "down" if leg == "up" else "up"
        did = await _apply_rescue(rt, row, missing, plan)
        n += did
        if did:
            rt.store.add_event("info", f"twap scratch {row['slug']} {why} @{plan.price}")
    return n


async def _scan_markets(rt: Runtime, events: list[dict]) -> None:
    s = rt.settings()
    if s.get("killed") or not s.get("engine_running"):
        return
    assert rt.data is not None
    circuit = rt.circuit_tripped()
    paper_mode = rt.mode() == "paper"
    paper = rt.store.paper_state() if paper_mode else None
    prev_tape = (rt.last_loop or {}).get("tape") or {}
    rt.last_loop = {
        "ts": time.time(),
        "status": "circuit_breaker" if circuit else "scan",
        "markets": len(events),
        "signals": 0,
        "fills": 0,
        "tape": prev_tape,
        "ws_status": rt.ws_status,
    }
    broker = rt.broker()
    signals = 0
    fills = 0
    snapshot_signals = 0
    fok_kills = 0
    fok_fills = 0
    quotes: list[dict] = []
    book_errors = 0
    stale_pairs = 0
    ws_pairs = 0
    http_pairs = 0
    empty_asks = 0
    max_age = setting_num(s, "max_book_age_ms", 60000.0)
    window = setting_num(s, "maker_window_seconds", 0.0)
    trade_cap = float(s["max_usd_per_trade"])
    twap_params = default_params(s)
    twap_skips: dict[str, int] = {}
    twap_gate: dict | None = None
    hold_cids = {str(r.get("condition_id") or "") for r in mode_inventory(rt) if r.get("condition_id")}
    if not circuit:
        try:
            await _scratch_twap(rt, events)
        except Exception as exc:
            rt.store.add_event("warn", f"twap scratch {fmt_exc(exc)}")
    for ev in events:
        slug = str(ev.get("slug") or "")
        left_now = seconds_left(ev.get("end"))
        parsed = parse_window(slug)
        is_future = parsed is not None and future_listing(left_now, parsed.window_seconds)
        cid = str(ev.get("condition_id") or "")
        holding = bool(cid and cid in hold_cids)
        if is_future and not holding:
            if slug_allowed(slug, twap_params):
                why = "future_listing"
                twap_skips[why] = twap_skips.get(why, 0) + 1
                gate = {
                    "slug": slug,
                    "left": None if left_now is None else round(float(left_now), 1),
                    "lead_bps": None,
                    "ask": _event_ask_hint(ev),
                    "fair": None,
                    "reason": why,
                    "side": None,
                }
                if gate_better(twap_gate, gate):
                    twap_gate = gate
                note_wall_gate(rt, gate)
            continue
        wanted_toks = set(rt.books.wanted)
        up_t = str(ev.get("up_token") or "")
        dn_t = str(ev.get("down_token") or "")
        on_ws = (not up_t or up_t in wanted_toks) and (not dn_t or dn_t in wanted_toks)
        if not holding and not on_ws:
            if slug_allowed(slug, twap_params):
                if left_now is None or left_now > twap_params.max_left:
                    why = "twap_window"
                else:
                    why = "twap_ws_slot"
                twap_skips[why] = twap_skips.get(why, 0) + 1
                gate = {
                    "slug": slug,
                    "left": None if left_now is None else round(float(left_now), 1),
                    "lead_bps": None,
                    "ask": _event_ask_hint(ev),
                    "fair": None,
                    "reason": why,
                    "side": None,
                }
                if gate_better(twap_gate, gate):
                    twap_gate = gate
                note_wall_gate(rt, gate)
            continue
        up_book, dn_book, src = await _pair_books(rt, ev, max_age_ms=max_age)
        if up_book is None or dn_book is None:
            stale_pairs += 1
            continue
        if src == "ws":
            ws_pairs += 1
        else:
            http_pairs += 1
        if not up_book.get("asks") or not dn_book.get("asks"):
            empty_asks += 1
        fee_rate = float(ev.get("fee_rate") or s.get("fee_rate") or 0.07)
        if cid and cid in hold_cids:
            _touch_held_high_bid(rt, cid, up_book, dn_book)
        quotes.append(
            book_quote(
                slug=ev["slug"],
                up_asks=up_book["asks"],
                down_asks=dn_book["asks"],
                up_bids=up_book["bids"],
                down_bids=dn_book["bids"],
                fee_rate=fee_rate,
                end=ev.get("end"),
            )
        )
        if circuit:
            continue
        if not paper_mode and rt.clob_halted():
            twap_skips["clob_halt"] = twap_skips.get("clob_halt", 0) + 1
            note_wall_gate(
                rt,
                {
                    "slug": ev.get("slug"),
                    "left": seconds_left(ev.get("end")),
                    "reason": "clob_halt",
                },
            )
            continue
        slug = str(ev.get("slug") or "")
        if not slug_allowed(slug, twap_params):
            continue
        left_now = seconds_left(ev.get("end"))
        parsed = parse_window(slug)
        is_future = parsed is not None and future_listing(left_now, parsed.window_seconds)
        is_conflict = (not is_future) and twap_conflict_open(rt, slug)
        inv = rt.store.inventory_one(ev["condition_id"])
        max_usd = min(_trade_budget(s, paper), favorite_budget(trade_cap, inv))
        need_shares = max(float(s["min_shares"]), float(ev.get("min_size") or 5))
        hi = float(s.get("twap_max_price") or 0.55)
        is_budget = max_usd + 1e-9 < taker_cash(need_shares, hi, fee_rate)
        snap = None
        setup = None
        if not is_future:
            snap = rt.chainlink.snapshot(
                str(ev.get("slug") or ""),
                lookback=int(twap_params.lookback),
                left=seconds_left(ev.get("end")),
            )
        if not is_future and not is_conflict and not is_budget:
            try:
                setup = hunt(
                    slug=ev["slug"],
                    title=ev["title"],
                    condition_id=ev["condition_id"],
                    up_token=ev["up_token"],
                    down_token=ev["down_token"],
                    up_asks=up_book["asks"],
                    down_asks=dn_book["asks"],
                    up_bids=up_book["bids"],
                    down_bids=dn_book["bids"],
                    max_usd=max_usd,
                    min_shares=max(float(s["min_shares"]), float(ev.get("min_size") or 5)),
                    min_edge=float(s["min_edge"]),
                    fee_rate=fee_rate,
                    prefer_tail=False,
                    tail_confirm=float(s["tail_confirm"]),
                    maker_first=False,
                    end=ev.get("end"),
                    maker_window_seconds=0.0,
                    strategy_mode="twap",
                    twap_snap=snap,
                    twap_params=twap_params,
                    first_px=_twap_first_px_of(rt, slug),
                )
            except Exception as exc:
                rt.store.add_event("warn", f"hunt {ev.get('slug')}: {fmt_exc(exc)}")
                continue
            if setup:
                _lock_twap_first_px(
                    rt,
                    slug,
                    float((setup.extra or {}).get("fill_px") or setup.up_price or setup.down_price),
                )
        gate = _twap_gate_row(
            ev,
            snap,
            up_book,
            dn_book,
            fee_rate,
            twap_params,
            setup,
            chainlink=rt.chainlink,
            first_px=_twap_first_px_of(rt, slug),
        )
        if is_future:
            gate["reason"] = "future_listing"
        elif is_conflict:
            gate["reason"] = "twap_conflict"
        elif is_budget:
            gate["reason"] = "twap_budget"
        why = str(gate.get("reason") or "skip")
        if why not in {"signal", "ready"}:
            twap_skips[why] = twap_skips.get(why, 0) + 1
        if gate_better(twap_gate, gate):
            twap_gate = gate
        note_wall_gate(rt, gate)
        if not setup:
            continue
        if is_one_leg_setup(setup) and not favorite_ws_ok(rt.ws_status, src, up_book, dn_book):
            continue
        if setup.kind == "maker" and window < 3 and not is_favorite_setup(setup):
            continue
        replacing_rest = False
        if paper_mode:
            rest = rt.store.resting_by_slug(setup.slug)
            if rest is not None:
                if favorite_taker_replaces_rest(setup, rest):
                    rt.store.cancel_resting(rest["id"], "favorite_lift")
                    replacing_rest = True
                    rt.store.add_event("info", f"cancel rest {setup.slug} to lift {setup.up_price}+{setup.down_price}")
                else:
                    continue
        if paper_mode:
            setup.extra["paper_slip_ticks"] = int(s.get("paper_slip_ticks") or 0)
        if rt.cooldown.get(setup.slug, 0.0) > time.time() and not replacing_rest:
            continue
        signals += 1
        if paper_mode:
            paper = rt.store.paper_state()
        inv = rt.store.inventory_one(setup.condition_id)
        if (
            (float(inv.get("up") or 0) > 0.01 or float(inv.get("down") or 0) > 0.01)
            and not inventory_matches_mode(inv.get("kind"), live=not paper_mode)
        ):
            continue
        decision = approve(
            setup,
            stale_leg=float(s["stale_leg"]),
            tail_confirm=float(s["tail_confirm"]),
            max_imbalance=float(s["max_imbalance_shares"]),
            inventory_up=float(inv["up"]),
            inventory_down=float(inv["down"]),
            daily_pnl=paper["today_pnl"] if paper else rt.store.today_pnl(mode="live"),
            daily_loss_limit=float(s["daily_loss_limit_usd"]),
            open_markets=len({str(r.get("condition_id") or "") for r in mode_inventory(rt) if r.get("condition_id")}),
            max_open_markets=int(s["max_open_markets"]),
            killed=bool(s["killed"]),
            engine_running=bool(s["engine_running"]),
            auto_execute=bool(s["auto_execute"]),
            cash=paper["cash"] if paper else None,
            cost=setup.cost if paper else None,
            unmatched_shares=rt.store.unmatched_shares(),
            seconds_left=seconds_left(ev.get("end")),
            maker_window=window,
            maker_min_leg=setting_num(s, "maker_min_leg", 0.22),
            maker_max_skew=setting_num(s, "maker_max_skew", 0.10),
            favorite_min_price=setting_num(s, "favorite_min_price", 0.97),
            favorite_max_price=setting_num(s, "favorite_max_price", 0.98),
            favorite_window_seconds=favorite_window_of(s),
            favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
            max_usd_per_trade=trade_cap,
            favorite_spent=float(inv.get("cost") or 0),
            twap_min_price=setting_num(s, "twap_min_price", 0.45),
            twap_max_price=setting_num(s, "twap_max_price", 0.55),
            twap_min_left=setting_num(s, "twap_min_left", 120.0),
            twap_max_left=setting_num(s, "twap_max_left", 280.0),
            twap_late_left=setting_num(s, "twap_late_left", 0.0),
            twap_late_min_price=setting_num(s, "twap_late_min_price", 0.45),
            twap_alt_min_left=setting_num(s, "twap_alt_min_left", 120.0),
            twap_core_assets=s.get("twap_core_assets") or ["btc", "eth"],
        )
        payload = {
            "title": setup.title,
            "kind": setup.kind,
            "up": setup.up_price,
            "down": setup.down_price,
            "shares": setup.shares,
            "net": setup.net,
            "cost": setup.cost,
            "gross": setup.gross,
            "tail": setup.tail,
            "reason": decision.reason,
            "mode": rt.mode(),
            "book": src,
            "strategy": (setup.extra or {}).get("strategy"),
            "leg": (setup.extra or {}).get("leg"),
        }
        rt.store.add_scan(setup.slug, setup.kind, payload)
        if not paper_mode and rt.clob_halted():
            twap_skips["clob_halt"] = twap_skips.get("clob_halt", 0) + 1
            note_wall_gate(rt, {"slug": setup.slug, "reason": "clob_halt", "ask": setup.up_price or setup.down_price})
            continue
        if not decision.ok:
            if s.get("notify_rejects"):
                await rt.notify(f"⏭ 跳過 {setup.title}\n原因：{decision.reason}")
            continue
        if setup.kind == "taker":
            snapshot_signals += 1
        if setup.kind == "taker" and bool(s.get("taker_fok", True)):
            setup.extra["paper_slip_ticks"] = 0
            confirm = await _fok_confirm(rt, ev, setup)
            payload["fok"] = confirm.reason
            payload["fok_up"] = confirm.up_price
            payload["fok_down"] = confirm.down_price
            payload["snapshot_net"] = setup.net
            if not confirm.ok:
                fok_kills += 1
                rt.cooldown[setup.slug] = time.time() + 0.4
                rt.store.add_scan(setup.slug, "taker", {**payload, "reason": confirm.reason})
                rt.store.add_trade(
                    slug=setup.slug,
                    kind="taker",
                    shares=setup.shares,
                    up_price=setup.up_price,
                    down_price=setup.down_price,
                    net=0.0,
                    mode=rt.mode(),
                    status="paper_fok_killed" if paper_mode else "fok_killed",
                    payload={
                        "detail": f"FOK {confirm.reason} snapshot ${setup.net:.2f} @{setup.up_price}+{setup.down_price}",
                        "snapshot_net": setup.net,
                        "snapshot_up": setup.up_price,
                        "snapshot_down": setup.down_price,
                        "fok": confirm.reason,
                    },
                )
                if s.get("notify_signals"):
                    await rt.notify(
                        f"🧪FOK 殺單（舊紙盤會當成交）\n{setup.title}\n"
                        f"snapshot {setup.up_price}+{setup.down_price} × {format_share_qty(setup.shares)} 淨 ${setup.net:.2f}\n"
                        f"確認後：{confirm.reason}",
                    )
                continue
            if confirm.shares > 0:
                setup.shares = round(float(confirm.shares), 4)
                setup.fillable = setup.shares
            setup.up_price = confirm.up_price
            setup.down_price = confirm.down_price
            setup.fees = confirm.fees
            setup.gross = round(1.0 - (confirm.up_price + confirm.down_price), 4)
            if is_twap_setup(setup):
                from app.twap import twap_post_fok_net

                px = float(confirm.up_price or confirm.down_price)
                extra = setup.extra or {}
                setup.net = twap_post_fok_net(
                    reverse=bool(extra.get("reverse")),
                    shares=setup.shares,
                    px=px,
                    fair_p=extra.get("fair_p"),
                    fee_rate=fee_rate,
                )
                setup.extra["cash_cost"] = confirm.cost
                setup.extra["fill_px"] = px
            else:
                setup.net = confirm.net
            setup.extra["fok"] = confirm.reason
            payload["up"] = confirm.up_price
            payload["down"] = confirm.down_price
            payload["net"] = setup.net
            payload["shares"] = setup.shares
            payload["reason"] = confirm.reason
            if paper_mode:
                paper = rt.store.paper_state()
            resized = approve(
                setup,
                stale_leg=float(s["stale_leg"]),
                tail_confirm=float(s["tail_confirm"]),
                max_imbalance=float(s["max_imbalance_shares"]),
                inventory_up=float(inv["up"]),
                inventory_down=float(inv["down"]),
                daily_pnl=paper["today_pnl"] if paper else rt.store.today_pnl(),
                daily_loss_limit=float(s["daily_loss_limit_usd"]),
                open_markets=rt.store.stats()["open_markets"],
                max_open_markets=int(s["max_open_markets"]),
                killed=bool(s["killed"]),
                engine_running=bool(s["engine_running"]),
                auto_execute=bool(s["auto_execute"]),
                cash=paper["cash"] if paper else None,
                cost=setup.cost if paper else None,
                unmatched_shares=rt.store.unmatched_shares(),
                seconds_left=seconds_left(ev.get("end")),
                maker_window=window,
                maker_min_leg=setting_num(s, "maker_min_leg", 0.22),
                maker_max_skew=setting_num(s, "maker_max_skew", 0.10),
                favorite_min_price=setting_num(s, "favorite_min_price", 0.97),
                favorite_max_price=setting_num(s, "favorite_max_price", 0.98),
                favorite_window_seconds=favorite_window_of(s),
                favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
                max_usd_per_trade=trade_cap,
                favorite_spent=float(inv.get("cost") or 0),
                twap_min_price=setting_num(s, "twap_min_price", 0.45),
                twap_max_price=setting_num(s, "twap_max_price", 0.55),
                twap_min_left=setting_num(s, "twap_min_left", 120.0),
                twap_max_left=setting_num(s, "twap_max_left", 280.0),
                twap_late_left=setting_num(s, "twap_late_left", 0.0),
                twap_late_min_price=setting_num(s, "twap_late_min_price", 0.45),
                twap_alt_min_left=setting_num(s, "twap_alt_min_left", 120.0),
                twap_core_assets=s.get("twap_core_assets") or ["btc", "eth"],
            )
            if not resized.ok:
                fok_kills += 1
                rt.cooldown[setup.slug] = time.time() + 0.4
                rt.store.add_scan(setup.slug, "taker", {**payload, "reason": f"fok_{resized.reason}"})
                continue
            rt.store.add_scan(setup.slug, "taker", payload)
        result: FillResult = await broker.execute_pair(setup)
        cool = setting_num(s, "quote_cooldown_seconds", 5.0)
        if is_favorite_setup(setup) and setup.kind == "maker":
            cool = min(cool, 0.4)
        rt.cooldown[setup.slug] = time.time() + cool
        fill_payload = {"detail": result.detail, **(result.payload or {})}
        if setup.kind == "taker" and not fill_payload.get("orders"):
            fill_payload["orders"] = setup_buy_orders(setup)
        if is_twap_setup(setup):
            extra = setup.extra or {}
            if extra.get("fair_p") is not None:
                fill_payload["fair_p"] = extra.get("fair_p")
            if extra.get("lead_bps") is not None:
                fill_payload["lead_bps"] = extra.get("lead_bps")
        halt_kind = ""
        if not paper_mode and not result.ok:
            halt_kind = await _maybe_halt_clob(rt, result.detail, result.payload if isinstance(result.payload, dict) else {})
            if halt_kind == "repeat":
                continue
        rt.store.add_trade(
            slug=setup.slug,
            kind=setup.kind,
            shares=setup.shares,
            up_price=setup.up_price,
            down_price=setup.down_price,
            net=(result.payload or {}).get("net", setup.net) if result.ok and result.status in {"filled", "paper_filled"} and not is_one_leg_setup(setup) else 0.0,
            mode=result.mode,
            status=result.status,
            payload=fill_payload,
        )
        if result.ok and result.status in {"filled", "paper_filled"}:
            fills += 1
            if not paper_mode:
                rt.clear_clob_halt()
            if setup.kind == "taker" and bool(s.get("taker_fok", True)):
                fok_fills += 1
            fill_cost = float((result.payload or {}).get("cost", setup.cost))
            fill_net = float((result.payload or {}).get("net", setup.net))
            fill_up = float((result.payload or {}).get("up_price", setup.up_price))
            fill_down = float((result.payload or {}).get("down_price", setup.down_price))
            fill_shares = float((result.payload or {}).get("shares", setup.shares) or setup.shares)
            if paper_mode:
                try:
                    rt.store.paper_apply_buy(fill_cost)
                except ValueError:
                    rt.store.add_event("warn", f"paper cash race {setup.slug}")
                    continue
            if is_one_leg_setup(setup):
                leg = str((setup.extra or {}).get("leg") or "up")
                up_sh = fill_shares if leg == "up" else 0.0
                dn_sh = fill_shares if leg == "down" else 0.0
                kind = "twap" if is_twap_setup(setup) else "favorite"
                if not paper_mode:
                    kind = kind + "_live"
                rt.store.add_inventory(
                    setup.condition_id,
                    setup.slug,
                    up_sh,
                    dn_sh,
                    kind=kind,
                    cost=fill_cost,
                )
                if is_twap_setup(setup):
                    _remember_twap_clock(rt, setup.slug)
            else:
                rt.store.add_inventory(setup.condition_id, setup.slug, setup.shares, setup.shares)
                if s.get("auto_merge"):
                    merged = rt.store.merge_inventory(setup.condition_id, setup.shares)
                    take = float(merged["merged"] or 0)
                    if take > 0 and paper_mode:
                        net_part = fill_net * (take / setup.shares) if setup.shares else 0.0
                        rt.store.paper_apply_merge(take, net_part)
                    await broker.merge(setup.condition_id, take)
            paper = rt.store.paper_state() if paper_mode else None
            if s.get("notify_signals"):
                flag = "🧪紙盤" if result.mode == "paper" else "🔴實盤"
                book = ""
                if paper:
                    book = (
                        f"\n現金 ${paper['cash']:.2f} · 權益 ${paper['equity']:.2f}"
                        f"\n累計 PnL {format_signed_usd(paper['total_pnl'])} · 今日 {format_signed_usd(paper['today_pnl'])}"
                    )
                label = "TWAP" if is_twap_setup(setup) else ("大熱" if is_favorite_setup(setup) else setup.kind)
                expect = "未結算期望" if is_one_leg_setup(setup) else "淨利"
                payout_line = ""
                if is_one_leg_setup(setup):
                    payout_line = f"贏可取回 ${fill_shares:.2f} · "
                await rt.notify(
                    f"{flag} 成交 {label}\n{setup.title}\n"
                    f"{format_fill_headline(up=fill_up, down=fill_down, shares=fill_shares, cost=fill_cost, leg=(setup.extra or {}).get('leg'))}\n"
                    f"{payout_line}{expect} ${fill_net:.2f}{book}",
                    important=True,
                )
        elif result.ok and result.status in {"paper_resting", "resting"}:
            if not paper_mode:
                rt.clear_clob_halt()
            if window < 3 and not is_favorite_setup(setup):
                rt.store.add_event("info", f"skip rest {setup.slug}: maker window off")
                continue
            if paper_mode:
                try:
                    rt.store.add_resting(
                        slug=setup.slug,
                        condition_id=setup.condition_id,
                        title=setup.title,
                        up_token=setup.up_token,
                        down_token=setup.down_token,
                        shares=setup.shares,
                        up_price=setup.up_price,
                        down_price=setup.down_price,
                        net=setup.net,
                        end=setup.end,
                        payload={
                            "detail": result.detail,
                            "strategy": (setup.extra or {}).get("strategy"),
                            "leg": (setup.extra or {}).get("leg"),
                        },
                    )
                except ValueError as exc:
                    rt.store.add_event("warn", f"paper rest skip {setup.slug}: {exc}")
                    continue
            if s.get("notify_signals"):
                paper = rt.store.paper_state() if paper_mode else None
                lock = f" · 鎖 ${paper['reserved']:.2f}" if paper else ""
                await rt.notify(
                    f"📌 {'紙盤' if paper_mode else '實盤'}掛單 {setup.title}\n"
                    f"{format_leg_prices(setup.up_price, setup.down_price, leg=(setup.extra or {}).get('leg'))} × {format_share_qty(setup.shares)}"
                    f"\n未碰到盤口唔入帳{lock}"
                )
        else:
            if halt_kind:
                continue
            if not paper_mode:
                rt.clear_clob_halt()
            await rt.notify(f"❌ 下單失敗：{result.detail}", important=True)
    tape = summarize_quotes(quotes)
    tape["book_errors"] = book_errors
    tape["stale_pairs"] = stale_pairs
    tape["ws_pairs"] = ws_pairs
    tape["http_pairs"] = http_pairs
    tape["empty_ask_legs"] = empty_asks
    tape["ws_status"] = rt.ws_status
    tape["slugs"] = [ev.get("slug") for ev in events[:24] if ev.get("slug")]
    tape["tags"] = list(s.get("tags") or [s.get("tag") or "5M"])
    tape["taker_fok"] = bool(s.get("taker_fok", True))
    tape["snapshot_signals"] = snapshot_signals
    tape["fok_kills"] = fok_kills
    tape["fok_fills"] = fok_fills
    tape["strategy_mode"] = strategy_mode_of(s)
    tape["chainlink_status"] = rt.chainlink_status
    cl = rt.chainlink.public()
    btc = (cl.get("symbols") or {}).get("btc/usd") or {}
    tape["chainlink_btc"] = btc.get("px")
    eth = (cl.get("symbols") or {}).get("eth/usd") or {}
    tape["chainlink_eth"] = eth.get("px")
    tape["chainlink_age_ms"] = cl.get("age_ms")
    tape["twap_skips"] = twap_skips
    tape["twap_gate"] = twap_gate
    tape["clob_ws_wanted_n"] = len(rt.books.wanted)
    wanted_toks = set(rt.books.wanted)
    tape["clob_ws_slugs"] = [
        str(ev.get("slug") or "")
        for ev in events
        if str(ev.get("slug") or "")
        and (
            str(ev.get("up_token") or "") in wanted_toks
            or str(ev.get("down_token") or "") in wanted_toks
        )
    ][:16]
    tape["last_ws_error"] = rt.last_ws_error or None
    tape["twap_ptb_n"] = len(rt.chainlink.ptb)
    tape["favorite_min"] = setting_num(s, "favorite_min_price", 0.97)
    tape["favorite_max"] = setting_num(s, "favorite_max_price", 0.98)
    tape["favorite_window"] = favorite_window_of(s)
    tape["favorite_dir"] = parse_favorite_dir(s.get("favorite_dir"))
    tape["clob_rtt_ms"] = setting_num(s, "clob_rtt_ms", 150.0)
    rt.last_loop.update(
        {
            "signals": signals,
            "fills": fills,
            "snapshot_signals": snapshot_signals,
            "fok_kills": fok_kills,
            "fok_fills": fok_fills,
            "status": "circuit_breaker" if circuit else "ok",
            "paper": paper,
            "tape": tape,
            "ws_status": rt.ws_status,
        }
    )


async def _fetch_fok_books(rt: Runtime, ev: dict) -> tuple[dict, dict] | None:
    """Delayed-book confirm: prefer a fresh WS pair over a new HTTP RTT.

    Hunt already subscribed these tokens. HTTP after the 250ms itode wait
    ages the delayed book another 100–300ms and is how sticky 45–55 holes
    become `fok_short` / `clob_rtt_miss`.
    """
    up_t = str(ev.get("up_token") or "")
    dn_t = str(ev.get("down_token") or "")
    cached = rt.books.pair(up_t, dn_t, max_age_ms=1500.0) if up_t and dn_t else None
    if cached:
        up_book, dn_book = cached["up"], cached["down"]
        if (up_book.get("asks") or []) and (dn_book.get("asks") or []):
            return up_book, dn_book
    if rt.data is None:
        return None
    try:
        up_book, dn_book = await asyncio.gather(
            rt.data.book(ev["up_token"]),
            rt.data.book(ev["down_token"]),
        )
    except Exception as exc:
        rt.store.add_event("warn", f"fok book {ev.get('slug')}: {fmt_exc(exc)}")
        return None
    return up_book, dn_book


def _confirm_from_books(
    rt: Runtime,
    ev: dict,
    setup,
    up_book: dict,
    dn_book: dict,
    s: dict,
    fee_rate: float,
    paper,
    *,
    allow_requote: bool,
) -> TakerSim:
    if is_favorite_setup(setup):
        return _confirm_favorite(rt, ev, setup, up_book, dn_book, s, fee_rate, paper, allow_requote=allow_requote)
    if is_twap_setup(setup):
        return _confirm_twap(rt, ev, setup, up_book, dn_book, s, fee_rate, paper, allow_requote=allow_requote)
    return confirm_pair(
        setup=setup,
        up_asks=up_book.get("asks") or [],
        down_asks=dn_book.get("asks") or [],
        up_bids=up_book.get("bids") or [],
        down_bids=dn_book.get("bids") or [],
        min_shares=max(float(s["min_shares"]), float(ev.get("min_size") or 5)),
        min_edge=float(s["min_edge"]),
        fee_rate=fee_rate,
        tail_confirm=float(s["tail_confirm"]),
        max_usd=_trade_budget(s, paper),
        prefer_tail=bool(s["prefer_tail"]),
        requote=allow_requote,
    )


async def _fok_confirm(rt: Runtime, ev: dict, setup) -> TakerSim:
    """Wait 250ms, FAK leftover at the locked limit or requote (no cheaper).

    Paper then waits CLOB RTT and re-walks with no requote so a miss is
    `clob_rtt_miss`, not a ghost fill. Live skips that second walk: Rev 10
    already forbade a second wait (requote+itode misses 300–400ms holes).
    First confirm already passed `twap_no_cheaper`; sending FAK is the same
    first-cross sleeve. Exchange itode still revalidates the live order.
    """
    s = rt.settings()
    delay_ms = setting_num(s, "fok_delay_ms", 250.0)
    if delay_ms > 0:
        await asyncio.sleep(min(2.0, delay_ms / 1000.0))
    books = await _fetch_fok_books(rt, ev)
    if books is None:
        reason = "fok_no_http" if rt.data is None else "fok_http"
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, reason)
    up_book, dn_book = books
    fee_rate = float(ev.get("fee_rate") or s.get("fee_rate") or 0.07)
    paper = rt.store.paper_state() if rt.mode() == "paper" else None
    first = _confirm_from_books(rt, ev, setup, up_book, dn_book, s, fee_rate, paper, allow_requote=True)
    rtt = setting_num(s, "clob_rtt_ms", 150.0)
    if rt.mode() == "live":
        rtt = 0.0
    if not first.ok or rtt <= 0:
        return first
    if first.shares > 0:
        setup.shares = round(float(first.shares), 4)
    setup.up_price = first.up_price
    setup.down_price = first.down_price
    await asyncio.sleep(min(2.0, rtt / 1000.0))
    books2 = await _fetch_fok_books(rt, ev)
    if books2 is None:
        return TakerSim(False, first.up_price, first.down_price, 0.0, 0.0, 0.0, False, "clob_rtt_miss")
    second = _confirm_from_books(
        rt, ev, setup, books2[0], books2[1], s, fee_rate, paper, allow_requote=False
    )
    if not second.ok:
        return TakerSim(False, first.up_price, first.down_price, 0.0, 0.0, 0.0, False, "clob_rtt_miss")
    return second


def _confirm_favorite(rt: Runtime, ev: dict, setup, up_book: dict, dn_book: dict, s: dict, fee_rate: float, paper, *, allow_requote: bool = True) -> TakerSim:
    leg = str((setup.extra or {}).get("leg") or "up")
    asks = (up_book.get("asks") or []) if leg == "up" else (dn_book.get("asks") or [])
    bids = (up_book.get("bids") or []) if leg == "up" else (dn_book.get("bids") or [])
    other_asks = (dn_book.get("asks") or []) if leg == "up" else (up_book.get("asks") or [])
    min_px = setting_num(s, "favorite_min_price", 0.97)
    max_px = setting_num(s, "favorite_max_price", 0.98)
    limit = setup.up_price if leg == "up" else setup.down_price
    lock = favorite_lock_reason(
        asks=asks,
        bids=bids,
        other_asks=other_asks,
        min_px=min_px,
        max_px=max_px,
    )
    if lock:
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, lock)
    min_shares = max(float(s["min_shares"]), float(ev.get("min_size") or 5))
    fill = fak_one(
        asks=asks,
        shares=setup.shares,
        limit=limit,
        min_shares=min_shares,
        min_px=min_px,
        max_px=max_px,
        fee_rate=fee_rate,
    )
    if fill.ok:
        px = fill.up_price
        return TakerSim(
            True,
            px if leg == "up" else 0.0,
            px if leg == "down" else 0.0,
            fill.net,
            fill.cost,
            fill.fees,
            False,
            fill.reason,
            fill.shares,
        )
    if not allow_requote:
        return fill
    hunted = hunt(
        slug=ev["slug"],
        title=ev.get("title") or setup.title,
        condition_id=ev["condition_id"],
        up_token=ev["up_token"],
        down_token=ev["down_token"],
        up_asks=up_book.get("asks") or [],
        down_asks=dn_book.get("asks") or [],
        up_bids=up_book.get("bids") or [],
        down_bids=dn_book.get("bids") or [],
        max_usd=min(
            _trade_budget(s, paper),
            favorite_budget(float(s["max_usd_per_trade"]), rt.store.inventory_one(ev["condition_id"])),
        ),
        min_shares=min_shares,
        min_edge=float(s["min_edge"]),
        fee_rate=fee_rate,
        prefer_tail=bool(s["prefer_tail"]),
        tail_confirm=float(s["tail_confirm"]),
        maker_first=False,
        end=ev.get("end") or setup.end,
        maker_window_seconds=0.0,
        strategy_mode="favorite",
        favorite_min_price=min_px,
        favorite_max_price=max_px,
        favorite_window_seconds=favorite_window_of(s),
        favorite_maker=False,
        favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
    )
    if hunted is None or hunted.kind != "taker" or hunted.net <= 0:
        return fill
    new_px = float((hunted.extra or {}).get("favorite_px") or 0)
    # 0.98 FOK-kill then leftover 0.97 is the 99¢ steamroller, not a better fill.
    if new_px + 1e-12 < float(limit):
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "favorite_no_down_requote")
    if str((hunted.extra or {}).get("leg") or leg) != leg:
        return fill
    setup.extra["leg"] = (hunted.extra or {}).get("leg") or leg
    return TakerSim(
        True,
        hunted.up_price,
        hunted.down_price,
        hunted.net,
        hunted.cost,
        hunted.fees,
        False,
        "fok_requote",
        hunted.shares,
    )


def _confirm_twap(rt: Runtime, ev: dict, setup, up_book: dict, dn_book: dict, s: dict, fee_rate: float, paper, *, allow_requote: bool = True) -> TakerSim:
    """One-leg FAK in 45–55¢. First-cross: no leftover cheaper fill; flipping the leg is not."""
    params = default_params(s)
    leg = str((setup.extra or {}).get("leg") or "up")
    asks = (up_book.get("asks") or []) if leg == "up" else (dn_book.get("asks") or [])
    limit = setup.up_price if leg == "up" else setup.down_price
    first_px = _twap_first_px_of(rt, str(ev.get("slug") or setup.slug or "")) or float(limit)
    min_shares = max(float(s["min_shares"]), float(ev.get("min_size") or 5))
    fill = fak_one(
        asks=asks,
        shares=setup.shares,
        limit=limit,
        min_shares=min_shares,
        min_px=params.min_price,
        max_px=params.max_price,
        fee_rate=fee_rate,
    )
    if fill.ok:
        px = fill.up_price
        if params.no_cheaper and cheaper_than_first(px, first_px):
            return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "twap_no_cheaper")
        return TakerSim(
            True,
            px if leg == "up" else 0.0,
            px if leg == "down" else 0.0,
            fill.net,
            fill.cost,
            fill.fees,
            False,
            fill.reason,
            fill.shares,
        )
    if not allow_requote:
        return fill
    top = _top(asks, asks=True)
    if params.no_cheaper and cheaper_than_first(top, first_px):
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "twap_no_cheaper")
    snap = rt.chainlink.snapshot(
        str(ev.get("slug") or setup.slug),
        lookback=int(params.lookback),
        left=seconds_left(ev.get("end") or setup.end),
    )
    hunted = hunt(
        slug=ev["slug"],
        title=ev.get("title") or setup.title,
        condition_id=ev["condition_id"],
        up_token=ev["up_token"],
        down_token=ev["down_token"],
        up_asks=up_book.get("asks") or [],
        down_asks=dn_book.get("asks") or [],
        up_bids=up_book.get("bids") or [],
        down_bids=dn_book.get("bids") or [],
        max_usd=min(
            _trade_budget(s, paper),
            favorite_budget(float(s["max_usd_per_trade"]), rt.store.inventory_one(ev["condition_id"])),
        ),
        min_shares=min_shares,
        min_edge=float(s["min_edge"]),
        fee_rate=fee_rate,
        prefer_tail=bool(s["prefer_tail"]),
        tail_confirm=float(s["tail_confirm"]),
        maker_first=False,
        end=ev.get("end") or setup.end,
        maker_window_seconds=0.0,
        strategy_mode="twap",
        twap_snap=snap,
        twap_params=params,
        first_px=first_px,
    )
    if hunted is None or not is_twap_setup(hunted) or hunted.net <= 0:
        return fill
    if str((hunted.extra or {}).get("leg") or leg) != leg:
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "twap_no_flip")
    new_px = float((hunted.extra or {}).get("fill_px") or hunted.up_price or hunted.down_price)
    if params.no_cheaper and cheaper_than_first(new_px, first_px):
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "twap_no_cheaper")
    if new_px - 1e-12 > float(limit):
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "twap_no_up_requote")
    return TakerSim(
        True,
        hunted.up_price,
        hunted.down_price,
        hunted.net,
        hunted.cost,
        hunted.fees,
        False,
        "fok_requote",
        hunted.shares,
    )


async def _pair_books(rt: Runtime, ev: dict, *, max_age_ms: float) -> tuple[dict | None, dict | None, str]:
    cached = rt.books.pair(ev.get("up_token") or "", ev.get("down_token") or "", max_age_ms=max_age_ms)
    left = seconds_left(ev.get("end"))
    ws_empty = False
    if cached:
        ws_empty = not (cached["up"].get("asks") or []) or not (cached["down"].get("asks") or [])
    flicker = left is not None and 0 < left <= 180 and ws_empty
    slug = str(ev.get("slug") or ev.get("condition_id") or "")
    missing = cached is None
    now = time.time()
    http_due = http_book_due(missing=missing, flicker=flicker)
    if http_due and rt.data is not None and now - rt._http_at.get(slug, 0.0) >= 1.0:
        try:
            up_book, dn_book = await asyncio.gather(
                rt.data.book(ev["up_token"]),
                rt.data.book(ev["down_token"]),
            )
        except Exception as exc:
            rt.store.add_event("warn", f"book {ev.get('slug')}: {fmt_exc(exc)}")
            if cached:
                return cached["up"], cached["down"], "ws"
            return None, None, "error"
        rt._http_at[slug] = now
        now_ms = time.time() * 1000.0
        rt.books.put(ev["up_token"], up_book["asks"], up_book["bids"], ts_ms=now_ms, source="http")
        rt.books.put(ev["down_token"], dn_book["asks"], dn_book["bids"], ts_ms=now_ms, source="http")
        return up_book, dn_book, "http"
    if cached:
        return cached["up"], cached["down"], "ws"
    return None, None, "stale"


async def _prime_ws_books(rt: Runtime, events: list[dict], tokens: list[str]) -> int:
    """One HTTP snapshot for newly subscribed tokens. initial_dump is off."""
    wanted = {str(t) for t in tokens if t}
    if not wanted or rt.data is None:
        return 0
    now = time.time()
    n = 0
    for ev in events:
        up_t = str(ev.get("up_token") or "")
        dn_t = str(ev.get("down_token") or "")
        if up_t not in wanted and dn_t not in wanted:
            continue
        cached = rt.books.pair(up_t, dn_t, max_age_ms=60000.0)
        if cached and (cached["up"].get("asks") or []) and (cached["down"].get("asks") or []):
            continue
        slug = str(ev.get("slug") or ev.get("condition_id") or "")
        if now - rt._http_at.get(slug, 0.0) < 1.0:
            continue
        if not ev.get("up_token") or not ev.get("down_token"):
            continue
        try:
            up_book, dn_book = await asyncio.gather(
                rt.data.book(ev["up_token"]),
                rt.data.book(ev["down_token"]),
            )
        except Exception:
            continue
        rt._http_at[slug] = time.time()
        now_ms = time.time() * 1000.0
        rt.books.put(ev["up_token"], up_book["asks"], up_book["bids"], ts_ms=now_ms, source="http")
        rt.books.put(ev["down_token"], dn_book["asks"], dn_book["bids"], ts_ms=now_ms, source="http")
        n += 1
        if n >= 7:
            break
    return n


async def _resting_pair_books(rt: Runtime, row: dict, *, max_age_ms: float) -> tuple[dict | None, dict | None]:
    """Prefer WS books so a last-second dump can hit a 97¢ bid. HTTP is fallback."""
    cached = rt.books.pair(row.get("up_token") or "", row.get("down_token") or "", max_age_ms=max_age_ms)
    if cached:
        return cached["up"], cached["down"]
    if rt.data is None:
        return None, None
    try:
        return await asyncio.gather(
            rt.data.book(row["up_token"]),
            rt.data.book(row["down_token"]),
        )
    except Exception as exc:
        rt.store.add_event("warn", f"rest book {row['slug']}: {exc}"[:200])
        return None, None


async def _process_resting(rt: Runtime) -> int:
    """Fill paper maker legs only when the live book trades through the resting bid."""
    if rt.mode() != "paper":
        return 0
    s = rt.settings()
    fills = 0
    max_age = setting_num(s, "max_book_age_ms", 60000.0)
    for row in list(rt.store.resting_open()):
        payload = row.get("payload") or {}
        favorite = payload.get("strategy") == "favorite"
        if market_expired(row.get("end")):
            one_sided = bool(row["up_filled"]) != bool(row["down_filled"])
            rt.store.cancel_resting(row["id"], "expired")
            rt.store.add_event("info", f"paper rest expired {row['slug']}")
            if s.get("notify_signals"):
                leftover = "；未配對倉等結算" if one_sided else ""
                await rt.notify(f"⌛ 紙盤掛單到期撤單 {row['slug']}{leftover}")
            continue
        up_book, dn_book = await _resting_pair_books(rt, row, max_age_ms=max_age)
        if up_book is None or dn_book is None:
            continue
        if favorite:
            leg = str(payload.get("leg") or ("up" if float(row["up_price"]) >= float(row["down_price"]) else "down"))
            book = up_book if leg == "up" else dn_book
            px = float(row["up_price"] if leg == "up" else row["down_price"])
            already = bool(row["up_filled"] if leg == "up" else row["down_filled"])
            if not already and asks_cross_bid(book.get("asks") or [], px, float(row["shares"])):
                row = rt.store.fill_resting_leg(row["id"], leg)
                rt.store.complete_resting(row["id"], "favorite_hit")
                fills += 1
                paper = rt.store.paper_state()
                rt.store.add_trade(
                    slug=row["slug"],
                    kind="maker",
                    shares=row["shares"],
                    up_price=row["up_price"],
                    down_price=row["down_price"],
                    net=0.0,
                    mode="paper",
                    status="paper_filled",
                    payload={"detail": f"favorite bid hit {leg} @{px}", "resting_id": row["id"], "strategy": "favorite"},
                )
                if s.get("notify_signals"):
                    await rt.notify(
                        f"📌大熱掛單碰到（未結算）\n{row.get('title') or row['slug']}\n"
                        f"{leg} @{px} × {format_share_qty(row['shares'])} · 權益 ${paper['equity']:.2f}",
                        important=True,
                    )
            continue
        filled_now = []
        if not row["up_filled"] and asks_cross_bid(up_book["asks"], float(row["up_price"]), float(row["shares"])):
            row = rt.store.fill_resting_leg(row["id"], "up")
            filled_now.append("Up")
        if not row["down_filled"] and asks_cross_bid(dn_book["asks"], float(row["down_price"]), float(row["shares"])):
            row = rt.store.fill_resting_leg(row["id"], "down")
            filled_now.append("Down")
        if row["status"] == "filled" and row["up_filled"] and row["down_filled"]:
            fills += 1
            if s.get("auto_merge"):
                merged = rt.store.merge_inventory(row["condition_id"], float(row["shares"]))
                take = float(merged["merged"] or 0)
                if take > 0:
                    net_part = float(row.get("net") or 0) * (take / float(row["shares"]))
                    rt.store.paper_apply_merge(take, net_part)
                    await rt.broker().merge(row["condition_id"], take)
            paper = rt.store.paper_state()
            rt.store.add_trade(
                slug=row["slug"],
                kind="maker",
                shares=row["shares"],
                up_price=row["up_price"],
                down_price=row["down_price"],
                net=float(row.get("net") or 0),
                mode="paper",
                status="paper_filled",
                payload={"detail": "maker both legs filled after trade-through", "resting_id": row["id"]},
            )
            if s.get("notify_signals"):
                await rt.notify(
                    f"🧪紙盤 maker 兩邊碰到先成交\n{row.get('title') or row['slug']}\n"
                    f"{row['up_price']}+{row['down_price']} × {format_share_qty(row['shares'])} 淨利 ${float(row.get('net') or 0):.2f}\n"
                    f"現金 ${paper['cash']:.2f} · 權益 ${paper['equity']:.2f} · 累計 {format_signed_usd(paper['total_pnl'])}",
                    important=True,
                )
            continue
        one_sided = bool(row["up_filled"]) != bool(row["down_filled"])
        if not one_sided:
            continue
        if filled_now:
            rt.store.add_trade(
                slug=row["slug"],
                kind="maker",
                shares=row["shares"],
                up_price=row["up_price"],
                down_price=row["down_price"],
                net=0.0,
                mode="paper",
                status="paper_leg_fill",
                payload={"detail": f"legs {filled_now}", "resting_id": row["id"]},
            )
        did = await _rescue_resting_row(rt, row, up_book, dn_book)
        fills += did
    return fills


async def _rescue_resting_row(rt: Runtime, row: dict, up_book: dict, dn_book: dict) -> int:
    s = rt.settings()
    fee_rate = float(s.get("fee_rate") or 0.07)
    shares = float(row["shares"])
    if row["up_filled"] and not row["down_filled"]:
        filled_px = float(row["up_price"])
        plan = plan_rescue(
            filled_px=filled_px,
            shares=shares,
            other_asks=dn_book["asks"],
            filled_bids=up_book["bids"],
            fee_rate=fee_rate,
        )
        side = "down"
    elif row["down_filled"] and not row["up_filled"]:
        filled_px = float(row["down_price"])
        plan = plan_rescue(
            filled_px=filled_px,
            shares=shares,
            other_asks=up_book["asks"],
            filled_bids=dn_book["bids"],
            fee_rate=fee_rate,
        )
        side = "up"
    else:
        return 0
    if plan.action == "hold":
        return 0
    if plan.action == "hedge":
        cash = float(rt.store.paper_state()["cash"])
        leftover = float(row.get("reserved") or 0)
        if cash + leftover + 1e-9 < plan.cash_out:
            # cannot lift the other ask after releasing the rest; dump instead if possible
            if row["up_filled"] and not row["down_filled"]:
                plan = plan_rescue(
                    filled_px=float(row["up_price"]),
                    shares=shares,
                    other_asks=[],
                    filled_bids=up_book["bids"],
                    fee_rate=fee_rate,
                )
            else:
                plan = plan_rescue(
                    filled_px=float(row["down_price"]),
                    shares=shares,
                    other_asks=[],
                    filled_bids=dn_book["bids"],
                    fee_rate=fee_rate,
                )
            if plan.action == "hold":
                return 0
    rt.store.cancel_resting(row["id"], f"rescue_{plan.action}")
    return await _apply_rescue(rt, row, side, plan)


async def _apply_rescue(rt: Runtime, row: dict, missing_side: str, plan) -> int:
    s = rt.settings()
    shares = float(row["shares"])
    slug = row["slug"]
    cid = row["condition_id"]
    if plan.action == "hedge":
        try:
            rt.store.paper_apply_buy(plan.cash_out)
        except ValueError:
            rt.store.add_event("warn", f"rescue hedge cash {slug}")
            return 0
        up_add = shares if missing_side == "up" else 0.0
        dn_add = shares if missing_side == "down" else 0.0
        rt.store.add_inventory(cid, slug, up_add, dn_add)
        merged = rt.store.merge_inventory(cid, shares)
        take = float(merged["merged"] or 0)
        if take > 0:
            rt.store.paper_apply_merge(take, plan.pnl * (take / shares) if shares else 0.0)
            await rt.broker().merge(cid, take)
        rt.store.add_trade(
            slug=slug,
            kind="maker",
            shares=shares,
            up_price=row["up_price"],
            down_price=row["down_price"],
            net=plan.pnl,
            mode="paper",
            status="paper_hedged",
            payload={"detail": f"hedge {missing_side} @{plan.price}", "fees": plan.fees},
        )
        if s.get("notify_signals"):
            await rt.notify(
                f"🛟 單邊對沖 {slug}\n買 {missing_side} @{plan.price} 後 merge · 淨 ${plan.pnl:.2f}",
                important=True,
            )
        return 1
    if plan.action == "dump":
        paper_mode = rt.mode() == "paper"
        if not paper_mode and rt.clob_halted():
            return 0
        token = str(row["up_token"] if missing_side == "down" else row["down_token"])
        floor = float(plan.floor_px or plan.price or 0.01)
        sell = await rt.broker().execute_sell(token, shares, floor)
        if not sell.ok:
            payload = sell.payload if isinstance(sell.payload, dict) else {}
            if not paper_mode:
                await _maybe_halt_clob(rt, sell.detail, payload)
            detail = str(sell.detail or "")
            if sell_size_dust(detail):
                if slug not in rt._dump_fail_logged:
                    rt._dump_fail_logged.add(slug)
                    rt.store.add_event("warn", f"dump fail {slug}: {detail}"[:220])
            else:
                rt.store.add_event("warn", f"dump fail {slug}: {detail}"[:220])
            return 0
        rt._dump_fail_logged.discard(slug)
        if not paper_mode:
            rt.clear_clob_halt()
        sold = float((sell.payload or {}).get("shares") or shares)
        if sold <= 0.01:
            return 0
        frac = min(1.0, sold / shares) if shares else 1.0
        cash_out = (sell.payload or {}).get("proceeds")
        try:
            cash_out_f = float(cash_out) if cash_out is not None else round(float(plan.cash_out) * frac, 6)
        except (TypeError, ValueError):
            cash_out_f = round(float(plan.cash_out) * frac, 6)
        fee_rate = float(s.get("fee_rate") or 0.07)
        fill_px = round(cash_out_f / sold, 4) if sold > 0 and cash_out is not None else round(float(plan.price or 0), 4)
        sell_fee = float(plan.fees or 0)
        if not paper_mode and cash_out is not None and sold > 0:
            sell_fee = taker_fee(sold, cash_out_f / sold, fee_rate)
            cash_out_f = round(max(0.0, cash_out_f - sell_fee), 6)
        up_take = sold if missing_side == "down" else 0.0
        dn_take = sold if missing_side == "up" else 0.0
        before = rt.store.inventory_one(cid)
        if missing_side == "down":
            held_leg = float((before or {}).get("up") or 0)
            if 0 < held_leg - sold <= 0.011:
                up_take = held_leg
        else:
            held_leg = float((before or {}).get("down") or 0)
            if 0 < held_leg - sold <= 0.011:
                dn_take = held_leg
        cost_before = float((before or {}).get("cost") or 0)
        rt.store.take_inventory(cid, up=up_take, down=dn_take)
        after = rt.store.inventory_one(cid)
        cost_taken = round(cost_before - float((after or {}).get("cost") or 0), 6)
        pnl = round(cash_out_f - cost_taken, 6)
        if paper_mode:
            rt.store.paper_apply_credit(cash_out_f, realized=pnl)
        kind = str(row.get("kind") or "maker")
        dump_px = fill_px
        rt.store.add_trade(
            slug=slug,
            kind=kind,
            shares=sold,
            up_price=dump_px if missing_side == "down" else 0.0,
            down_price=dump_px if missing_side == "up" else 0.0,
            net=pnl,
            mode=rt.mode(),
            status="paper_dumped" if paper_mode else "dumped",
            payload={
                **(sell.payload or {}),
                "detail": f"dump @{dump_px} floor {floor}",
                "proceeds": cash_out_f,
                "fees": sell_fee,
                "floor_px": floor,
                "cost_taken": cost_taken,
            },
        )
        if s.get("notify_signals"):
            flag = "🧪紙盤" if paper_mode else "🔴實盤"
            await rt.notify(
                f"🧯 {flag} 單邊出貨 {slug}\n@{dump_px} 回籠 ${cash_out_f:.2f} · 淨 ${pnl:.2f}",
                important=True,
            )
        return 1
    return 0


async def _rescue_naked(rt: Runtime, events: list[dict]) -> int:
    if rt.mode() != "paper" or rt.data is None:
        return 0
    live = {ev["condition_id"]: ev for ev in events if ev.get("condition_id")}
    n = 0
    for inv in list(rt.store.inventory()):
        up, down = float(inv["up"] or 0), float(inv["down"] or 0)
        if min(up, down) > 0.01:
            continue
        if up < 0.01 and down < 0.01:
            continue
        if is_directional_inventory(inv.get("kind")):
            continue
        cid = inv["condition_id"]
        ev = live.get(cid)
        if ev is None:
            continue
        rest = rt.store.latest_resting(cid)
        if rest and rest.get("status") == "open":
            continue
        try:
            up_book, dn_book = await asyncio.gather(rt.data.book(ev["up_token"]), rt.data.book(ev["down_token"]))
        except Exception as exc:
            rt.store.add_event("warn", f"naked book {inv.get('slug')}: {exc}"[:200])
            continue
        fee_rate = float(rt.settings().get("fee_rate") or 0.07)
        if up > down:
            filled_px = float((rest or {}).get("up_price") or 0) or 0.5
            shares = up
            plan = plan_rescue(filled_px=filled_px, shares=shares, other_asks=dn_book["asks"], filled_bids=up_book["bids"], fee_rate=fee_rate)
            missing = "down"
        else:
            filled_px = float((rest or {}).get("down_price") or 0) or 0.5
            shares = down
            plan = plan_rescue(filled_px=filled_px, shares=shares, other_asks=up_book["asks"], filled_bids=dn_book["bids"], fee_rate=fee_rate)
            missing = "up"
        row = {
            "id": (rest or {}).get("id"),
            "slug": inv.get("slug") or ev["slug"],
            "condition_id": cid,
            "shares": shares,
            "up_price": (rest or {}).get("up_price") or filled_px,
            "down_price": (rest or {}).get("down_price") or filled_px,
            "up_token": ev["up_token"],
            "down_token": ev["down_token"],
        }
        if plan.action == "hold":
            continue
        n += await _apply_rescue(rt, row, missing, plan)
    return n


async def _redeem_resolved(rt: Runtime) -> int:
    """Credit paper cash / on-chain redeemPositions once a market has resolved.

    Runs while paused, killed, or circuit-tripped so leftover favorite inventory
    is not stuck. Live tokens stay in the proxy until redeemPositions.
    """
    if rt.data is None:
        return 0
    s = rt.settings()
    if s.get("auto_redeem") is False:
        return 0
    paper_mode = rt.mode() == "paper"
    jobs: list[dict] = []
    seen: set[str] = set()
    for inv in list(rt.store.inventory()):
        up, down = float(inv["up"] or 0), float(inv["down"] or 0)
        if up < 0.01 and down < 0.01:
            continue
        cid = str(inv.get("condition_id") or "")
        slug = str(inv.get("slug") or "")
        if not cid:
            continue
        if paper_mode and is_live_inventory_kind(inv.get("kind")):
            continue
        try:
            ev = await rt.data.event_by_slug(slug)
        except Exception as exc:
            rt.store.add_event("warn", f"redeem fetch {slug}: {fmt_exc(exc)}"[:200])
            continue
        prices = is_redeemable_market(ev)
        if prices is None:
            continue
        jobs.append(
            {
                "condition_id": cid,
                "slug": slug,
                "up": up,
                "down": down,
                "cost": float(inv.get("cost") or 0),
                "kind": str(inv.get("kind") or ""),
                "prices": prices,
                "tracked": True,
            }
        )
        seen.add(cid)
    if not paper_mode:
        try:
            extra = await rt.broker().list_redeemable()
        except Exception as exc:
            rt.store.add_event("warn", f"redeem list {fmt_exc(exc)}"[:200])
            extra = []
        for row in extra:
            cid = str((row or {}).get("condition_id") or "")
            slug = str((row or {}).get("slug") or "")
            if not cid or cid in seen:
                continue
            if not parse_window(slug):
                continue
            jobs.append(
                {
                    "condition_id": cid,
                    "slug": slug,
                    "up": 0.0,
                    "down": 0.0,
                    "cost": 0.0,
                    "kind": "",
                    "prices": (0.0, 0.0),
                    "tracked": False,
                    "size": float((row or {}).get("size") or 0),
                }
            )
            seen.add(cid)
    n = 0
    now = time.time()
    for job in jobs:
        if n >= 8:
            break
        cid = job["condition_id"]
        if float(rt.cooldown.get(f"redeem:{cid}") or 0) > now:
            continue
        for rest in list(rt.store.resting_open()):
            if rest.get("condition_id") == cid:
                try:
                    rt.store.cancel_resting(int(rest["id"]), "redeem")
                except Exception:
                    pass
        paper_books = bool(job.get("tracked")) and not is_live_inventory_kind(job.get("kind"))
        waiting = cid in rt._redeem_wait_logged
        if paper_books and not paper_mode:
            result = FillResult(True, "paper_settled", "paper", "紙盤 redeem 入帳", {"condition_id": cid})
        elif waiting:
            held = None
            size_fn = getattr(rt.broker(), "condition_token_size", None)
            if callable(size_fn):
                try:
                    held = await size_fn(cid)
                except Exception:
                    held = None
            if held is not None and held < 0.01:
                result = FillResult(
                    True,
                    "redeemed",
                    "live",
                    "already empty",
                    {"condition_id": cid, "already": True},
                )
            else:
                rt.cooldown[f"redeem:{cid}"] = now + 45.0
                continue
        else:
            result = await rt.broker().redeem(cid)
        if not result.ok:
            detail = str(result.detail or "")
            waiting_now = result.status == "redeem_wait" or redeem_not_ready(detail)
            wait = 45.0 if waiting_now else 20.0
            rt.cooldown[f"redeem:{cid}"] = now + wait
            if waiting_now:
                if cid not in rt._redeem_wait_logged:
                    rt._redeem_wait_logged.add(cid)
                    rt.store.add_event(
                        "info",
                        f"redeem 等結算 {job['slug'] or cid}（CLOB 已除牌，等鏈上 auto-redeem）",
                    )
            else:
                rt.store.add_event("warn", f"redeem fail {job['slug'] or cid}: {result.detail}"[:220])
            continue
        rt.cooldown.pop(f"redeem:{cid}", None)
        rt._redeem_wait_logged.discard(cid)
        up, down = float(job["up"]), float(job["down"])
        cost = float(job["cost"])
        fav = is_favorite_inventory(job["kind"])
        twap = str(job.get("kind") or "").startswith("twap")
        directional = fav or twap
        up_p, dn_p = job["prices"]
        payout = round(up * up_p + down * dn_p, 6) if job["tracked"] else 0.0
        settle_net = round(payout - cost, 6) if directional else payout
        if job["tracked"]:
            rt.store.take_inventory(cid, up=up, down=down)
            if paper_books:
                rt.store.paper_apply_credit(
                    payout, realized=settle_net if directional else 0.0
                )
        rt.store.add_trade(
            slug=job["slug"],
            kind="settle",
            shares=max(up, down) if job["tracked"] else float(job.get("size") or 0),
            up_price=up_p,
            down_price=dn_p,
            net=settle_net,
            mode="paper" if paper_books else rt.mode(),
            status="paper_settled" if paper_books else "redeemed",
            payload={
                "up": up,
                "down": down,
                "payout": payout,
                "cost": cost,
                "strategy": "twap" if twap else ("favorite" if fav else "pair"),
                "redeem": True,
                "already": bool((result.payload or {}).get("already")),
            },
        )
        rt.store.add_event(
            "info",
            f"redeem {job['slug'] or cid} up={up:.2f}@{up_p} down={down:.2f}@{dn_p} payout=${payout:.2f}",
        )
        n += 1
        leftover_live = paper_books and not paper_mode
        if leftover_live:
            continue
        if s.get("notify_signals"):
            extra = f" · 淨 ${settle_net:.2f}" if directional else ""
            flag = "🧪紙盤" if paper_books else "🔴實盤"
            await rt.notify(
                f"♻️ {flag} redeem 取回 {job['slug'] or cid}\n"
                f"Up {format_share_qty(up)} × {up_p} + Down {format_share_qty(down)} × {dn_p} = ${payout:.2f}{extra}",
                important=True,
            )
    return n


async def _settle_inventory(rt: Runtime) -> int:
    """Back-compat alias: favorite hold-to-settle is now auto-redeem."""
    return await _redeem_resolved(rt)


async def _reset_http(rt: Runtime) -> None:
    client = rt.http
    rt.http = None
    rt.data = None
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        pass


def _trade_budget(s: dict, paper: dict | None) -> float:
    cap = float(s["max_usd_per_trade"])
    if not paper:
        return cap
    return max(0.0, min(cap, float(paper["cash"]) - 0.25))
