from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

from polyarb.config import Config, env_overrides, load_config, use_data_dir
from polyarb.data.clob import ClobClient
from polyarb.data.gamma import GammaClient
from polyarb.data.store import BookStore
from polyarb.engine import Engine, ScanResult, scan_once
from polyarb.execution.executor import Executor
from polyarb.execution.paper import PaperGateway
from polyarb.journal import Journal
from polyarb.risk import RiskManager, api_allowed
from polyarb.strategies.ladder_parse import detect_ladders
from polyarb.universe import build_strategies

UNGATED = {
    "convert.min_profit_usd": 0.0,
    "convert.min_edge": 0.0,
    "yes_basket.min_profit_usd": 0.0,
    "yes_basket.min_edge": 0.0,
    "yes_basket.min_apr": 0.0,
    "yes_basket.max_days": 3650.0,
    "ladder.min_profit_usd": 0.0,
    "ladder.min_edge": 0.0,
    "ladder.min_apr": 0.0,
    "ladder.max_days": 3650.0,
}


def _fmt_money(x: float) -> str:
    return f"${x:,.2f}"


def print_scan(result: ScanResult, top: int) -> None:
    u = result.universe
    t = result.timings
    print(f"\nUniverse: {len(u.events)} events, {len(u.tokens)} YES books, targets {u.counts()}")
    print(
        "Timings: gamma {gamma_s:.1f}s | universe {universe_s:.1f}s | books {books_s:.1f}s | evaluate {evaluate_s:.1f}s".format(**t)
    )
    print("\nTop-of-book edge per unit (after taker fees, ignoring depth):")
    for name, stats in result.edge_stats().items():
        print(f"  {name:20s} {stats}")
    opps = result.opportunities
    print(f"\nExecutable opportunities passing gates: {len(opps)}")
    for opp, _ in opps[:top]:
        apr = opp.apr()
        days = opp.days_to_resolution()
        print(
            f"\n[{opp.strategy} | risk {opp.risk}] {opp.title}\n"
            f"  size {opp.shares:.2f} sh/leg  cost {_fmt_money(opp.cost)}  payout>={_fmt_money(opp.payout_floor)}  "
            f"profit {_fmt_money(opp.profit)} ({100 * opp.edge:.2f}%)  fees {_fmt_money(opp.fees)}"
            + (f"  days {days:.1f}  APR {100 * apr:.0f}%" if apr is not None and days is not None else "  settles instantly (convert)")
        )
        for leg in opp.legs:
            print(f"    BUY {leg.outcome.value:3s} {leg.shares:9.2f} @avg {leg.avg_price:.4f} (limit {leg.limit_price}) {leg.label[:70]}")


def scan_payload(result: ScanResult, near: int = 25) -> dict[str, Any]:
    near_misses = {}
    for name, rows in result.edges.items():
        rows = sorted(rows, key=lambda r: -r[0])[:near]
        near_misses[name] = [{"edge": round(e, 5), "title": t.event.title, "key": t.key, "legs": len(t.tokens)} for e, t in rows]
    return {
        "universe": {"events": len(result.universe.events), "tokens": len(result.universe.tokens), "targets": result.universe.counts()},
        "timings": {k: round(v, 2) for k, v in result.timings.items()},
        "edge_stats": result.edge_stats(),
        "opportunities": [opp.summary() for opp, _ in result.opportunities],
        "near_misses": near_misses,
    }


async def cmd_scan(cfg: Config, args: argparse.Namespace) -> int:
    strategies = build_strategies(cfg)
    filters = dict(cfg.universe.gamma_filters)
    async with GammaClient() as gamma, ClobClient() as clob:
        events = None
        if args.max_pages:
            from polyarb.data.gamma import parse_event

            events = []
            async for raw in gamma.iter_raw_events(max_pages=args.max_pages, **filters):
                ev = parse_event(raw)
                if len(ev.tradeable_markets) >= cfg.universe.min_markets:
                    events.append(ev)
        result = await scan_once(cfg, strategies, gamma, clob, events)
    print_scan(result, args.top)
    if args.json:
        Path(args.json).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json).write_text(json.dumps(scan_payload(result), indent=2, default=str, ensure_ascii=False))
        print(f"\nwrote {args.json}")
    return 0


