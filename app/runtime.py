from __future__ import annotations

import asyncio
import inspect
import json
import time
from typing import Any

import httpx

from app.broker import FillResult, LiveBroker, PaperBroker
from app.config import Env, clamp_paper_cash, live_keys_ready, setting_num
from app.hunter import book_quote, hunt, is_favorite_setup, parse_favorite_dir, summarize_quotes
from app.markets import MarketData
from app.paper_sim import TakerSim, asks_cross_bid, confirm_pair, fak_one, market_expired, seconds_left
from app.rescue import is_redeemable_market, plan_rescue
from app.risk import approve
from app.store import Store
from app.universe import DEFAULT_ASSETS, DEFAULT_TAGS
from app.ws_books import WS_MARKET, BookCache


def fmt_exc(exc: BaseException) -> str:
    text = str(exc).strip() or repr(exc)
    return f"{type(exc).__name__}: {text}"[:300]


def http_book_due(*, missing: bool, flicker: bool) -> bool:
    """HTTP only when WS has no pair, or last-3-min books are one-sided empty.

    Polling every 1s across 24 near-expiry markets stalled the CLOB socket
    (1013 slow consumer) and missed 97–99¢ asks.
    """
    return bool(missing or flicker)


def favorite_budget(max_usd: float, inv: dict | None) -> float:
    """Room left under max_usd_per_trade for an existing favorite position."""
    cap = float(max_usd)
    if not inv or str(inv.get("kind") or "") != "favorite":
        return cap
    if float(inv.get("up") or 0) <= 0.01 and float(inv.get("down") or 0) <= 0.01:
        return cap
    return max(0.0, round(cap - float(inv.get("cost") or 0), 6))


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
        self.books = BookCache()
        self.universe: list[dict] = []
        self.ws_status = "off"
        self._hunt_event = asyncio.Event()
        self._lock = asyncio.Lock()
        self._http_at: dict[str, float] = {}

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
            self._broker = LiveBroker(self.env.private_key) if mode == "live" else PaperBroker()
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
        pnl = self.store.paper_state()["today_pnl"] if self.mode() == "paper" else self.store.today_pnl()
        return pnl <= -abs(limit)

    def snapshot(self) -> dict[str, Any]:
        s = self.settings()
        st = self.store.stats()
        paper = self.store.paper_state()
        noisy = {"paper_leg_fill", "paper_resting", "resting"}
        trades = [t for t in self.store.recent_trades(40) if t.get("status") not in noisy][:15]
        scans = [x for x in self.store.recent_scans(40) if float(x.get("ts") or 0) >= self.started_at - 2][:12]
        return {
            "mode": self.mode(),
            "keys_ready": live_keys_ready(self.env),
            "force_paper": self.env.force_paper,
            "uptime_s": int(time.time() - self.started_at),
            "circuit": self.circuit_tripped(),
            "geo": self.geo,
            "settings": s,
            "stats": st,
            "paper": paper,
            "last_loop": self.last_loop,
            "ws_status": self.ws_status,
            "inventory": self.store.inventory_open()[:20],
            "resting": self.store.resting_open()[:20],
            "trades": trades,
            "scans": scans,
            "events": [e for e in self.store.recent_events(40) if float(e.get("ts") or 0) >= self.started_at - 30][:15],
        }


