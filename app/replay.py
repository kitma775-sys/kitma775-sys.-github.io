"""Replay hunt/rescue on a historical trade tape.

CLOB /prices-history is a single mid series (YES+NO always sums to 1), so it
cannot show complement edge. data-api trades are independent Up/Down prints.
We rebuild a stale-limited bid/ask proxy from recent BUY (ask) and SELL (bid)
prints, then run the same hunt / asks_cross_bid / plan_rescue path as paper.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import groupby
from typing import Any

from app.config import DEFAULT_SETTINGS
from app.hunter import Level, hunt
from app.paper_sim import asks_cross_bid, parse_end, seconds_left
from app.rescue import plan_rescue
from app.risk import approve


def _dt(ts: float) -> datetime:
    return datetime.fromtimestamp(float(ts), tz=timezone.utc)


def live_replay_settings(**overrides: Any) -> dict:
    s = dict(DEFAULT_SETTINGS)
    s.update(overrides)
    return s


def normalize_trade(raw: dict) -> dict | None:
    try:
        ts = int(raw["t"] if "t" in raw else raw["timestamp"])
        price = float(raw["price"])
        size = float(raw.get("size") or 0)
        side = str(raw.get("side") or "").upper()
    except (KeyError, TypeError, ValueError):
        return None
    if side not in {"BUY", "SELL"} or price <= 0 or size <= 0:
        return None
    outcome = str(raw.get("outcome") or "").strip().lower()
    idx = raw.get("outcomeIndex")
    if outcome in {"up", "yes"}:
        leg = "up"
    elif outcome in {"down", "no"}:
        leg = "down"
    elif idx in (0, "0"):
        leg = "up"
    elif idx in (1, "1"):
        leg = "down"
    else:
        return None
    return {"t": ts, "side": side, "price": price, "size": size, "outcome": leg}


@dataclass
class Tape:
    prints: list[dict] = field(default_factory=list)

    def add(self, trade: dict) -> None:
        self.prints.append(trade)

    def books(self, now_t: float, *, ask_stale_s: float = 1.0, bid_stale_s: float = 10.0) -> dict[str, dict[str, list[Level]]]:
        now_t = float(now_t)
        ask_cut = now_t - float(ask_stale_s)
        bid_cut = now_t - float(bid_stale_s)
        out: dict[str, dict[str, list[Level]]] = {}
        for leg in ("up", "down"):
            buys = [p for p in self.prints if p["outcome"] == leg and p["side"] == "BUY" and ask_cut <= p["t"] <= now_t]
            sells = [p for p in self.prints if p["outcome"] == leg and p["side"] == "SELL" and bid_cut <= p["t"] <= now_t]
            asks: list[Level] = []
            bids: list[Level] = []
            if buys:
                last = buys[-1]
                depth = sum(p["size"] for p in buys)
                asks = [Level(round(last["price"], 4), max(depth, 5.0))]
            if sells:
                last = sells[-1]
                depth = sum(p["size"] for p in sells)
                bids = [Level(round(last["price"], 4), max(depth, 5.0))]
            out[leg] = {"asks": asks, "bids": bids}
        return out


@dataclass
class Resting:
    setup: Any
    up_filled: bool = False
    down_filled: bool = False


def _empty_stats() -> dict:
    return {
        "n_trades_in": 0,
        "taker_n": 0,
        "taker_pnl": 0.0,
        "maker_quoted": 0,
        "maker_two_sided_n": 0,
        "maker_two_sided_pnl": 0.0,
        "maker_hedge_n": 0,
        "maker_hedge_pnl": 0.0,
        "maker_dump_n": 0,
        "maker_dump_pnl": 0.0,
        "maker_expire_unfilled": 0,
        "maker_expire_settle_n": 0,
        "maker_expire_settle_pnl": 0.0,
        "pnl": 0.0,
        "events": [],
    }


def replay_market(
    trades: list[dict],
    *,
    end: str,
    settings: dict | None = None,
    resolution: tuple[float, float] | None = None,
    slug: str = "mkt",
    ask_stale_s: float = 1.0,
    bid_stale_s: float = 10.0,
    allow_taker: bool = True,
) -> dict:
    """Replay one market. `trades` are normalize_trade dicts or data-api rows."""
    s = live_replay_settings() if settings is None else settings
    stats = _empty_stats()
    tape = Tape()
    rest: Resting | None = None
    rows = []
    for raw in trades:
        row = raw if "outcome" in raw and "t" in raw and "side" in raw else normalize_trade(raw)
        if row:
            rows.append(row)
    rows.sort(key=lambda r: (r["t"], r["outcome"], r["side"]))
    end_ts = None
    edt = parse_end(end)
    if edt is not None:
        end_ts = int(edt.timestamp())
    if end_ts is not None:
        rows = [r for r in rows if r["t"] <= end_ts + 2]
    stats["n_trades_in"] = len(rows)

    def _clock_rows() -> list[dict]:
        if end_ts is None:
            return rows
        return rows + [{"t": end_ts, "expiry": True}]

    for now_t, group in groupby(_clock_rows(), key=lambda r: r["t"]):
        batch = list(group)
        clock = _dt(now_t)
        left = seconds_left(end, clock)
        expiry = any(r.get("expiry") for r in batch)
        for row in batch:
            if not row.get("expiry"):
                tape.add(row)
        books = tape.books(float(now_t), ask_stale_s=ask_stale_s, bid_stale_s=bid_stale_s)
        if rest is not None:
            rest, stats = _progress_resting(rest, books, s, left, resolution, stats)
            continue
        if expiry or left is None or left <= 0:
            continue
        setup = hunt(
            slug=slug,
            title=slug,
            condition_id=slug,
            up_token="u",
            down_token="d",
            up_asks=books["up"]["asks"] if allow_taker else [],
            down_asks=books["down"]["asks"] if allow_taker else [],
            up_bids=books["up"]["bids"],
            down_bids=books["down"]["bids"],
            max_usd=float(s["max_usd_per_trade"]),
            min_shares=float(s["min_shares"]),
            min_edge=float(s["min_edge"]),
            fee_rate=float(s["fee_rate"]),
            prefer_tail=bool(s["prefer_tail"]),
            tail_confirm=float(s["tail_confirm"]),
            maker_first=bool(s["maker_first"]),
            end=end,
            maker_min_leg=float(s.get("maker_min_leg") or 0.22),
            maker_max_skew=float(s.get("maker_max_skew") or 0.28),
            maker_window_seconds=float(s.get("maker_window_seconds") or 75),
            maker_min_edge=s.get("maker_min_edge"),
            now=clock,
        )
        if setup is None:
            continue
        decision = approve(
            setup,
            stale_leg=float(s["stale_leg"]),
            tail_confirm=float(s["tail_confirm"]),
            max_imbalance=float(s["max_imbalance_shares"]),
            inventory_up=0.0,
            inventory_down=0.0,
            daily_pnl=stats["pnl"],
            daily_loss_limit=float(s["daily_loss_limit_usd"]),
            open_markets=1 if rest else 0,
            max_open_markets=int(s["max_open_markets"]),
            killed=False,
            engine_running=True,
            auto_execute=True,
            cash=10_000.0,
            cost=setup.cost,
            unmatched_shares=0.0,
            seconds_left=left,
            maker_window=float(s.get("maker_window_seconds") or 75),
            maker_min_leg=float(s.get("maker_min_leg") or 0.22),
            maker_max_skew=float(s.get("maker_max_skew") or 0.28),
        )
        if not decision.ok:
            continue
        if setup.kind == "taker":
            stats["taker_n"] += 1
            stats["taker_pnl"] = round(stats["taker_pnl"] + setup.net, 6)
            stats["pnl"] = round(stats["pnl"] + setup.net, 6)
            stats["events"].append({"kind": "taker", "pnl": setup.net, "up": setup.up_price, "down": setup.down_price, "t": now_t})
            continue
        stats["maker_quoted"] += 1
        rest = Resting(setup=setup)
        rest, stats = _progress_resting(rest, books, s, left, resolution, stats)

    if rest is not None:
        rest, stats = _expire_resting(
            rest,
            tape.books(end_ts or (rows[-1]["t"] if rows else 0), ask_stale_s=ask_stale_s, bid_stale_s=bid_stale_s),
            resolution,
            stats,
        )
    stats["pnl"] = round(stats["pnl"], 4)
    return stats


def _progress_resting(rest: Resting, books: dict, settings: dict, left: float | None, resolution: tuple[float, float] | None, stats: dict) -> tuple[Resting | None, dict]:
    setup = rest.setup
    shares = float(setup.shares)
    if not rest.up_filled and asks_cross_bid(books["up"]["asks"], setup.up_price, shares):
        rest.up_filled = True
    if not rest.down_filled and asks_cross_bid(books["down"]["asks"], setup.down_price, shares):
        rest.down_filled = True
    if rest.up_filled and rest.down_filled:
        stats["maker_two_sided_n"] += 1
        stats["maker_two_sided_pnl"] = round(stats["maker_two_sided_pnl"] + setup.net, 6)
        stats["pnl"] = round(stats["pnl"] + setup.net, 6)
        stats["events"].append({"kind": "maker_two_sided", "pnl": setup.net, "up": setup.up_price, "down": setup.down_price})
        return None, stats
    one = rest.up_filled != rest.down_filled
    if one:
        fee = float(settings.get("fee_rate") or 0.07)
        if rest.up_filled:
            plan = plan_rescue(
                filled_px=setup.up_price,
                shares=shares,
                other_asks=books["down"]["asks"],
                filled_bids=books["up"]["bids"],
                fee_rate=fee,
            )
        else:
            plan = plan_rescue(
                filled_px=setup.down_price,
                shares=shares,
                other_asks=books["up"]["asks"],
                filled_bids=books["down"]["bids"],
                fee_rate=fee,
            )
        if plan.action == "hedge":
            stats["maker_hedge_n"] += 1
            stats["maker_hedge_pnl"] = round(stats["maker_hedge_pnl"] + plan.pnl, 6)
            stats["pnl"] = round(stats["pnl"] + plan.pnl, 6)
            stats["events"].append({"kind": "maker_hedge", "pnl": plan.pnl, "price": plan.price})
            return None, stats
        if plan.action == "dump":
            stats["maker_dump_n"] += 1
            stats["maker_dump_pnl"] = round(stats["maker_dump_pnl"] + plan.pnl, 6)
            stats["pnl"] = round(stats["pnl"] + plan.pnl, 6)
            stats["events"].append({"kind": "maker_dump", "pnl": plan.pnl, "price": plan.price})
            return None, stats
    if left is not None and left <= 0:
        return _expire_resting(rest, books, resolution, stats)
    return rest, stats


def _expire_resting(rest: Resting, books: dict, resolution: tuple[float, float] | None, stats: dict) -> tuple[None, dict]:
    setup = rest.setup
    shares = float(setup.shares)
    if rest.up_filled == rest.down_filled:
        if rest.up_filled:
            stats["maker_two_sided_n"] += 1
            stats["maker_two_sided_pnl"] = round(stats["maker_two_sided_pnl"] + setup.net, 6)
            stats["pnl"] = round(stats["pnl"] + setup.net, 6)
        else:
            stats["maker_expire_unfilled"] += 1
        return None, stats
    up_res, dn_res = resolution if resolution is not None else (0.0, 0.0)
    if rest.up_filled:
        pnl = round(shares * up_res - shares * setup.up_price, 6)
    else:
        pnl = round(shares * dn_res - shares * setup.down_price, 6)
    stats["maker_expire_settle_n"] += 1
    stats["maker_expire_settle_pnl"] = round(stats["maker_expire_settle_pnl"] + pnl, 6)
    stats["pnl"] = round(stats["pnl"] + pnl, 6)
    stats["events"].append({"kind": "maker_settle", "pnl": pnl})
    return None, stats
