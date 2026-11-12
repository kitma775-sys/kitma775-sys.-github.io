"""Cross-event same-oracle Range Bracket <-> Cumulative Above twin arbitrage.

Why single-event scanners miss this singularity:
    Polymarket lists twin events settling on the exact same underlying price feed
    and timestamp under separate Event IDs:
    * Daily Crypto: ``"Bitcoin price on November 14?"`` (NegRisk brackets, Binance
      1m Close at 12:00 ET, exact boundary goes to higher bracket ``[K_i, K_{i+1})``)
      paired with ``"Bitcoin above ___ on November 14?"`` (cumulative binaries,
      Binance 1m Close at 12:00 ET, strictly ``> K``).
    * Weekly Equities: ``"NVIDIA (NVDA) closes week of Nov 9 at ___?"`` (NegRisk
      brackets, Pyth Friday Close, boundary goes to higher bracket ``[K_i, K_{i+1})``)
      paired with ``"Will NVIDIA (NVDA) finish week of November 9 above___?"``
      (cumulative binaries, Pyth Friday Close, strictly ``> K``).

Zero-tie-risk payoff proof (Upper Partition Dollar at strike K):
    Buy ``NO("above K")`` + ``YES`` on every contiguous upper bracket ``[lo, hi)`` with ``lo >= K``:
    * ``P < K`` : ``NO("above K") = $1``, upper brackets = ``$0``  -> **$1.00**
    * ``P > K`` : ``NO("above K") = $0``, exact one upper bracket = ``$1`` -> **$1.00**
    * ``P = K`` : ``NO("above K") = $1`` AND ``YES("[K, K_next)") = $1`` -> **$2.00**
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable

from polyarb.config import TwinConfig
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_STOCK_RANGE_RE = re.compile(r"^(.+?)\s+closes\s+week\s+of\s+([A-Za-z]+)\s+(\d+)\s+at\b", re.I)
_STOCK_ABOVE_RE = re.compile(r"^Will\s+(.+?)\s+finish\s+week\s+of\s+([A-Za-z]+)\s+(\d+)\s+above", re.I)
_CRYPTO_RANGE_RE = re.compile(r"^([A-Za-z0-9]+)\s+price\s+on\s+([A-Za-z]+\s+\d+)\??$", re.I)
_CRYPTO_ABOVE_RE = re.compile(r"^(?:Will\s+)?([A-Za-z0-9]+)\s+(?:be\s+)?above\s+___\s+on\s+([A-Za-z]+\s+\d+)\??$", re.I)
_TICKER_RE = re.compile(r"\(([A-Z0-9.]+)\)")
_NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_LT_RE = re.compile(r"^[<≤]\s*(\d+(?:\.\d+)?)$")
_LT_WORDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*or\s*(?:below|less|under|lower)$", re.I)
_GT_RE = re.compile(r"^[>≥]\s*(\d+(?:\.\d+)?)$")
_GT_WORDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:\+|or\s*(?:above|more|higher|greater))$", re.I)
_SPAN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$")

OPEN_HIGH = 1e18
OPEN_LOW = -1e18


@dataclass(frozen=True, slots=True)
class TwinSlice:
    strike: float
    range_event: Event
    above_event: Event
    above_market: Market
    upper_markets: tuple[Market, ...]

    @property
    def leg_specs(self) -> list[tuple[Market, Outcome]]:
        return [(self.above_market, Outcome.NO), *((m, Outcome.YES) for m in self.upper_markets)]


def _norm_stock(raw: str) -> str:
    m = _TICKER_RE.search(raw)
    return m.group(1).lower() if m else re.sub(r"\s+", " ", raw.strip().lower())


def _norm_month_day(month: str, day: str) -> str:
    return f"{month.strip()[:3].lower()}-{int(day)}"


def _parse_num(text: str) -> float | None:
    cleaned = text.replace("$", "")
    m = _NUM_RE.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def parse_range_bracket(label: str) -> tuple[float, float] | None:
    s = label.strip().replace("$", "").replace(",", "")
    if not s:
        return None
    m = _LT_RE.match(s) or _LT_WORDS_RE.match(s)
    if m:
        return (OPEN_LOW, float(m.group(1)))
    m = _GT_RE.match(s) or _GT_WORDS_RE.match(s)
    if m:
        return (float(m.group(1)), OPEN_HIGH)
    m = _SPAN_RE.match(s)
    if m:
        lo, hi = float(m.group(1)), float(m.group(2))
        return (lo, hi) if hi > lo else None
    return None


def _compatible_oracles(r_ev: Event, a_ev: Event, family: str) -> bool:
    """Require identical settlement feed when descriptions carry oracle metadata."""
    if r_ev.oracle_sig or a_ev.oracle_sig:
        if r_ev.oracle_sig != a_ev.oracle_sig:
            return False
        if family == "crypto_day" and not (r_ev.oracle_sig or "").startswith("binance_1200et"):
            return False
        if family == "stock_wk" and r_ev.oracle_sig != "pyth_close":
            return False
    if r_ev.end_date and a_ev.end_date:
        if abs((r_ev.end_date - a_ev.end_date).total_seconds()) > 18 * 3600:
            return False
    return True


def _contiguous_upper(brackets: list[tuple[float, float, Market]], strike: float) -> list[Market] | None:
    upper = [(lo, hi, m) for lo, hi, m in brackets if lo >= strike - 1e-9]
    if not upper or abs(upper[0][0] - strike) > 1e-6 or upper[-1][1] < OPEN_HIGH * 0.5:
        return None
    for idx in range(len(upper) - 1):
        if abs(upper[idx][1] - upper[idx + 1][0]) > 1e-6:
            return None
    return [m for _, _, m in upper]


def pair_twin_slices(events: Iterable[Event], max_legs: int = 5) -> list[TwinSlice]:
    range_map: dict[tuple[str, str, str], Event] = {}
    above_map: dict[tuple[str, str, str], Event] = {}

    for ev in events:
        t = ev.title.strip()
        if m := _STOCK_RANGE_RE.match(t):
            range_map[("stock_wk", _norm_stock(m.group(1)), _norm_month_day(m.group(2), m.group(3)))] = ev
        elif m := _STOCK_ABOVE_RE.match(t):
            above_map[("stock_wk", _norm_stock(m.group(1)), _norm_month_day(m.group(2), m.group(3)))] = ev
        elif m := _CRYPTO_RANGE_RE.match(t):
            md = m.group(2).split()
            if len(md) == 2 and md[1].isdigit():
                range_map[("crypto_day", m.group(1).lower(), _norm_month_day(md[0], md[1]))] = ev
        elif m := _CRYPTO_ABOVE_RE.match(t):
            md = m.group(2).split()
            if len(md) == 2 and md[1].isdigit():
                above_map[("crypto_day", m.group(1).lower(), _norm_month_day(md[0], md[1]))] = ev

    slices: list[TwinSlice] = []
    for key in sorted(set(range_map) & set(above_map)):
        r_ev, a_ev = range_map[key], above_map[key]
        if not r_ev.neg_risk or r_ev.neg_risk_augmented or not _compatible_oracles(r_ev, a_ev, key[0]):
            continue
        # Range event must have no paused-open or already-YES resolved bracket.
        if any(not m.accepting_orders and not m.closed for m in r_ev.markets):
            continue
        if any(m.closed and m.resolved_outcome != "NO" for m in r_ev.markets):
            continue

        brackets: list[tuple[float, float, Market]] = []
        valid_r = True
        for m in r_ev.tradeable_markets:
            b = parse_range_bracket(m.group_title)
            if b is None or not m.is_yes_no or not m.end_date:
                valid_r = False
                break
            brackets.append((b[0], b[1], m))
        if not valid_r or len(brackets) < 2:
            continue
        brackets.sort(key=lambda x: x[0])

        for m_abv in a_ev.tradeable_markets:
            if not m_abv.is_yes_no or not m_abv.end_date:
                continue
            strike = _parse_num(m_abv.group_title or m_abv.question)
            if strike is None:
                continue
            upper = _contiguous_upper(brackets, strike)
            # Total legs = 1 (NO above K) + len(upper) <= max_legs
            if upper is None or len(upper) + 1 > max_legs:
                continue
            slices.append(
                TwinSlice(
                    strike=strike,
                    range_event=r_ev,
                    above_event=a_ev,
                    above_market=m_abv,
                    upper_markets=tuple(upper),
                )
            )
    return slices


class TwinPartitionArb:
    name = "twin_arb"

    def __init__(self, cfg: TwinConfig, max_trade_usd: float = math.inf) -> None:
        self.cfg = cfg
        self.enabled = cfg.enabled
        self.max_trade_usd = max_trade_usd

    def targets(self, events: Iterable[Event]) -> list[Target]:
        out: list[Target] = []
        for sl in pair_twin_slices(events, max_legs=self.cfg.max_legs):
            tokens = (sl.above_market.yes_token, *(m.yes_token for m in sl.upper_markets))
            key = f"twin:{sl.range_event.id}:{sl.above_event.id}:{sl.strike:g}"
            out.append(Target(key, self.name, sl.range_event, tokens, sl))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        sl: TwinSlice = target.payload
        b_abv = books.get(sl.above_market.yes_token)
        if b_abv is None:
            return None
        no_asks = b_abv.no_asks()
        if not no_asks:
            return None
        total = unit_cost(no_asks, sl.above_market.fee) or 0.0
        for m in sl.upper_markets:
            bm = books.get(m.yes_token)
            if bm is None or not bm.asks:
                return None
            total += unit_cost(bm.asks, m.fee) or 0.0
        return 1.0 - total

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        sl: TwinSlice = target.payload
        b_abv = books.get(sl.above_market.yes_token)
        if b_abv is None:
            return []
        no_asks = b_abv.no_asks()
        if not no_asks:
            return []

        leg_books = [LegBook(no_asks, sl.above_market.fee, sl.above_market.min_size)]
        for m in sl.upper_markets:
            bm = books.get(m.yes_token)
            if bm is None or not bm.asks:
                return []
            leg_books.append(LegBook(bm.asks, m.fee, m.min_size))

        if sum(unit_cost(lb.levels, lb.fee) or 1.0 for lb in leg_books) > 1.0 - cfg.min_unit_edge:
            return []

        all_mkts = [sl.above_market, *sl.upper_markets]
        resolves_at = max(m.end_date for m in all_mkts if m.end_date) + timedelta(days=cfg.resolution_lag_days)
        now_dt = utc(now)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
            return []

        fill = size_basket(leg_books, 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
        if fill is None:
            return []

        legs = [
            buy_leg(sl.above_market, Outcome.NO, fill.fills[0]),
            *(buy_leg(m, Outcome.YES, f) for m, f in zip(sl.upper_markets, fill.fills[1:])),
        ]
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=sl.range_event.id,
            title=f"{sl.range_event.title} ⇄ >{sl.strike:g}",
            legs=legs,
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=resolves_at,
            detected_at=now,
            meta={
                "strike": sl.strike,
                "legs": len(legs),
                "event_ids": [sl.range_event.id, sl.above_event.id],
                "condition_ids": [m.condition_id for m in all_mkts if m.condition_id],
            },
        )
        apr = opp.apr(now_dt)
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge or (apr is not None and apr < cfg.min_apr):
            return []
        return [opp]