async def engine_loop(rt: Runtime) -> None:
    await _ensure_http(rt)
    await asyncio.gather(_universe_loop(rt), _ws_loop(rt), _hunt_loop(rt))


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
        if n:
            await rt.notify(f"🛑 已撤 {n} 張紙盤掛單")
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
    tokens: list[str] = []
    for ev in events:
        tokens.append(str(ev.get("up_token") or ""))
        tokens.append(str(ev.get("down_token") or ""))
    rt.universe = events
    rt.books.set_wanted(tokens)
    rescued = await _rescue_naked(rt, events)
    if rt.circuit_tripped():
        n = rt.store.cancel_all_resting("circuit")
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
            rt.store.add_event("warn", f"circuit breaker pnl={pnl:.2f} limit={limit:.2f} cancelled_resting={n}")
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
    backoff = 1.0
    while True:
        if not rt.settings().get("engine_running") or rt.settings().get("killed"):
            rt.ws_status = "paused"
            rt.books.connected = False
            await asyncio.sleep(1.0)
            continue
        wanted = list(rt.books.wanted)
        if not wanted:
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
            return json.dumps(
                {"assets_ids": ids, "type": "market", "custom_feature_enabled": True, "initial_dump": True}
            )

        try:
            async with websockets.connect(WS_MARKET, **kw) as ws:
                rt.ws_status = "connected"
                rt.books.connected = True
                backoff = 1.0
                await ws.send(_sub(wanted))
                now = time.time()
                if now - rt._last_ws_info_ts > 60:
                    rt.store.add_event("info", f"ws connected {len(wanted)} tokens")
                    rt._last_ws_info_ts = now
                ping = asyncio.create_task(_ws_ping(ws))
                try:
                    async for raw in ws:
                        now_wanted = list(rt.books.wanted)
                        if now_wanted != wanted:
                            if not now_wanted:
                                break
                            wanted = now_wanted
                            await ws.send(_sub(wanted))
                            continue
                        changed = rt.books.apply_message(raw)
                        if changed:
                            rt._hunt_event.set()
                finally:
                    ping.cancel()
        except Exception as exc:
            rt.ws_status = "down"
            rt.books.connected = False
            detail = fmt_exc(exc)
            now = time.time()
            if now - rt._last_ws_error_ts > 120:
                rt._last_ws_error_ts = now
                rt.store.add_event("warn", f"ws {detail}")
            await asyncio.sleep(backoff)
            backoff = min(15.0, backoff * 2)
        else:
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
    for ev in events:
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
        inv = rt.store.inventory_one(ev["condition_id"])
        max_usd = min(_trade_budget(s, paper), favorite_budget(trade_cap, inv))
        if max_usd + 1e-9 < float(s["min_shares"]) * 0.90:
            continue
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
                prefer_tail=bool(s["prefer_tail"]),
                tail_confirm=float(s["tail_confirm"]),
                maker_first=bool(s["maker_first"]),
                end=ev.get("end"),
                maker_min_leg=setting_num(s, "maker_min_leg", 0.22),
                maker_max_skew=setting_num(s, "maker_max_skew", 0.10),
                maker_window_seconds=window,
                maker_min_edge=float(s["maker_min_edge"]) if s.get("maker_min_edge") is not None else None,
                strategy_mode=str(s.get("strategy_mode") or "auto"),
                favorite_min_price=setting_num(s, "favorite_min_price", 0.95),
                favorite_max_price=setting_num(s, "favorite_max_price", 0.99),
                favorite_window_seconds=setting_num(s, "favorite_window_seconds", 30.0),
                favorite_maker=bool(s.get("favorite_maker")),
                favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
            )
        except Exception as exc:
            rt.store.add_event("warn", f"hunt {ev.get('slug')}: {fmt_exc(exc)}")
            continue
        if not setup:
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
        decision = approve(
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
            favorite_min_price=setting_num(s, "favorite_min_price", 0.95),
            favorite_max_price=setting_num(s, "favorite_max_price", 0.99),
            favorite_window_seconds=setting_num(s, "favorite_window_seconds", 30.0),
            favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
            max_usd_per_trade=trade_cap,
            favorite_spent=float(inv.get("cost") or 0),
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
                        f"snapshot {setup.up_price}+{setup.down_price} × {setup.shares:.1f} 淨 ${setup.net:.2f}\n"
                        f"確認後：{confirm.reason}",
                    )
                continue
            if confirm.shares > 0:
                setup.shares = round(float(confirm.shares), 4)
                setup.fillable = setup.shares
            setup.up_price = confirm.up_price
            setup.down_price = confirm.down_price
            setup.net = confirm.net
            setup.fees = confirm.fees
            setup.gross = round(1.0 - (confirm.up_price + confirm.down_price), 4)
            setup.extra["fok"] = confirm.reason
            payload["up"] = confirm.up_price
            payload["down"] = confirm.down_price
            payload["net"] = confirm.net
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
                favorite_min_price=setting_num(s, "favorite_min_price", 0.95),
                favorite_max_price=setting_num(s, "favorite_max_price", 0.99),
                favorite_window_seconds=setting_num(s, "favorite_window_seconds", 30.0),
                favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
                max_usd_per_trade=trade_cap,
                favorite_spent=float(inv.get("cost") or 0),
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
        rt.store.add_trade(
            slug=setup.slug,
            kind=setup.kind,
            shares=setup.shares,
            up_price=setup.up_price,
            down_price=setup.down_price,
            net=(result.payload or {}).get("net", setup.net) if result.ok and result.status in {"filled", "paper_filled"} and not is_favorite_setup(setup) else 0.0,
            mode=result.mode,
            status=result.status,
            payload={"detail": result.detail, **(result.payload or {})},
        )
        if result.ok and result.status in {"filled", "paper_filled"}:
            fills += 1
            if setup.kind == "taker" and bool(s.get("taker_fok", True)):
                fok_fills += 1
            fill_cost = float((result.payload or {}).get("cost", setup.cost))
            fill_net = float((result.payload or {}).get("net", setup.net))
            fill_up = float((result.payload or {}).get("up_price", setup.up_price))
            fill_down = float((result.payload or {}).get("down_price", setup.down_price))
            if paper_mode:
                try:
                    rt.store.paper_apply_buy(fill_cost)
                except ValueError:
                    rt.store.add_event("warn", f"paper cash race {setup.slug}")
                    continue
            if is_favorite_setup(setup):
                leg = str((setup.extra or {}).get("leg") or "up")
                up_sh = setup.shares if leg == "up" else 0.0
                dn_sh = setup.shares if leg == "down" else 0.0
                rt.store.add_inventory(
                    setup.condition_id, setup.slug, up_sh, dn_sh, kind="favorite", cost=fill_cost
                )
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
                    sign = "+" if paper["total_pnl"] >= 0 else ""
                    book = (
                        f"\n成本 ${fill_cost:.2f} · 現金 ${paper['cash']:.2f} · 權益 ${paper['equity']:.2f}"
                        f"\n累計 PnL {sign}${paper['total_pnl']:.2f} · 今日 ${paper['today_pnl']:.2f}"
                    )
                await rt.notify(
                    f"{flag} 成交 {'大熱' if is_favorite_setup(setup) else setup.kind}\n{setup.title}\n"
                    f"Up {fill_up} + Down {fill_down} × {setup.shares:.1f}\n"
                    f"{'未結算期望' if is_favorite_setup(setup) else '淨利'} ${fill_net:.2f}{book}",
                    important=True,
                )
        elif result.ok and result.status in {"paper_resting", "resting"}:
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
                    f"📌 紙盤掛單 {setup.title}\n{setup.up_price}+{setup.down_price} × {setup.shares:.1f}"
                    f"\n未碰到盤口唔入帳{lock}"
                )
        else:
            await rt.notify(f"❌ 下單失敗：{result.detail}", important=True)
    tape = summarize_quotes(quotes)
    tape["book_errors"] = book_errors
    tape["stale_pairs"] = stale_pairs
    tape["ws_pairs"] = ws_pairs
    tape["http_pairs"] = http_pairs
    tape["empty_ask_legs"] = empty_asks
    tape["ws_status"] = rt.ws_status
    tape["slugs"] = [ev.get("slug") for ev in events[:12] if ev.get("slug")]
    tape["tags"] = list(s.get("tags") or [s.get("tag") or "15M"])
    tape["taker_fok"] = bool(s.get("taker_fok", True))
    tape["snapshot_signals"] = snapshot_signals
    tape["fok_kills"] = fok_kills
    tape["fok_fills"] = fok_fills
    tape["strategy_mode"] = str(s.get("strategy_mode") or "auto")
    tape["favorite_min"] = setting_num(s, "favorite_min_price", 0.95)
    tape["favorite_max"] = setting_num(s, "favorite_max_price", 0.99)
    tape["favorite_window"] = setting_num(s, "favorite_window_seconds", 30.0)
    tape["favorite_dir"] = parse_favorite_dir(s.get("favorite_dir"))
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