async def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    mode = args.mode or cfg.execution.mode
    strategies = build_strategies(cfg)
    store = BookStore(max_age_s=cfg.universe.max_book_age_s)
    risk = RiskManager(cfg.risk, cfg.execution.state_path)
    journal = Journal(cfg.execution.journal_path)
    async with GammaClient() as gamma, ClobClient() as clob:
        if mode == "live":
            if args.no_gates:
                print("Refusing live mode with --no-gates.")
                return 2
            if os.environ.get("POLYARB_LIVE_CONFIRM") != "yes":
                print("Refusing live mode: set POLYARB_LIVE_CONFIRM=yes after reading the README risk section.")
                return 2
            geo = await risk.check_geo()
            if not api_allowed(geo):
                print(f"Refusing live mode: Polymarket does not accept API orders from this IP ({geo})")
                return 2
            from polyarb.execution.live import PolymarketGateway

            key = os.environ.get("POLYMARKET_PRIVATE_KEY")
            if not key:
                print("POLYMARKET_PRIVATE_KEY is not set")
                return 2
            gateway = await PolymarketGateway.create(key, wallet=os.environ.get("POLYMARKET_WALLET") or None)
        else:
            cfg.risk.require_geo_ok = False
            gateway = PaperGateway(store, fail_prob=args.paper_fail_prob, seed=args.seed)
        executor = Executor(gateway, store, {s.name: s for s in strategies}, cfg.execution, clob)
        engine = Engine(cfg, strategies, gamma, clob, store, executor, risk, journal)
        try:
            await engine.run(args.duration)
        finally:
            await gateway.close()
    print(json.dumps({"stats": dict(engine.stats), "pnl_total": risk.state.realized_total, "holds": len(risk.state.holds)}, indent=2))
    return 0


async def cmd_ladders(cfg: Config, args: argparse.Namespace) -> int:
    from polyarb.data.gamma import parse_event

    params: dict[str, Any] = {}
    if args.search:
        params["title_search"] = args.search
    count = 0
    async with GammaClient() as gamma:
        async for raw in gamma.iter_raw_events(max_pages=args.max_pages, **params):
            for ladder in detect_ladders(parse_event(raw)):
                count += 1
                print(f"\n{ladder.event.title}  [{ladder.direction.value}] {len(ladder.rungs)} rungs  ({ladder.key})")
                for r in ladder.rungs:
                    print(f"   {r.value:>16g}  {r.market.question[:100]}")
    print(f"\n{count} ladders")
    return 0


async def cmd_serve(cfg: Config, args: argparse.Namespace) -> int:
    from polyarb.app import serve

    return await serve(cfg, host=args.host, port=args.port)


async def cmd_geo(cfg: Config, args: argparse.Namespace) -> int:
    geo = await RiskManager(cfg.risk).check_geo()
    print(json.dumps({**geo, "api_allowed": api_allowed(geo)}, indent=2))
    return 0


def cmd_status(cfg: Config, args: argparse.Namespace) -> int:
    risk = RiskManager(cfg.risk, cfg.execution.state_path)
    s = risk.state
    print(json.dumps(
        {
            "killed": s.killed,
            "kill_reason": s.kill_reason,
            "realized_total": round(s.realized_total, 4),
            "realized_today": round(s.realized_today, 4),
            "trades": s.trades,
            "available": round(risk.available(), 2),
            "hold_exposure": round(risk.hold_exposure, 2),
            "holds": [{"title": h.title, "cost": h.cost, "payout": h.payout, "resolves_at": h.resolves_at} for h in s.holds],
        },
        indent=2,
    ))
    return 0


def cmd_reset(cfg: Config, args: argparse.Namespace) -> int:
    RiskManager(cfg.risk, cfg.execution.state_path).reset()
    print("kill switch cleared")
    return 0


