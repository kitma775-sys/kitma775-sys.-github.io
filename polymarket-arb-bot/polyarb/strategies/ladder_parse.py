"""Detect monotone ("implication") ladders inside a Polymarket event.

A ladder is a set of binary markets whose YES conditions are nested:

* ``UP``      - "above / reach / at least X": YES(X_high) implies YES(X_low)
* ``DOWN``    - "below / dip to / at most X":  YES(X_low)  implies YES(X_high)
* ``BY_DATE`` - "happens by D":                YES(D_early) implies YES(D_late)

Rungs are ordered strongest (hardest) first, so rung i implies rung j for i < j.
Detection is deliberately conservative: questions must be identical except for
the single varying threshold/date, ranges and "exactly" buckets are rejected, and
NegRisk events (mutually exclusive by construction) are never treated as ladders.
"""

from __future__ import annotations

import enum
import hashlib
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from itertools import groupby

from polyarb.models import Event, Market


class Direction(str, enum.Enum):
    UP = "up"
    DOWN = "down"
    BY_DATE = "by_date"


@dataclass(frozen=True, slots=True)
class Rung:
    market: Market
    value: float
    label: str


@dataclass(frozen=True, slots=True)
class Ladder:
    event: Event
    direction: Direction
    template: str
    rungs: tuple[Rung, ...]

    @property
    def key(self) -> str:
        digest = hashlib.sha1(self.template.encode()).hexdigest()[:8]
        return f"{self.event.id}:{self.direction.value}:{digest}"

    @property
    def tokens(self) -> list[str]:
        return [r.market.yes_token for r in self.rungs]


_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_MONTH = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sept?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_DATE_RE = re.compile(rf"\b{_MONTH}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(20\d\d))?", re.I)
_MONTH_YEAR_RE = re.compile(rf"\b{_MONTH}\.?,?\s+(20\d\d)\b", re.I)
_YEAR_RE = re.compile(r"\b(20\d\d)\b")
_NUM_RE = re.compile(
    r"(?<![\w.])(?P<sign>[-−])?\$?(?P<int>\d{1,3}(?:,\d{3})+|\d+)(?P<frac>\.\d+)?"
    r"(?:\s*(?P<suf>k|m|b|t|bn|mn|thousand|million|billion|trillion)(?![a-z]))?",
    re.I,
)
_RANGE_RE = re.compile(r"\d\s*(?:[-–—]|to)\s*[$−-]?\d|\bbetween\b", re.I)
_EXCLUDE_RE = re.compile(r"\b(exactly|between|range|closest|nearest|the most|the fewest|rank(?:ed)?)\b", re.I)
_SUFFIX = {
    "k": 1e3, "thousand": 1e3, "m": 1e6, "mn": 1e6, "million": 1e6,
    "b": 1e9, "bn": 1e9, "billion": 1e9, "t": 1e12, "trillion": 1e12,
}
_UP_BEFORE = re.compile(
    r"(above|over|more than|greater than|at least|exceed(?:s|ing)?|higher than|reach(?:es)?|surpass(?:es)?|≥|>|↑)\s*\$?\s*$",
    re.I,
)
_DOWN_BEFORE = re.compile(
    r"(below|under|less than|fewer than|lower than|at most|dip(?:s)? to|drop(?:s)? to|fall(?:s)? to|"
    r"sink(?:s)? to|≤|<|↓)\s*\$?\s*$",
    re.I,
)
_UP_AFTER = re.compile(r"^\s*(?:[kmbt%°]|bn|mn|bps|million|billion|trillion|thousand)?\s*(\+|or more|or higher|or above|or greater|and above|and up)", re.I)
_DOWN_AFTER = re.compile(r"^\s*(?:[kmbt%°]|bn|mn|bps|million|billion|trillion|thousand)?\s*(or less|or lower|or below|or fewer|and below|and under)", re.I)
_BY_BEFORE = re.compile(r"\b(by|before|until|prior to|no later than|by the end of|by end of)\s*(?:the\s+)?$", re.I)
_ON_BEFORE = re.compile(r"\b(on|in|during|after)\s*(?:the\s+)?$", re.I)


