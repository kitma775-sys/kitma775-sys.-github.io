"""Cross-event same-oracle partition & implication arbitrage (``twin_arb``).

Covers 4 boundary-safe cross-event families where single-event scanners are blind:

1. **Upper Partition Dollar (``twin_upper``)**:
   * ``NO("above K") + YES(all brackets [lo, hi) with lo >= K)``
   * Pays ``$1.00`` for ``P != K`` and ``$2.00`` at exact boundary ``P == K``.
2. **Lower Overlapping Partition Dollar (``twin_lower``)**:
   * ``YES("above K_a") + YES(all brackets [lo, hi) with hi <= K_b)`` where ``K_b > K_a``
     is the next bracket boundary above ``K_a``.
   * Pays ``$1.00`` for ``P <= K_a`` (even at ``P == K_a``) and ``P >= K_b`` (even at ``P == K_b``),
     and pays **``$2.00`` across the entire ``(K_a, K_b)`` price band**!
3. **Same-Oracle Close -> Hit Implication (``cross_close_hit`` / ``cross_range_dip``)**:
   * On identical Binance Spot pair & contained ET date ``D in [W_start, W_end]``:
     - ``Close_D > K_c => max_{t in W} High_t >= K_h`` (``K_c >= K_h``):
       ``NO("above K_c on D") + YES("hit ↑ K_h in W") >= $1.00``
     - ``Close_D < K_c => min_{t in W} Low_t <= K_l`` (``K_c <= K_l``):
       ``NO("< K_c on D") + YES("hit ↓ K_l in W") >= $1.00``
4. **Same-Oracle Nested Period Hit (``cross_hit_period``)**:
   * On identical Binance Spot pair & strict window containment ``W_1 subset W_2``:
     ``NO("hit in W_1") + YES("hit in W_2") >= $1.00``.
"""

from __future__ import annotations

import calendar
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from polyarb.config import TwinConfig
from polyarb.data.binance import ET_ZONE
from polyarb.models import Event, Market, Opportunity, Outcome, Settlement
from polyarb.sizing import LegBook, size_basket, unit_cost
from polyarb.strategies.base import Books, Target, buy_leg, utc

_STOCK_RANGE_RE = re.compile(r"^(.+?)\s+closes\s+week\s+of\s+([A-Za-z]+)\s+(\d+)\s+at\b", re.I)
_STOCK_ABOVE_RE = re.compile(r"^Will\s+(.+?)\s+finish\s+week\s+of\s+([A-Za-z]+)\s+(\d+)\s+above", re.I)
_CRYPTO_RANGE_RE = re.compile(r"^([A-Za-z0-9]+)\s+price\s+on\s+([A-Za-z]+\s+\d+)\??$", re.I)
_CRYPTO_ABOVE_RE = re.compile(r"^(?:Will\s+)?([A-Za-z0-9]+)\s+(?:be\s+)?above\s+___\s+on\s+([A-Za-z]+\s+\d+)\??$", re.I)
_CRYPTO_HIT_RE = re.compile(r"^What\s+price\s+will\s+([A-Za-z0-9]+)\s+hit\s+(.+?)\??$", re.I)

_TICKER_RE = re.compile(r"\(([A-Z0-9.]+)\)")
_NUM_RE = re.compile(r"([\d,]+(?:\.\d+)?)")
_LT_RE = re.compile(r"^[<≤]\s*(\d+(?:\.\d+)?)$")
_LT_WORDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*or\s*(?:below|less|under|lower)$", re.I)
_GT_RE = re.compile(r"^[>≥]\s*(\d+(?:\.\d+)?)$")
_GT_WORDS_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:\+|or\s*(?:above|more|higher|greater))$", re.I)
_SPAN_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*[-–]\s*(\d+(?:\.\d+)?)$")

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3,
    "april": 4, "apr": 4, "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7,
    "august": 8, "aug": 8, "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10, "november": 11, "nov": 11, "december": 12, "dec": 12,
}

OPEN_HIGH = 1e18
OPEN_LOW = -1e18


@dataclass(frozen=True, slots=True)
class TwinSlice:
    kind: str
    strike: float
    primary_event: Event
    secondary_event: Event
    legs: tuple[tuple[Market, Outcome], ...]
    label: str = ""

    @property
    def range_event(self) -> Event:
        return self.primary_event

    @property
    def above_event(self) -> Event:
        return self.secondary_event

    @property
    def leg_specs(self) -> list[tuple[Market, Outcome]]:
        return list(self.legs)


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


def _infer_year(month: int, ref_end: datetime | None) -> int:
    if ref_end is not None:
        et_end = ref_end.astimezone(ET_ZONE)
        if et_end.month == 1 and month == 12:
            return et_end.year - 1
        return et_end.year
    return datetime.now(ET_ZONE).year