async def cmd_convert(cfg: Config, args: argparse.Namespace) -> int:
    """Manual recovery: convert NO positions you already hold (live wallet)."""
    from polyarb.execution.live import PolymarketGateway
    from polyarb.execution.settlement import index_set

    key = os.environ.get("POLYMARKET_PRIVATE_KEY")
    if not key:
        print("POLYMARKET_PRIVATE_KEY is not set")
        return 2
    gateway = await PolymarketGateway.create(key, wallet=os.environ.get("POLYMARKET_WALLET") or None)
    try:
        indices = [int(x) for x in args.indices.split(",")]
        ok, tx, proceeds = await gateway.convert(args.market_id, index_set(indices), args.shares)
        print(json.dumps({"ok": ok, "tx": tx, "pusd_out": proceeds}, indent=2))
    finally:
        await gateway.close()
    return 0 if ok else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="polyarb", description="Polymarket structural arbitrage bot")
    p.add_argument("-c", "--config", help="YAML config (see config/default.yaml)")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="one-shot scan of every open event")
    s.add_argument("--top", type=int, default=15)
    s.add_argument("--json", help="write full results to this path")
    s.add_argument("--max-pages", type=int, help="limit Gamma pages (100 events each) for a quick look")
    s.add_argument("--no-gates", action="store_true", help="drop profit/edge/APR gates to see every positive edge")

    sv = sub.add_parser("serve", help="engine + web dashboard (paper unless every live precondition holds)")
    sv.add_argument("--host", default="0.0.0.0")
    sv.add_argument("--port", type=int, help="default: $PORT or 8080")

    r = sub.add_parser("run", help="continuous engine (paper by default)")
    r.add_argument("--mode", choices=["paper", "live"])
    r.add_argument("--duration", type=float, help="seconds to run")
    r.add_argument("--paper-fail-prob", type=float, default=0.0, help="paper only: random leg miss probability")
    r.add_argument("--seed", type=int)
    r.add_argument("--no-gates", action="store_true")

    l = sub.add_parser("ladders", help="list detected implication ladders")
    l.add_argument("--search", help="Gamma title_search filter")
    l.add_argument("--max-pages", type=int, default=3)

    sub.add_parser("geo", help="check whether Polymarket accepts API orders from this IP")
    cl = sub.add_parser("crypto-lock", help="paper-only: buy a locked 5-minute crypto side while the ask is still cheap")
    cl.add_argument("--duration", type=float, help="seconds to run (default: until stopped)")
    cl.add_argument("--journal", help="default: $POLYARB_DATA_DIR/crypto-lock.jsonl or data/crypto-lock.jsonl")
    for name, text in (("status", "risk state, holds and PnL"), ("reset-kill", "clear the kill switch")):
        sp = sub.add_parser(name, help=text)
        sp.add_argument("--mode", choices=["paper", "live"], help="which book under $POLYARB_DATA_DIR (default: $POLYARB_MODE)")

    c = sub.add_parser("convert", help="manually convert held NegRisk NO positions (live)")
    c.add_argument("--market-id", required=True)
    c.add_argument("--indices", required=True, help="comma separated question indices")
    c.add_argument("--shares", type=float, required=True)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    overrides = {**env_overrides(), **(UNGATED if getattr(args, "no_gates", False) else {})}
    cfg = load_config(args.config or os.environ.get("POLYARB_CONFIG"), overrides)
    data_dir = os.environ.get("POLYARB_DATA_DIR")
    if data_dir and args.cmd in ("run", "status", "reset-kill"):
        mode = getattr(args, "mode", None) or os.environ.get("POLYARB_MODE") or cfg.execution.mode
        use_data_dir(cfg, data_dir, mode)
    if args.cmd == "scan":
        return asyncio.run(cmd_scan(cfg, args))
    if args.cmd == "serve":
        return asyncio.run(cmd_serve(cfg, args))
    if args.cmd == "run":
        return asyncio.run(cmd_run(cfg, args))
    if args.cmd == "ladders":
        return asyncio.run(cmd_ladders(cfg, args))
    if args.cmd == "geo":
        return asyncio.run(cmd_geo(cfg, args))
    if args.cmd == "status":
        return cmd_status(cfg, args)
    if args.cmd == "reset-kill":
        return cmd_reset(cfg, args)
    if args.cmd == "convert":
        return asyncio.run(cmd_convert(cfg, args))
    if args.cmd == "crypto-lock":
        from polyarb.crypto_lock_run import run_paper

        journal = Path(args.journal or (Path(os.environ.get("POLYARB_DATA_DIR") or "data") / "crypto-lock.jsonl"))
        return asyncio.run(run_paper(journal, args.duration))
    return 1


if __name__ == "__main__":
    sys.exit(main())
