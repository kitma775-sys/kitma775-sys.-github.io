"""Full-universe scan: fetch every open event once, then price every target with
the production gates and with the gates removed, against the same book snapshot.

    python research/full_scan.py research/scan.json '{"universe.max_seconds_delay": 3}'
"""

import asyncio
import json
import sys
import time
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyarb.cli import UNGATED, scan_payload  # noqa: E402
from polyarb.config import load_config  # noqa: E402
from polyarb.data.clob import ClobClient  # noqa: E402
from polyarb.data.gamma import GammaClient  # noqa: E402
from polyarb.engine import ScanResult, scan_once  # noqa: E402
from polyarb.universe import build_strategies, build_universe  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "scan_full.json"
EXTRA = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
CONFIG = ROOT / "config" / "default.yaml"


async def main() -> None:
    cfg = load_config(CONFIG, EXTRA)
    ungated_cfg = load_config(CONFIG, {**UNGATED, **EXTRA})
    t0 = time.time()
    async with GammaClient() as gamma, ClobClient() as clob:
        events = await gamma.fetch_events(min_markets=cfg.universe.min_markets)
        t1 = time.time()
        print(f"gamma: {len(events)} multi-market events in {t1 - t0:.0f}s", flush=True)
        gated = await scan_once(cfg, build_strategies(cfg), gamma, clob, events)

    strategies = build_strategies(ungated_cfg)
    universe = build_universe(events, strategies, ungated_cfg.universe)
    by_name = {s.name: s for s in strategies}
    opps = []
    now = time.time()
    for target in universe.targets:
        opps.extend((o, target) for o in by_name[target.strategy].evaluate(target, gated.store, now))
    opps.sort(key=lambda p: -p[0].profit)
    ungated = ScanResult(universe, gated.store, opps, gated.edges, gated.timings)

    neg = [e for e in events if e.neg_risk]
    profile = {
        "events_multi_market": len(events),
        "negrisk_events": len(neg),
        "negrisk_v1": sum(1 for e in neg if e.version == "v1"),
        "negrisk_augmented": sum(1 for e in neg if e.neg_risk_augmented),
        "markets": sum(len(e.tradeable_markets) for e in events),
        "fee_rates": dict(Counter(m.fee.rate for e in events for m in e.tradeable_markets).most_common()),
        "delayed_events": sum(1 for e in events if any(m.seconds_delay > 0 for m in e.tradeable_markets)),
        "books_fetched": len(gated.store),
        "books_with_bid_and_ask": sum(1 for b in gated.store.values() if b.bids and b.asks),
    }
    out = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "overrides": EXTRA,
        "gamma_s": round(t1 - t0, 1),
        "profile": profile,
        "gated": scan_payload(gated),
        "ungated": scan_payload(ungated),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, default=str, ensure_ascii=False))
    print(json.dumps({"profile": profile, "gated_opps": len(gated.opportunities), "ungated_opps": len(opps),
                      "edge_stats": gated.edge_stats()}, indent=2, default=str))
    print(f"wrote {OUT}")


asyncio.run(main())
