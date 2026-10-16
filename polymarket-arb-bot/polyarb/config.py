from __future__ import annotations

import dataclasses
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class ConvertConfig:
    """NegRisk NO-basket bought on the CLOB and converted to pUSD on-chain (no resolution risk)."""

    enabled: bool = True
    min_profit_usd: float = 0.25
    min_edge: float = 0.003
    min_unit_edge: float = 0.0005
    max_legs: int = 12
    gas_usd: float = 0.0


@dataclass
class YesBasketConfig:
    """Buy YES on every outcome of an exhaustive NegRisk event and hold to resolution."""

    enabled: bool = True
    min_profit_usd: float = 1.0
    min_edge: float = 0.01
    min_apr: float = 0.25
    max_days: float = 45.0
    min_unit_edge: float = 0.002
    max_legs: int = 12
    require_catch_all: bool = True
    min_ask_sum: float = 0.85
    resolution_lag_days: float = 1.0


@dataclass
class LadderConfig:
    """Buy YES on the weaker rung and NO on the stronger rung of a monotone ladder."""

    enabled: bool = True
    min_profit_usd: float = 0.5
    min_edge: float = 0.008
    min_apr: float = 0.20
    max_days: float = 90.0
    min_unit_edge: float = 0.002
    directions: list[str] = field(default_factory=lambda: ["up", "down", "by_date"])
    resolution_lag_days: float = 1.0


@dataclass
class UniverseConfig:
    min_markets: int = 2
    max_seconds_delay: int = 0
    exclude_tags: list[str] = field(default_factory=list)
    refresh_minutes: float = 20.0
    max_book_age_s: float = 20.0
    gamma_filters: dict[str, Any] = field(default_factory=dict)


@dataclass
class RiskConfig:
    bankroll_usd: float = 1000.0
    max_trade_usd: float = 100.0
    max_event_exposure_usd: float = 250.0
    max_hold_exposure_usd: float = 600.0
    max_daily_loss_usd: float = 25.0
    max_consecutive_failures: int = 3
    cooldown_s: float = 30.0
    kill_file: str = "KILL"
    require_geo_ok: bool = True


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    slippage_ticks: int = 0
    recheck_books: bool = True
    max_unwind_loss_usd: float = 5.0
    journal_path: str = "data/journal.jsonl"
    state_path: str = "data/state.json"


@dataclass
class EngineConfig:
    rest_sweep_s: float = 6.0
    use_websocket: bool = True
    ws_max_targets: int = 300
    ws_assets_per_conn: int = 400
    report_every_s: float = 60.0


@dataclass
class Config:
    convert: ConvertConfig = field(default_factory=ConvertConfig)
    yes_basket: YesBasketConfig = field(default_factory=YesBasketConfig)
    ladder: LadderConfig = field(default_factory=LadderConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)


def _build(cls: type, data: Any, path: str) -> Any:
    if not dataclasses.is_dataclass(cls):
        return data
    if data is None:
        return cls()
    if not isinstance(data, dict):
        raise ValueError(f"{path or 'config'}: expected a mapping, got {type(data).__name__}")
    hints = typing.get_type_hints(cls)
    names = {f.name for f in dataclasses.fields(cls)}
    unknown = set(data) - names
    if unknown:
        raise ValueError(f"{path or 'config'}: unknown keys {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        hint = hints[name]
        sub = f"{path}.{name}" if path else name
        if dataclasses.is_dataclass(hint):
            kwargs[name] = _build(hint, value, sub)
        else:
            kwargs[name] = _coerce(hint, value, sub)
    return cls(**kwargs)


def _coerce(hint: Any, value: Any, path: str) -> Any:
    origin = typing.get_origin(hint)
    if hint is float and isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if hint is int and isinstance(value, int) and not isinstance(value, bool):
        return value
    if hint is bool and isinstance(value, bool):
        return value
    if hint is str and isinstance(value, str):
        return value
    if origin in (list, dict) and isinstance(value, origin):
        return value
    if origin in (typing.Union, types.UnionType):
        return value
    raise ValueError(f"{path}: expected {hint}, got {value!r}")


def load_config(path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> Config:
    data: dict[str, Any] = {}
    if path:
        text = Path(path).read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
    for dotted, value in (overrides or {}).items():
        node = data
        *parents, leaf = dotted.split(".")
        for key in parents:
            node = node.setdefault(key, {})
        node[leaf] = value
    return _build(Config, data, "")
