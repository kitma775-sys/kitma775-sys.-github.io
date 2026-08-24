from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.broker import FillResult, LiveBroker, PaperBroker
from app.config import Env, live_keys_ready
from app.hunter import hunt
from app.markets import MarketData
from app.risk import approve
from app.store import Store


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

    def settings(self) -> dict:
        return self.store.settings()

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

    def snapshot(self) -> dict[str, Any]:
        s = self.settings()
        st = self.store.stats()
        return {
            "mode": self.mode(),
            "keys_ready": live_keys_ready(self.env),
            "force_paper": self.env.force_paper,
            "uptime_s": int(time.time() - self.started_at),
            "geo": self.geo,
            "settings": s,
            "stats": st,
            "last_loop": self.last_loop,
            "inventory": self.store.inventory()[:20],
            "trades": self.store.recent_trades(15),
            "scans": self.store.recent_scans(12),
            "events": self.store.recent_events(15),
        }


async def engine_loop(rt: Runtime) -> None:
    backoff = 1.0
    while True:
        s = rt.settings()
        poll = max(0.5, float(s.get("poll_seconds") or 2))
        try:
            if rt.http is None:
                rt.http = httpx.AsyncClient(headers={"User-Agent": "surf-arb-bot/0.1"}, timeout=15)
                rt.data = MarketData(rt.http)
                rt.geo = await rt.data.geoblock()
                rt.store.add_event("info", f"geoblock={rt.geo}")
            await _tick(rt)
            backoff = 1.0
        except Exception as exc:
            rt.store.add_event("error", f"loop {exc}"[:300])
            await rt.notify(f"⚠️ 引擎出錯：{exc}"[:200], important=True)
            backoff = min(30.0, backoff * 2)
        await asyncio.sleep(max(poll, backoff) if rt.settings().get("engine_running") else 1.0)


async def _tick(rt: Runtime) -> None:
    s = rt.settings()
    rt.last_loop = {"ts": time.time(), "status": "idle"}
    if s.get("killed") or not s.get("engine_running"):
        rt.last_loop["status"] = "paused" if not s.get("killed") else "killed"
        return
    assert rt.data is not None
    events = await rt.data.live_events(s.get("tag") or "15M", list(s.get("assets") or ["btc", "eth"]))
    rt.last_loop = {"ts": time.time(), "status": "scan", "markets": len(events), "signals": 0, "fills": 0}
    broker = rt.broker()
    signals = 0
    fills = 0
    for ev in events:
        up_book, dn_book = await asyncio.gather(
            rt.data.book(ev["up_token"]),
            rt.data.book(ev["down_token"]),
        )
        fee_rate = float(ev.get("fee_rate") or s.get("fee_rate") or 0.07)
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
            max_usd=float(s["max_usd_per_trade"]),
            min_shares=max(float(s["min_shares"]), float(ev.get("min_size") or 5)),
            min_edge=float(s["min_edge"]),
            fee_rate=fee_rate,
            prefer_tail=bool(s["prefer_tail"]),
            tail_confirm=float(s["tail_confirm"]),
            maker_first=bool(s["maker_first"]),
            end=ev.get("end"),
        )
        if not setup:
            continue
        cool = float(s.get("quote_cooldown_seconds") or 30)
        last = rt.cooldown.get(setup.slug, 0)
        if time.time() - last < cool:
            continue
        signals += 1
        inv = rt.store.inventory_one(setup.condition_id)
        decision = approve(
            setup,
            stale_leg=float(s["stale_leg"]),
            tail_confirm=float(s["tail_confirm"]),
            max_imbalance=float(s["max_imbalance_shares"]),
            inventory_up=float(inv["up"]),
            inventory_down=float(inv["down"]),
            daily_pnl=rt.store.today_pnl(),
            daily_loss_limit=float(s["daily_loss_limit_usd"]),
            open_markets=rt.store.stats()["open_markets"],
            max_open_markets=int(s["max_open_markets"]),
            killed=bool(s["killed"]),
            engine_running=bool(s["engine_running"]),
            auto_execute=bool(s["auto_execute"]),
        )
        payload = {
            "title": setup.title,
            "kind": setup.kind,
            "up": setup.up_price,
            "down": setup.down_price,
            "shares": setup.shares,
            "net": setup.net,
            "gross": setup.gross,
            "tail": setup.tail,
            "reason": decision.reason,
            "mode": rt.mode(),
        }
        rt.store.add_scan(setup.slug, setup.kind, payload)
        if not decision.ok:
            if s.get("notify_rejects"):
                await rt.notify(f"⏭ 跳過 {setup.title}\n原因：{decision.reason}")
            continue
        result: FillResult = await broker.execute_pair(setup)
        rt.cooldown[setup.slug] = time.time()
        rt.store.add_trade(
            slug=setup.slug,
            kind=setup.kind,
            shares=setup.shares,
            up_price=setup.up_price,
            down_price=setup.down_price,
            net=setup.net if result.ok and result.status in {"filled", "paper_filled"} else 0.0,
            mode=result.mode,
            status=result.status,
            payload={"detail": result.detail, **(result.payload or {})},
        )
        if result.ok and result.status in {"filled", "paper_filled"}:
            fills += 1
            rt.store.add_inventory(setup.condition_id, setup.slug, setup.shares, setup.shares)
            if s.get("auto_merge"):
                merged = rt.store.merge_inventory(setup.condition_id, setup.shares)
                await broker.merge(setup.condition_id, merged["merged"])
            if s.get("notify_signals"):
                flag = "🧪紙盤" if result.mode == "paper" else "🔴實盤"
                await rt.notify(
                    f"{flag} 成交 {setup.kind}\n{setup.title}\n"
                    f"Up {setup.up_price} + Down {setup.down_price} × {setup.shares:.1f}\n"
                    f"淨利約 ${setup.net:.2f}",
                    important=True,
                )
        elif result.ok and result.status in {"paper_resting", "resting"}:
            if s.get("notify_signals"):
                await rt.notify(f"📌 掛單 {setup.title}\n{setup.up_price}+{setup.down_price} × {setup.shares:.1f}")
        else:
            await rt.notify(f"❌ 下單失敗：{result.detail}", important=True)
    rt.last_loop.update({"signals": signals, "fills": fills, "status": "ok"})
