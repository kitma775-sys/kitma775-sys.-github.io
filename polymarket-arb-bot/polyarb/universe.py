from __future__ import annotations

import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from polyarb.config import Config, UniverseConfig
from polyarb.data.metar import MetarFeed
from polyarb.models import Event
from polyarb.strategies.base import Strategy, Target
from polyarb.strategies.ladder import LadderArb
from polyarb.strategies.metar import MetarLock
from polyarb.strategies.negrisk import NegRiskConvert, NegRiskYesBasket
from polyarb.strategies.twin import TwinPartitionArb


def build_all_strategies(cfg: Config, metar_feed: MetarFeed | None = None) -> list[Strategy]:
    cap = cfg.risk.max_trade_usd
    return [
        NegRiskConvert(cfg.convert, cap),
        MetarLock(cfg.metar, metar_feed, cap),
        TwinPartitionArb(cfg.twin, cap),
        NegRiskYesBasket(cfg.yes_basket, cap),
        LadderArb(cfg.ladder, cap),
    ]


def build_strategies(cfg: Config, metar_feed: MetarFeed | None = None) -> list[Strategy]:
    return [s for s in build_all_strategies(cfg, metar_feed=metar_feed) if s.enabled]


def filter_events(events: Iterable[Event], ucfg: UniverseConfig) -> list[Event]:
    excluded = set(ucfg.exclude_tags)
    out = []
    for ev in events:
        if excluded and excluded.intersection(ev.tags):
            continue
        tradeable = ev.tradeable_markets
        if len(tradeable) < ucfg.min_markets:
            continue
        # Delayed matching (live sports) turns a multi-leg basket into a sequence of blind bets.
        if any(m.seconds_delay > ucfg.max_seconds_delay for m in tradeable):
            continue
        out.append(ev)
    return out


@dataclass
class Universe:
    events: list[Event]
    targets: list[Target]
    by_token: dict[str, list[Target]] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)

    @property
    def tokens(self) -> list[str]:
        return list(self.by_token)

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for t in self.targets:
            out[t.strategy] += 1
        return dict(out)


def build_universe(events: Iterable[Event], strategies: Iterable[Strategy], ucfg: UniverseConfig) -> Universe:
    kept = filter_events(events, ucfg)
    targets: list[Target] = []
    for strategy in strategies:
        targets.extend(strategy.targets(kept))
    by_token: dict[str, list[Target]] = defaultdict(list)
    for t in targets:
        for token in t.tokens:
            by_token[token].append(t)
    return Universe(kept, targets, dict(by_token))
