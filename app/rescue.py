"""Rescue a one-sided complement fill.

Maker bids on both YES and NO get picked off on the dying leg of a 15m
up/down market (adverse selection). Holding the leftover to expiry at a $0
mark is how the paper book went deeply negative.

After one leg fills, compare:
- hedge: take the other ask, merge, lock in (usually small) pair PnL
- dump: sell the filled inventory at the bid
- hold: keep it marked $0 (almost never best if a bid exists)
"""

from __future__ import annotations

from dataclasses import dataclass

from app.fees import taker_fee
from app.hunter import Level, walk
from app.paper_sim import seconds_left


@dataclass(frozen=True)
class RescuePlan:
    action: str  # hedge | dump | hold
    price: float
    fees: float
    cash_out: float
    pnl: float
    reason: str


def plan_rescue(
    *,
    filled_px: float,
    shares: float,
    other_asks: list[Level],
    filled_bids: list[Level],
    fee_rate: float,
) -> RescuePlan:
    shares = float(shares)
    filled_px = float(filled_px)
    filled_cost = round(shares * filled_px, 6)
    hold = RescuePlan("hold", 0.0, 0.0, 0.0, round(-filled_cost, 6), "hold_mark0")
    candidates: list[RescuePlan] = []

    filled_n, ask_px = walk(other_asks, shares, asks=True)
    if filled_n + 1e-9 >= shares and ask_px > 0:
        fees = taker_fee(shares, ask_px, fee_rate)
        cost = round(shares * ask_px + fees, 6)
        pnl = round(shares - filled_cost - cost, 6)
        candidates.append(RescuePlan("hedge", round(ask_px, 4), fees, cost, pnl, "hedge_take"))

    filled_n, bid_px = walk(filled_bids, shares, asks=False)
    if filled_n + 1e-9 >= shares and bid_px > 0:
        fees = taker_fee(shares, bid_px, fee_rate)
        proceeds = round(max(0.0, shares * bid_px - fees), 6)
        pnl = round(proceeds - filled_cost, 6)
        candidates.append(RescuePlan("dump", round(bid_px, 4), fees, proceeds, pnl, "dump_bid"))

    def _key(p: RescuePlan) -> tuple[float, int]:
        rank = {"hedge": 2, "dump": 1, "hold": 0}[p.action]
        return (p.pnl, rank)

    candidates.append(hold)
    return max(candidates, key=_key)


def parse_outcome_prices(raw) -> tuple[float, float] | None:
    prices = raw
    if isinstance(prices, str):
        import json

        try:
            prices = json.loads(prices)
        except json.JSONDecodeError:
            return None
    if not isinstance(prices, (list, tuple)) or len(prices) < 2:
        return None
    try:
        up_p, dn_p = float(prices[0]), float(prices[1])
    except (TypeError, ValueError):
        return None
    if up_p < -1e-9 or dn_p < -1e-9:
        return None
    return up_p, dn_p


def is_redeemable_market(event: dict | None) -> tuple[float, float] | None:
    """Official payout once Gamma shows a resolved 0/1 (or a true 50/50 invalid).

    Crypto 5m books print ~0.50/0.50 the second the window ends, before
    Chainlink posts 0/1. Treating that mid as a payout crystallizes a ~50%
    loss on books that later resolve to $1 — that is what kept tripping the
    $50 paper circuit. Wait for a binary vector, or for UMA to actually
    resolve an invalid 50/50. Never redeem on "clock hit zero and the book
    is still mid".
    """
    if not isinstance(event, dict):
        return None
    market = (event.get("markets") or [{}])[0]
    if not isinstance(market, dict):
        market = {}
    prices = parse_outcome_prices(market.get("outcomePrices"))
    if prices is None:
        return None
    up_p, dn_p = prices
    binary_up = up_p >= 0.99 and dn_p <= 0.01
    binary_dn = dn_p >= 0.99 and up_p <= 0.01
    status = str(market.get("umaResolutionStatus") or event.get("umaResolutionStatus") or "").lower()
    auto = bool(market.get("automaticallyResolved") or event.get("automaticallyResolved"))
    closed = bool(event.get("closed") or market.get("closed"))
    left = seconds_left(market.get("endDate") or event.get("endDate") or event.get("end"))
    ended = left is not None and left <= 0
    posted = closed or ended or status == "resolved" or auto
    if (binary_up or binary_dn) and posted:
        return (1.0, 0.0) if binary_up else (0.0, 1.0)
    if abs(up_p - 0.5) <= 0.001 and abs(dn_p - 0.5) <= 0.001:
        if closed and status == "resolved":
            return (0.5, 0.5)
    return None
