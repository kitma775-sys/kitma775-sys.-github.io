from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx

from app.broker import FillResult, LiveBroker, PaperBroker
from app.config import Env, clamp_paper_cash, live_keys_ready
from app.hunter import hunt
from app.markets import MarketData
from app.paper_sim import asks_cross_bid, market_expired
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

    def snapshot(self) -> dict[str, Any]:
        s = self.settings()
        st = self.store.stats()
        paper = self.store.paper_state()
        return {
            "mode": self.mode(),
            "keys_ready": live_keys_ready(self.env),
            "force_paper": self.env.force_paper,
            "uptime_s": int(time.time() - self.started_at),
            "geo": self.geo,
            "settings": s,
            "stats": st,
            "paper": paper,
            "last_loop": self.last_loop,
            "inventory": self.store.inventory()[:20],
            "resting": self.store.resting_open()[:20],
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
    if s.get("killed"):
        n = rt.store.cancel_all_resting("kill")
        if n:
            await rt.notify(f"🛑 已撤 {n} 張紙盤掛單")
        rt.last_loop["status"] = "killed"
        return
    if not s.get("engine_running"):
        rt.last_loop["status"] = "paused"
        return
    assert rt.data is not None
    events = await rt.data.live_events(s.get("tag") or "15M", list(s.get("assets") or ["btc", "eth"]))
    resting_fills = await _process_resting(rt)
    rt.last_loop = {"ts": time.time(), "status": "scan", "markets": len(events), "signals": 0, "fills": 0}
    broker = rt.broker()
    paper_mode = rt.mode() == "paper"
    paper = rt.store.paper_state() if paper_mode else None
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
            max_usd=_trade_budget(s, paper),
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
        if paper_mode and rt.store.has_open_resting(setup.slug):
            continue
        if paper_mode:
            setup.extra["paper_slip_ticks"] = int(s.get("paper_slip_ticks") or 0)
        cool = float(s.get("quote_cooldown_seconds") or 30)
        last = rt.cooldown.get(setup.slug, 0)
        if time.time() - last < cool:
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
            net=(result.payload or {}).get("net", setup.net) if result.ok and result.status in {"filled", "paper_filled"} else 0.0,
            mode=result.mode,
            status=result.status,
            payload={"detail": result.detail, **(result.payload or {})},
        )
        if result.ok and result.status in {"filled", "paper_filled"}:
            fills += 1
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
                    f"{flag} 成交 {setup.kind}\n{setup.title}\n"
                    f"Up {fill_up} + Down {fill_down} × {setup.shares:.1f}\n"
                    f"淨利 ${fill_net:.2f}{book}",
                    important=True,
                )
        elif result.ok and result.status in {"paper_resting", "resting"}:
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
                        payload={"detail": result.detail},
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
    rt.last_loop.update({"signals": signals, "fills": fills + resting_fills, "status": "ok", "paper": paper, "resting_fills": resting_fills})


async def _process_resting(rt: Runtime) -> int:
    """Fill paper maker legs only when the live book trades through the resting bid."""
    if rt.mode() != "paper" or rt.data is None:
        return 0
    s = rt.settings()
    fills = 0
    for row in list(rt.store.resting_open()):
        if market_expired(row.get("end")):
            rt.store.cancel_resting(row["id"], "expired")
            rt.store.add_event("info", f"paper rest expired {row['slug']}")
            if s.get("notify_signals"):
                leftover = "；已成交腳按 $0 計" if (row["up_filled"] or row["down_filled"]) else ""
                await rt.notify(f"⌛ 紙盤掛單到期撤單 {row['slug']}{leftover}")
            continue
        try:
            up_book, dn_book = await asyncio.gather(
                rt.data.book(row["up_token"]),
                rt.data.book(row["down_token"]),
            )
        except Exception as exc:
            rt.store.add_event("warn", f"rest book {row['slug']}: {exc}"[:200])
            continue
        filled_now = []
        if not row["up_filled"] and asks_cross_bid(up_book["asks"], float(row["up_price"]), float(row["shares"])):
            row = rt.store.fill_resting_leg(row["id"], "up")
            filled_now.append("Up")
        if not row["down_filled"] and asks_cross_bid(dn_book["asks"], float(row["down_price"]), float(row["shares"])):
            row = rt.store.fill_resting_leg(row["id"], "down")
            filled_now.append("Down")
        if not filled_now:
            continue
        if row["status"] == "filled" and row["up_filled"] and row["down_filled"]:
            fills += 1
            take = 0.0
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
        else:
            rt.store.add_trade(
                slug=row["slug"],
                kind="maker",
                shares=row["shares"],
                up_price=row["up_price"],
                down_price=row["down_price"],
                net=0.0,
                mode="paper",
                status="paper_leg_fill",
                payload={"detail": f"legs {filled_now}", "resting_id": row["id"], "unmatched_mark": 0},
            )
            if s.get("notify_signals"):
                await rt.notify(
                    f"📌 紙盤只成交 {','.join(filled_now)} {row.get('title') or row['slug']}\n"
                    "另一邊未碰到，唔 merge，未配對倉按 $0 計"
                )
    return fills



def _trade_budget(s: dict, paper: dict | None) -> float:
    cap = float(s["max_usd_per_trade"])
    if not paper:
        return cap
    # leave a small buffer so taker fees don't overshoot remaining cash
    return max(0.0, min(cap, float(paper["cash"]) - 0.25))