async def _fok_confirm(rt: Runtime, ev: dict, setup) -> TakerSim:
    """Wait the official 250ms taker delay, then FAK leftover size or requote."""
    s = rt.settings()
    delay_ms = setting_num(s, "fok_delay_ms", 250.0)
    if delay_ms > 0:
        await asyncio.sleep(min(2.0, delay_ms / 1000.0))
    if rt.data is None:
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "fok_no_http")
    try:
        up_book, dn_book = await asyncio.gather(
            rt.data.book(ev["up_token"]),
            rt.data.book(ev["down_token"]),
        )
    except Exception as exc:
        rt.store.add_event("warn", f"fok book {ev.get('slug')}: {fmt_exc(exc)}")
        return TakerSim(False, setup.up_price, setup.down_price, 0.0, 0.0, 0.0, False, "fok_http")
    fee_rate = float(ev.get("fee_rate") or s.get("fee_rate") or 0.07)
    paper = rt.store.paper_state() if rt.mode() == "paper" else None
    if is_favorite_setup(setup):
        return _confirm_favorite(rt, ev, setup, up_book, dn_book, s, fee_rate, paper)
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
    )


def _confirm_favorite(rt: Runtime, ev: dict, setup, up_book: dict, dn_book: dict, s: dict, fee_rate: float, paper) -> TakerSim:
    leg = str((setup.extra or {}).get("leg") or "up")
    asks = (up_book.get("asks") or []) if leg == "up" else (dn_book.get("asks") or [])
    min_px = setting_num(s, "favorite_min_price", 0.95)
    max_px = setting_num(s, "favorite_max_price", 0.99)
    limit = setup.up_price if leg == "up" else setup.down_price
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
    requote = hunt(
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
        favorite_window_seconds=setting_num(s, "favorite_window_seconds", 30.0),
        favorite_maker=False,
        favorite_dir=parse_favorite_dir(s.get("favorite_dir")),
    )
    if requote is None or requote.kind != "taker" or requote.net <= 0:
        return fill
    setup.extra["leg"] = (requote.extra or {}).get("leg") or leg
    return TakerSim(
        True,
        requote.up_price,
        requote.down_price,
        requote.net,
        requote.cost,
        requote.fees,
        False,
        "fok_requote",
        requote.shares,
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
                        f"{leg} @{px} × {row['shares']:.1f} · 權益 ${paper['equity']:.2f}",
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
                sign = "+" if paper["total_pnl"] >= 0 else ""
                await rt.notify(
                    f"🧪紙盤 maker 兩邊碰到先成交\n{row.get('title') or row['slug']}\n"
                    f"{row['up_price']}+{row['down_price']} × {row['shares']:.1f} 淨利 ${float(row.get('net') or 0):.2f}\n"
                    f"現金 ${paper['cash']:.2f} · 權益 ${paper['equity']:.2f} · 累計 {sign}${paper['total_pnl']:.2f}",
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
        up_take = shares if missing_side == "down" else 0.0
        dn_take = shares if missing_side == "up" else 0.0
        rt.store.take_inventory(cid, up=up_take, down=dn_take)
        rt.store.paper_apply_credit(plan.cash_out)
        rt.store.add_trade(
            slug=slug,
            kind="maker",
            shares=shares,
            up_price=row["up_price"],
            down_price=row["down_price"],
            net=plan.pnl,
            mode="paper",
            status="paper_dumped",
            payload={"detail": f"dump @{plan.price}", "proceeds": plan.cash_out, "fees": plan.fees},
        )
        if s.get("notify_signals"):
            await rt.notify(
                f"🧯 單邊出貨 {slug}\n@{plan.price} 回籠 ${plan.cash_out:.2f} · 淨 ${plan.pnl:.2f}",
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
        if str(inv.get("kind") or "") == "favorite":
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
            if not cid or cid in seen:
                continue
            jobs.append(
                {
                    "condition_id": cid,
                    "slug": str((row or {}).get("slug") or ""),
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
        result = await rt.broker().redeem(cid)
        if not result.ok:
            rt.cooldown[f"redeem:{cid}"] = now + 20.0
            rt.store.add_event("warn", f"redeem fail {job['slug'] or cid}: {result.detail}"[:220])
            continue
        rt.cooldown.pop(f"redeem:{cid}", None)
        up, down = float(job["up"]), float(job["down"])
        cost = float(job["cost"])
        fav = str(job["kind"] or "") == "favorite"
        up_p, dn_p = job["prices"]
        payout = round(up * up_p + down * dn_p, 6) if job["tracked"] else 0.0
        if job["tracked"]:
            rt.store.take_inventory(cid, up=up, down=down)
            if paper_mode and payout > 0:
                rt.store.paper_apply_credit(payout)
        settle_net = round(payout - cost, 6) if fav else payout
        rt.store.add_trade(
            slug=job["slug"],
            kind="settle",
            shares=max(up, down) if job["tracked"] else float(job.get("size") or 0),
            up_price=up_p,
            down_price=dn_p,
            net=settle_net if paper_mode else 0.0,
            mode=rt.mode(),
            status="paper_settled" if paper_mode else "redeemed",
            payload={
                "up": up,
                "down": down,
                "payout": payout,
                "cost": cost,
                "strategy": "favorite" if fav else "pair",
                "redeem": True,
                "already": bool((result.payload or {}).get("already")),
            },
        )
        rt.store.add_event(
            "info",
            f"redeem {job['slug'] or cid} up={up:.1f}@{up_p} down={down:.1f}@{dn_p} payout=${payout:.2f}",
        )
        n += 1
        if s.get("notify_signals"):
            extra = f" · 淨 ${settle_net:.2f}" if fav and paper_mode else ""
            flag = "🧪紙盤" if paper_mode else "🔴實盤"
            await rt.notify(
                f"♻️ {flag} redeem 取回 {job['slug'] or cid}\n"
                f"Up {up:.1f}×{up_p} + Down {down:.1f}×{dn_p} = ${payout:.2f}{extra}",
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