@dataclass(frozen=True, slots=True)
class _Num:
    value: float
    start: int
    end: int


@dataclass(frozen=True, slots=True)
class _Date:
    value: date
    start: int
    end: int


def _numbers(text: str) -> list[_Num]:
    out = []
    for m in _NUM_RE.finditer(text):
        raw = m.group("int").replace(",", "") + (m.group("frac") or "")
        try:
            val = float(raw)
        except ValueError:
            continue
        suf = (m.group("suf") or "").lower()
        val *= _SUFFIX.get(suf, 1.0)
        if m.group("sign"):
            val = -val
        out.append(_Num(val, m.start(), m.end()))
    return out


def _resolve_year(month: int, day: int, year: int | None, ref: datetime | None) -> date | None:
    try:
        if year:
            return date(year, month, min(day, monthrange(year, month)[1]))
        if ref is None:
            return None
        candidates = []
        for y in (ref.year - 1, ref.year, ref.year + 1):
            d = date(y, month, min(day, monthrange(y, month)[1]))
            candidates.append((abs((d - ref.date()).days), d))
        return min(candidates)[1]
    except ValueError:
        return None


def _dates(text: str, ref: datetime | None) -> list[_Date]:
    out: list[_Date] = []
    taken: list[tuple[int, int]] = []
    for m in _DATE_RE.finditer(text):
        month = _MONTHS[m.group(1)[:3].lower()]
        d = _resolve_year(month, int(m.group(2)), int(m.group(3)) if m.group(3) else None, ref)
        if d:
            out.append(_Date(d, m.start(), m.end()))
            taken.append((m.start(), m.end()))
    for m in _MONTH_YEAR_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        month = _MONTHS[m.group(1)[:3].lower()]
        year = int(m.group(2))
        out.append(_Date(date(year, month, monthrange(year, month)[1]), m.start(), m.end()))
        taken.append((m.start(), m.end()))
    for m in _YEAR_RE.finditer(text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        year = int(m.group(1))
        before = text[max(0, m.start() - 12):m.start()].lower()
        # "before 2027" means the deadline is the last day of 2026
        d = date(year - 1, 12, 31) if before.rstrip().endswith("before") else date(year, 12, 31)
        out.append(_Date(d, m.start(), m.end()))
    out.sort(key=lambda x: x.start)
    return out


def question_template(question: str) -> str:
    t = question.lower().replace("−", "-")
    t = _DATE_RE.sub("@", t)
    t = _MONTH_YEAR_RE.sub("@", t)
    t = _NUM_RE.sub("#", t)
    return re.sub(r"\s+", " ", t).strip()


def _direction_near(text: str, start: int, end: int, label: str) -> Direction | None:
    before = text[max(0, start - 40):start]
    after = text[end:end + 30]
    lab = label.strip()
    if lab.startswith(("↑", ">", "≥")) or re.search(r"\d\s*\+\s*$", lab):
        return Direction.UP
    if lab.startswith(("↓", "<", "≤")):
        return Direction.DOWN
    if _UP_BEFORE.search(before) or _UP_AFTER.search(after):
        return Direction.UP
    if _DOWN_BEFORE.search(before) or _DOWN_AFTER.search(after):
        return Direction.DOWN
    return None


def _varying_index(rows: list[list[float]]) -> int | None:
    if len(rows) < 2 or len({len(r) for r in rows}) != 1:
        return None
    varying = [i for i in range(len(rows[0])) if len({r[i] for r in rows}) > 1]
    return varying[0] if len(varying) == 1 else None


def detect_ladders(event: Event, min_rungs: int = 2) -> list[Ladder]:
    if event.neg_risk:
        return []
    markets = [m for m in event.tradeable_markets if m.is_yes_no and m.question]
    if len(markets) < min_rungs:
        return []
    ladders: list[Ladder] = []
    keyed = sorted(markets, key=lambda m: question_template(m.question))
    for template, group_iter in groupby(keyed, key=lambda m: question_template(m.question)):
        group = list(group_iter)
        if len(group) < min_rungs or _EXCLUDE_RE.search(template):
            continue
        if any(_RANGE_RE.search(m.question) or _RANGE_RE.search(m.group_title) for m in group):
            continue
        ladders.extend(_numeric_ladders(event, template, group, min_rungs))
        ladders.extend(_date_ladders(event, template, group, min_rungs))
    return ladders


def _numeric_ladders(event: Event, template: str, group: list[Market], min_rungs: int) -> list[Ladder]:
    if "#" not in template:
        return []
    # "March 31" vs "May 31" differ only in a word, which the number scan cannot see.
    if len({tuple(m.question[d.start:d.end].lower() for d in _dates(m.question, m.end_date)) for m in group}) > 1:
        return []
    parsed = [_numbers(m.question.replace("−", "-")) for m in group]
    # Dates are part of the numeric scan ("October 17"), so they must not vary inside a numeric ladder.
    idx = _varying_index([[n.value for n in nums] for nums in parsed])
    if idx is None:
        return []
    by_dir: dict[Direction, list[Rung]] = {}
    for m, nums in zip(group, parsed):
        n = nums[idx]
        if _date_overlaps(m.question, n.start, n.end, m.end_date):
            return []
        direction = _direction_near(m.question, n.start, n.end, m.group_title)
        if direction is None:
            continue
        by_dir.setdefault(direction, []).append(Rung(m, n.value, m.group_title or _snippet(m.question, n)))
    out = []
    for direction, rungs in by_dir.items():
        ladder = _finish(event, direction, template, rungs, min_rungs)
        if ladder:
            out.append(ladder)
    return out


def _date_ladders(event: Event, template: str, group: list[Market], min_rungs: int) -> list[Ladder]:
    if "@" not in template and not _YEAR_RE.search(group[0].question):
        return []
    parsed = [_dates(m.question, m.end_date) for m in group]
    idx = _varying_index([[d.value.toordinal() for d in ds] for ds in parsed])
    if idx is None:
        return []
    thresholds = {
        tuple(n.value for n in _numbers(m.question.replace("−", "-")) if not any(d.start <= n.start < d.end for d in ds))
        for m, ds in zip(group, parsed)
    }
    if len(thresholds) > 1:
        return []
    rungs = []
    for m, ds in zip(group, parsed):
        d = ds[idx]
        if not _BY_BEFORE.search(m.question[max(0, d.start - 25):d.start]):
            return []
        if _ON_BEFORE.search(m.question[max(0, d.start - 10):d.start]):
            return []
        rungs.append(Rung(m, float(d.value.toordinal()), m.group_title or d.value.isoformat()))
    ladder = _finish(event, Direction.BY_DATE, template, rungs, min_rungs)
    if ladder is None:
        return []
    # Deadlines and market end dates must agree on ordering; otherwise the parse is suspect.
    ends = [r.market.end_date for r in ladder.rungs]
    if all(ends):
        for a, b in zip(ends, ends[1:]):
            if b < a - timedelta(days=2):
                return []
    return [ladder]


def _finish(event: Event, direction: Direction, template: str, rungs: list[Rung], min_rungs: int) -> Ladder | None:
    values = [r.value for r in rungs]
    if len(set(values)) != len(values):
        dupes = {v for v in values if values.count(v) > 1}
        rungs = [r for r in rungs if r.value not in dupes]
    if len(rungs) < min_rungs:
        return None
    reverse = direction is Direction.UP
    ordered = tuple(sorted(rungs, key=lambda r: r.value, reverse=reverse))
    return Ladder(event, direction, template, ordered)


def _date_overlaps(question: str, start: int, end: int, ref: datetime | None) -> bool:
    return any(d.start <= start < d.end for d in _dates(question, ref))


def _snippet(question: str, n: _Num) -> str:
    return question[max(0, n.start - 12):n.end + 6].strip()
