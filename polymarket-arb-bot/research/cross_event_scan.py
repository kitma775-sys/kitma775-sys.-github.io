"""Structural relations the three live strategies never see.

The live bot only builds targets inside one event. This scan looks across events for:

1. The same yes/no question listed as two markets (different condition ids).
2. Implication ladders whose rungs sit in different events.

Gamma's displayed prices only nominate candidates. Executable edge is computed
from a CLOB book snapshot, taker fees included, top of book only.

    python research/cross_event_scan.py research/cross_event_2026-10-16.json
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from polyarb.data.clob import ClobClient  # noqa: E402
from polyarb.data.gamma import GammaClient, parse_market  # noqa: E402
from polyarb.fees import fee_per_share  # noqa: E402
from polyarb.models import Event, Market  # noqa: E402
from polyarb.strategies.ladder_parse import detect_ladders  # noqa: E402

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "research" / "cross_event.json"
_SPACE = re.compile(r"\s+")


@dataclass
class Listed:
    event_id: str
    event_title: str
    event_slug: str
    neg_risk: bool
    market: Market


def norm_question(question: str) -> str:
    return _SPACE.sub(" ", question.strip().lower().replace("−", "-")).rstrip("?").strip()


def days_apart(a: datetime | None, b: datetime | None) -> float | None:
    if a is None or b is None:
        return None
    return abs((a - b).total_seconds()) / 86400.0


def pair_edge(store, left: Listed, right: Listed) -> dict | None:
    """Buy YES on one listing and NO on the other. Pays $1 if they resolve the same way."""
    book_l = store.get(left.market.yes_token)
    book_r = store.get(right.market.yes_token)
    if book_l is None or book_r is None:
        return None
    best = None
    for buy, other, bb, bo in (
        (left, right, book_l, book_r),
        (right, left, book_r, book_l),
    ):
        if not bb.asks or not bo.bids:
            continue
        ask = bb.asks[0].price
        no_price = 1.0 - bo.bids[0].price
        cost = ask + fee_per_share(buy.market.fee, ask) + no_price + fee_per_share(other.market.fee, no_price)
        edge = 1.0 - cost
        shares = min(bb.asks[0].size, bo.bids[0].size)
        row = {
            "edge": edge,
            "shares": shares,
            "profit": edge * shares,
            "buy_yes": buy.event_title,
            "buy_question": buy.market.question,
            "hedge_no": other.event_title,
        }
        if best is None or row["profit"] > best["profit"]:
            best = row
    return best


def implies_by_conjunction(weaker: str, stronger: str) -> bool:
    """``stronger`` is ``weaker`` plus `` and ...``, so the longer proposition implies the shorter one."""
    if not weaker or not stronger or weaker == stronger:
        return False
    return stronger.startswith(weaker + " and ")


def shell_event(markets: list[Market]) -> Event:
    return Event(
        id="cross",
        slug="",
        title="",
        markets=markets,
        neg_risk=False,
        neg_risk_augmented=False,
        neg_risk_market_id=None,
        end_date=None,
        version="v1",
    )


async def main() -> None:
    listed: list[Listed] = []
    reward_by_condition: dict[str, float] = {}
    holding_markets = 0
    t0 = time.time()
    async with GammaClient() as gamma:
        async for raw in gamma.iter_raw_events():
            event_end = None
            title = str(raw.get("title") or "")
            for rm in raw.get("markets") or []:
                if rm.get("holdingRewardsEnabled") is True:
                    holding_markets += 1
                for slot in rm.get("clobRewards") or []:
                    try:
                        rate = float(slot.get("rewardsDailyRate") or 0)
                    except (TypeError, ValueError):
                        rate = 0.0
                    if rate > 0 and rm.get("conditionId"):
                        reward_by_condition[str(rm["conditionId"])] = max(
                            reward_by_condition.get(str(rm["conditionId"]), 0.0), rate
                        )
                market = parse_market(rm, event_end)
                if market is None or not market.accepting_orders or not market.is_yes_no:
                    continue
                if market.seconds_delay > 0:
                    continue
                listed.append(
                    Listed(
                        event_id=str(raw.get("id")),
                        event_title=title,
                        event_slug=str(raw.get("slug") or ""),
                        neg_risk=bool(raw.get("negRisk") or raw.get("enableNegRisk") or market.neg_risk),
                        market=market,
                    )
                )
    print(f"gamma: {len(listed)} yes/no markets in {time.time() - t0:.0f}s", flush=True)

    # Conjunctions only outside NegRisk: inside a NegRisk event the outcome
    # questions are mutually exclusive buckets, so a shared prefix is not an implication.
    by_event: dict[str, list[Listed]] = defaultdict(list)
    for row in listed:
        if not row.neg_risk:
            by_event[row.event_id].append(row)
    conjunctions: list[tuple[Listed, Listed]] = []
    for rows in by_event.values():
        if len(rows) < 2:
            continue
        norms = [(norm_question(r.market.question), r) for r in rows]
        for weaker, left in norms:
            for stronger, right in norms:
                if implies_by_conjunction(weaker, stronger):
                    conjunctions.append((right, left))  # strong, weak
    print(f"conjunction pairs {len(conjunctions)}; reward markets {len(reward_by_condition)}", flush=True)

    by_question: dict[str, list[Listed]] = defaultdict(list)
    for row in listed:
        key = norm_question(row.market.question)
        if len(key) < 24:
            continue
        by_question[key].append(row)

    duplicate_groups = []
    for key, rows in by_question.items():
        uniq: dict[str, Listed] = {}
        for row in rows:
            uniq.setdefault(row.market.condition_id, row)
        rows = list(uniq.values())
        if len(rows) < 2:
            continue
        titles = {r.market.group_title.strip().lower() for r in rows}
        if len(titles) > 1:
            continue
        ends = [days_apart(a.market.end_date, b.market.end_date) for a in rows for b in rows]
        ends = [d for d in ends if d is not None]
        same_deadline = not ends or max(ends) <= 3.0
        duplicate_groups.append((key, rows, same_deadline))

    same_deadline = [g for g in duplicate_groups if g[2]]
    drifted = [g for g in duplicate_groups if not g[2]]

    # Cross-event ladders: only non-neg-risk markets, same rule as detect_ladders.
    # detect_ladders groups by question template, so one shell event is enough to
    # see rungs that the live bot, which never looks across events, would miss.
    by_event_market: dict[str, str] = {row.market.id: row.event_id for row in listed}
    plain = [row.market for row in listed if not row.neg_risk]
    cross_ladders = []
    for ladder in detect_ladders(shell_event(plain)):
        events = {by_event_market.get(r.market.id) for r in ladder.rungs}
        if len(events) > 1:
            cross_ladders.append(ladder)

    # Book the duplicate listings with the same deadline, plus every cross-event rung.
    tokens = []
    for _, rows, ok in duplicate_groups:
        if not ok:
            continue
        tokens.extend(r.market.yes_token for r in rows)
    for ladder in cross_ladders:
        tokens.extend(r.market.yes_token for r in ladder.rungs)
    for strong, weak in conjunctions:
        tokens.extend((strong.market.yes_token, weak.market.yes_token))
    tokens = list(dict.fromkeys(tokens))
    print(
        f"duplicate groups {len(same_deadline)} same-deadline / {len(drifted)} different-deadline; "
        f"cross-event ladders {len(cross_ladders)}; books {len(tokens)}",
        flush=True,
    )

    async with ClobClient() as clob:
        store = await clob.get_books(tokens)
    print(f"books back {len(store)}", flush=True)

    priced = []
    for key, rows, ok in same_deadline:
        best = None
        for i in range(len(rows)):
            for j in range(i + 1, len(rows)):
                edge = pair_edge(store, rows[i], rows[j])
                if edge and (best is None or edge["profit"] > best["profit"]):
                    best = edge
        if best is None:
            continue
        priced.append({
            "question": rows[0].market.question,
            "listings": len(rows),
            "events": [r.event_title for r in rows],
            "slugs": [r.event_slug for r in rows],
            **{k: round(best[k], 4) if isinstance(best[k], float) else best[k] for k in ("edge", "shares", "profit")},
        })
    priced.sort(key=lambda r: -r["profit"])

    cross_priced = []
    for ladder in cross_ladders:
        best = None
        for i, strong in enumerate(ladder.rungs):
            book_s = store.get(strong.market.yes_token)
            if book_s is None or not book_s.bids:
                continue
            no_price = 1.0 - book_s.bids[0].price
            c_no = no_price + fee_per_share(strong.market.fee, no_price)
            for weak in ladder.rungs[i + 1:]:
                book_w = store.get(weak.market.yes_token)
                if book_w is None or not book_w.asks:
                    continue
                ask = book_w.asks[0].price
                edge = 1.0 - c_no - ask - fee_per_share(weak.market.fee, ask)
                shares = min(book_s.bids[0].size, book_w.asks[0].size)
                row = {
                    "edge": round(edge, 4),
                    "shares": round(shares, 2),
                    "profit": round(edge * shares, 4),
                    "strong": strong.market.question,
                    "weak": weak.market.question,
                    "strong_event": by_event_market.get(strong.market.id),
                    "weak_event": by_event_market.get(weak.market.id),
                }
                if best is None or row["profit"] > best["profit"]:
                    best = row
        if best:
            cross_priced.append(best)
    cross_priced.sort(key=lambda r: -r["profit"])

    conj_priced = []
    for strong, weak in conjunctions:
        book_s = store.get(strong.market.yes_token)
        book_w = store.get(weak.market.yes_token)
        if book_s is None or book_w is None or not book_s.bids or not book_w.asks:
            continue
        no_price = 1.0 - book_s.bids[0].price
        edge = 1.0 - (no_price + fee_per_share(strong.market.fee, no_price)) - (
            book_w.asks[0].price + fee_per_share(weak.market.fee, book_w.asks[0].price)
        )
        shares = min(book_s.bids[0].size, book_w.asks[0].size)
        conj_priced.append({
            "edge": round(edge, 4),
            "shares": round(shares, 2),
            "profit": round(edge * shares, 4),
            "strong": strong.market.question,
            "weak": weak.market.question,
            "event": strong.event_title,
        })
    conj_priced.sort(key=lambda r: -r["profit"])

    def summarize(rows: list[dict], edge_key: str = "edge") -> dict:
        pos = [r for r in rows if r[edge_key] > 0]
        tradable = [r for r in pos if r["profit"] >= 0.25 and r["shares"] >= 5]
        return {
            "priced": len(rows),
            "positive_edge": len(pos),
            "profit_at_least_25c": len(tradable),
            "sum_positive_profit": round(sum(r["profit"] for r in pos), 2),
            "best": rows[:15],
        }

    payload = {
        "scanned_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "markets": len(listed),
        "duplicate_question_groups_same_deadline": len(same_deadline),
        "duplicate_question_groups_different_deadline": len(drifted),
        "cross_event_ladders": len(cross_ladders),
        "identical_question": summarize(priced),
        "cross_event_ladder": summarize(cross_priced),
        "conjunction_outside_negrisk": summarize(conj_priced),
        "liquidity_rewards": {
            "markets_with_daily_rate": len(reward_by_condition),
            "sum_daily_rate_usd": round(sum(reward_by_condition.values()), 2),
            "holding_rewards_enabled_markets": holding_markets,
        },
        "different_deadline_examples": [
            {"question": rows[0].market.question, "events": [r.event_title for r in rows]}
            for _, rows, _ in drifted[:12]
        ],
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    print(json.dumps({k: payload[k] for k in payload if k not in ("identical_question", "cross_event_ladder", "different_deadline_examples")}, indent=2))
    print("identical", json.dumps(payload["identical_question"]["best"][:5], ensure_ascii=False, indent=2))
    print("cross", json.dumps(payload["cross_event_ladder"]["best"][:5], ensure_ascii=False, indent=2))
    print(f"wrote {OUT}")


if __name__ == "__main__":
    asyncio.run(main())