def _parse_et_window(raw_period: str, ref_end: datetime | None) -> tuple[date, date] | None:
    s = raw_period.strip().lower().rstrip("?")
    if m := re.match(r"^(?:on\s+)?([a-z]+)\s+(\d{1,2})(?:,\s*(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(3)) if m.group(3) else _infer_year(mon, ref_end)
        d = date(yr, mon, int(m.group(2)))
        return (d, d)
    if m := re.match(r"^([a-z]+)\s+(\d{1,2})\s*-\s*(\d{1,2})(?:,\s*(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(4)) if m.group(4) else _infer_year(mon, ref_end)
        return (date(yr, mon, int(m.group(2))), date(yr, mon, int(m.group(3))))
    if m := re.match(r"^in\s+([a-z]+)(?:\s+(20\d\d))?$", s):
        mon = _MONTHS.get(m.group(1))
        if not mon:
            return None
        yr = int(m.group(2)) if m.group(2) else _infer_year(mon, ref_end)
        return (date(yr, mon, 1), date(yr, mon, calendar.monthrange(yr, mon)[1]))
    return None


def _binance_pair_key(ev: Event, asset_fallback: str) -> str | None:
    sig = ev.oracle_sig or ""
    if sig and not sig.startswith("binance_"):
        return None
    if ":" in sig:
        return sig.split(":", 1)[1].lower()
    return f"{asset_fallback.lower()}_usdt"


def _compatible_oracles(r_ev: Event, a_ev: Event, family: str) -> bool:
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


def _contiguous_lower_overlapping(brackets: list[tuple[float, float, Market]], strike: float) -> tuple[float, list[Market]] | None:
    """Find contiguous lower brackets ``[(-inf, K_1), ..., [strike, K_next)]`` covering ``(-inf, K_next)``."""
    pivot = next(((lo, hi, m) for lo, hi, m in brackets if abs(lo - strike) <= 1e-6 and hi < OPEN_HIGH * 0.5), None)
    if pivot is None:
        return None
    k_next = pivot[1]
    lower = [(lo, hi, m) for lo, hi, m in brackets if hi <= k_next + 1e-9]
    if not lower or lower[0][0] > OPEN_LOW * 0.5 or abs(lower[-1][1] - k_next) > 1e-6:
        return None
    for idx in range(len(lower) - 1):
        if abs(lower[idx][1] - lower[idx + 1][0]) > 1e-6:
            return None
    return k_next, [m for _, _, m in lower]


def pair_twin_slices(events: Iterable[Event], max_legs: int = 5) -> list[TwinSlice]:
    range_map: dict[tuple[str, str, str], Event] = {}
    above_map: dict[tuple[str, str, str], Event] = {}
    crypto_day_above: list[tuple[str, tuple[date, date], Event]] = []
    crypto_day_range: list[tuple[str, tuple[date, date], Event]] = []
    crypto_hit: list[tuple[str, tuple[date, date], Event]] = []

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
                pk = _binance_pair_key(ev, m.group(1))
                w = _parse_et_window(m.group(2), ev.end_date)
                if pk and w:
                    crypto_day_range.append((pk, w, ev))
        elif m := _CRYPTO_ABOVE_RE.match(t):
            md = m.group(2).split()
            if len(md) == 2 and md[1].isdigit():
                above_map[("crypto_day", m.group(1).lower(), _norm_month_day(md[0], md[1]))] = ev
                pk = _binance_pair_key(ev, m.group(1))
                w = _parse_et_window(m.group(2), ev.end_date)
                if pk and w:
                    crypto_day_above.append((pk, w, ev))
        elif m := _CRYPTO_HIT_RE.match(t):
            pk = _binance_pair_key(ev, m.group(1))
            w = _parse_et_window(m.group(2), ev.end_date)
            if pk and w:
                crypto_hit.append((pk, w, ev))

    slices: list[TwinSlice] = []

    # 1 & 2. Range <-> Above Upper & Lower Overlapping Partitions
    for key in sorted(set(range_map) & set(above_map)):
        r_ev, a_ev = range_map[key], above_map[key]
        if not r_ev.neg_risk or r_ev.neg_risk_augmented or not _compatible_oracles(r_ev, a_ev, key[0]):
            continue
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
            if upper is not None and len(upper) + 1 <= max_legs:
                slices.append(
                    TwinSlice(
                        kind="twin_upper",
                        strike=strike,
                        primary_event=r_ev,
                        secondary_event=a_ev,
                        legs=((m_abv, Outcome.NO), *((um, Outcome.YES) for um in upper)),
                        label=f"{r_ev.title} ⇄ >{strike:g}",
                    )
                )
            lower_res = _contiguous_lower_overlapping(brackets, strike)
            if lower_res is not None and len(lower_res[1]) + 1 <= max_legs:
                k_next, lower_mkts = lower_res
                slices.append(
                    TwinSlice(
                        kind="twin_lower",
                        strike=strike,
                        primary_event=r_ev,
                        secondary_event=a_ev,
                        legs=((m_abv, Outcome.YES), *((lm, Outcome.YES) for lm in lower_mkts)),
                        label=f"{r_ev.title} ⇄ <{k_next:g}∪>{strike:g}",
                    )
                )

    # Pre-index hit barriers per event
    def _hit_rungs(ev: Event) -> tuple[list[tuple[float, Market]], list[tuple[float, Market]]]:
        ups: list[tuple[float, Market]] = []
        dns: list[tuple[float, Market]] = []
        for m in ev.tradeable_markets:
            if not m.is_yes_no or not m.end_date or m.creation_scoped:
                continue
            v = _parse_num(m.group_title or m.question)
            if v is None:
                continue
            q = m.question.lower()
            if "reach" in q or "↑" in m.group_title or "(high)" in q:
                ups.append((v, m))
            elif "dip" in q or "↓" in m.group_title or "(low)" in q:
                dns.append((v, m))
        ups.sort(key=lambda x: x[0], reverse=True)  # highest barrier first
        dns.sort(key=lambda x: x[0])               # lowest dip first
        return ups, dns

    hit_indexed = [(pk, w, ev, *_hit_rungs(ev)) for pk, w, ev in crypto_hit]

    # 3A. Daily Close > Kc => Hit High >= Kh (on contained period W, top 2 tightest Kh <= Kc)
    for pk, (d0, d1), a_ev in crypto_day_above:
        for pk2, (w0, w1), h_ev, ups, _ in hit_indexed:
            if pk != pk2 or not (w0 <= d0 and d1 <= w1) or not ups:
                continue
            # Require explicit Binance oracle signatures when either event carries oracle metadata
            if (a_ev.oracle_sig or h_ev.oracle_sig) and not (
                (a_ev.oracle_sig or "").startswith("binance_") and (h_ev.oracle_sig or "").startswith("binance_")
            ):
                continue
            for mc in a_ev.tradeable_markets:
                if not mc.is_yes_no or not mc.end_date:
                    continue
                kc = _parse_num(mc.group_title or mc.question)
                if kc is None:
                    continue
                tight_ups = [(kh, mh) for kh, mh in ups if kh <= kc + 1e-9][:2]
                for kh, mh in tight_ups:
                    slices.append(
                        TwinSlice(
                            kind="cross_close_hit",
                            strike=kc,
                            primary_event=a_ev,
                            secondary_event=h_ev,
                            legs=((mc, Outcome.NO), (mh, Outcome.YES)),
                            label=f"{a_ev.title}(>{kc:g}) ⇒ {h_ev.title}(↑{kh:g})",
                        )
                    )

    # 3B. Daily Range < Kc => Hit Dip <= Kl (on contained period W, top 2 tightest Kl >= Kc)
    for pk, (d0, d1), r_ev in crypto_day_range:
        for pk2, (w0, w1), h_ev, _, dns in hit_indexed:
            if pk != pk2 or not (w0 <= d0 and d1 <= w1) or not dns:
                continue
            if (r_ev.oracle_sig or h_ev.oracle_sig) and not (
                (r_ev.oracle_sig or "").startswith("binance_") and (h_ev.oracle_sig or "").startswith("binance_")
            ):
                continue
            for mc in r_ev.tradeable_markets:
                if not mc.is_yes_no or not mc.end_date:
                    continue
                b = parse_range_bracket(mc.group_title)
                if b is None or b[0] > OPEN_LOW * 0.5:
                    continue
                kc = b[1]
                tight_dns = [(kl, ml) for kl, ml in dns if kl >= kc - 1e-9][:2]
                for kl, ml in tight_dns:
                    slices.append(
                        TwinSlice(
                            kind="cross_range_dip",
                            strike=kc,
                            primary_event=r_ev,
                            secondary_event=h_ev,
                            legs=((mc, Outcome.NO), (ml, Outcome.YES)),
                            label=f"{r_ev.title}(<{kc:g}) ⇒ {h_ev.title}(↓{kl:g})",
                        )
                    )

    # 4. Shorter Hit Period W1 subset of Longer Hit Period W2 (e.g. Daily -> Weekly -> Monthly)
    for pk1, (s0, s1), ev1, ups1, dns1 in hit_indexed:
        for pk2, (l0, l1), ev2, ups2, dns2 in hit_indexed:
            if pk1 != pk2 or ev1.id == ev2.id:
                continue
            if not (l0 <= s0 and s1 <= l1 and (s0 > l0 or s1 < l1)):
                continue
            if (ev1.oracle_sig or ev2.oracle_sig) and not (
                (ev1.oracle_sig or "").startswith("binance_") and (ev2.oracle_sig or "").startswith("binance_")
            ):
                continue
            for k1, m1 in ups1:
                for k2, m2 in [(k, m) for k, m in ups2 if k <= k1 + 1e-9][:2]:
                    slices.append(
                        TwinSlice(
                            kind="cross_hit_period",
                            strike=k1,
                            primary_event=ev1,
                            secondary_event=ev2,
                            legs=((m1, Outcome.NO), (m2, Outcome.YES)),
                            label=f"{ev1.title}(↑{k1:g}) ⇒ {ev2.title}(↑{k2:g})",
                        )
                    )
            for k1, m1 in dns1:
                for k2, m2 in [(k, m) for k, m in dns2 if k >= k1 - 1e-9][:2]:
                    slices.append(
                        TwinSlice(
                            kind="cross_hit_period",
                            strike=k1,
                            primary_event=ev1,
                            secondary_event=ev2,
                            legs=((m1, Outcome.NO), (m2, Outcome.YES)),
                            label=f"{ev1.title}(↓{k1:g}) ⇒ {ev2.title}(↓{k2:g})",
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
        seen_keys: set[str] = set()
        for sl in pair_twin_slices(events, max_legs=self.cfg.max_legs):
            tokens = tuple(dict.fromkeys(m.yes_token for m, _ in sl.legs))
            leg_sig = "-".join(f"{m.id}:{o.value[0]}" for m, o in sl.legs)
            key = f"twin:{sl.kind}:{sl.primary_event.id}:{sl.secondary_event.id}:{leg_sig}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            out.append(Target(key, self.name, sl.primary_event, tokens, sl))
        return out

    def top_edge(self, target: Target, books: Books) -> float | None:
        sl: TwinSlice = target.payload
        total = 0.0
        for m, out in sl.legs:
            bm = books.get(m.yes_token)
            if bm is None:
                return None
            levels = bm.asks if out is Outcome.YES else bm.no_asks()
            c = unit_cost(levels, m.fee)
            if c is None:
                return None
            total += c
        return 1.0 - total

    def evaluate(self, target: Target, books: Books, now: float, max_cost: float | None = None) -> list[Opportunity]:
        cfg = self.cfg
        cap = self.max_trade_usd if max_cost is None else min(self.max_trade_usd, max_cost)
        if cap <= 0:
            return []
        sl: TwinSlice = target.payload
        leg_books: list[LegBook] = []
        for m, out in sl.legs:
            bm = books.get(m.yes_token)
            if bm is None:
                return []
            levels = bm.asks if out is Outcome.YES else bm.no_asks()
            if not levels:
                return []
            leg_books.append(LegBook(levels, m.fee, m.min_size))

        if sum(unit_cost(lb.levels, lb.fee) or 1.0 for lb in leg_books) > 1.0 - cfg.min_unit_edge:
            return []

        all_mkts = [m for m, _ in sl.legs]
        resolves_at = max(m.end_date for m in all_mkts if m.end_date) + timedelta(days=cfg.resolution_lag_days)
        now_dt = utc(now)
        if (resolves_at - now_dt).total_seconds() / 86400.0 > cfg.max_days:
            return []

        fill = size_basket(leg_books, 1.0, min_unit_edge=cfg.min_unit_edge, max_cost=cap)
        if fill is None:
            return []

        legs = [buy_leg(m, out, f) for (m, out), f in zip(sl.legs, fill.fills)]
        opp = Opportunity(
            strategy=self.name,
            key=target.key,
            event_id=sl.primary_event.id,
            title=sl.label or f"{sl.primary_event.title} ⇄ >{sl.strike:g}",
            legs=legs,
            shares=fill.shares,
            cost=fill.cost,
            payout_floor=fill.payout,
            settlement=Settlement.RESOLUTION,
            risk="low",
            resolves_at=resolves_at,
            detected_at=now,
            meta={
                "kind": sl.kind,
                "strike": sl.strike,
                "legs": len(legs),
                "fast_turnover": (resolves_at - now_dt).total_seconds() <= 3 * 86400,
                "independent_legs": True,
                "event_ids": [sl.primary_event.id, sl.secondary_event.id],
                "condition_ids": [m.condition_id for m in all_mkts if m.condition_id],
            },
        )
        apr = opp.apr(now_dt)
        if opp.profit < cfg.min_profit_usd or opp.edge < cfg.min_edge or (apr is not None and apr < cfg.min_apr):
            return []
        return [opp]
