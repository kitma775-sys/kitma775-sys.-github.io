from __future__ import annotations

import dataclasses
import os
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import yaml

ENV_PREFIX = "POLYARB__"


@dataclass
class ConvertConfig:
    """NegRisk NO-basket bought on the CLOB and converted to pUSD on-chain (no resolution risk)."""

    enabled: bool = True
    min_profit_usd: float = 0.25
    min_edge: float = 0.003
    min_unit_edge: float = 0.0005
    max_legs: int = 25
    gas_usd: float = 0.0
    surplus_haircut: float = 0.85
    surplus_min_depth_ratio: float = 2.0


@dataclass
class YesBasketConfig:
    """Buy YES on every outcome of an exhaustive NegRisk event and hold to resolution."""

    enabled: bool = True
    min_profit_usd: float = 1.0
    min_edge: float = 0.01
    min_apr: float = 0.25
    max_days: float = 45.0
    min_unit_edge: float = 0.002
    max_legs: int = 20
    require_catch_all: bool = True
    min_ask_sum: float = 0.85
    resolution_lag_days: float = 1.0


@dataclass
class MetarConfig:
    """Deterministic NOAA/AWC METAR temperature lock + surviving weather basket + fast 0.99+ flip."""

    enabled: bool = True
    min_profit_usd: float = 0.15
    min_edge: float = 0.004
    min_unit_edge: float = 0.002
    basket_min_profit_usd: float = 0.25
    basket_min_edge: float = 0.005
    max_days: float = 3.0
    resolution_lag_days: float = 0.5
    fast_poll_s: float = 3.0
    idle_poll_s: float = 15.0
    glitch_delta_c: float = 12.0
    flip_enabled: bool = True
    flip_ask_price: float = 0.995
    flip_min_instant_bid: float = 0.990
    flip_min_profit_usd: float = 0.08


@dataclass
class BinanceClockConfig:
    """Deterministic Binance Spot closed-candle sniping & 0-leg-risk bonding (hourly, noon, and hit barriers)."""

    enabled: bool = True
    min_profit_usd: float = 0.12
    min_edge: float = 0.004
    min_unit_edge: float = 0.002
    max_days: float = 3.0
    resolution_lag_days: float = 0.15
    burst_poll_s: float = 0.5
    idle_poll_s: float = 6.0
    flip_enabled: bool = True


@dataclass
class CryptoShortConfig:
    """Safe BTC/ETH/SOL/XRP 5m · 15m · 1h arbitrage (post-close 0-risk lock, exact-integral TWAP lock, 5m<->15m & 1h cross-locks)."""

    enabled: bool = True
    min_profit_usd: float = 0.08
    min_edge: float = 0.004
    min_unit_edge: float = 0.002
    postclose_window_s: float = 75.0
    preclose_lock_enabled: bool = True
    shock_bps: float = 90.0
    max_left_s: float = 20.0
    min_left_s: float = 2.0
    preclose_max_ask: float = 0.985
    sync_pair_enabled: bool = True
    poll_slugs_s: float = 20.0
    flip_enabled: bool = True


@dataclass
class MakerArbConfig:
    """Lead-leg postOnly maker quote (0 maker fee) + instant FOK hedge & convert on fill."""

    enabled: bool = True
    min_profit_usd: float = 0.25
    min_edge: float = 0.006
    deterministic_min_edge: float = 0.004
    rebate_credit_ratio: float = 0.75
    min_keep_edge: float = 0.002
    min_volume_24h: float = 500.0
    min_hedge_depth_ratio: float = 2.0
    max_open_quotes: int = 6
    max_quotes_per_event: int = 2
    pool_hedge_slots: int = 2
    allow_join_bid: bool = True
    repeg_after_s: float = 3.0
    max_legs: int = 8
    max_ladder_days: float = 21.0
    poll_s: float = 1.5
    quote_ttl_s: float = 300.0


@dataclass
class TwinConfig:
    """Cross-event same-oracle Range Bracket <-> Cumulative Above partition arbitrage."""

    enabled: bool = True
    min_profit_usd: float = 0.20
    min_edge: float = 0.004
    min_apr: float = 0.20
    max_days: float = 14.0
    min_unit_edge: float = 0.001
    max_legs: int = 5
    resolution_lag_days: float = 0.5


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
    liquid_reserve_usd: float = 0.0


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    slippage_ticks: int = 0
    recheck_books: bool = True
    fast_ws_skip_recheck_s: float = 1.5
    max_unwind_loss_usd: float = 5.0
    auto_redeem: bool = True
    early_exit_enabled: bool = True
    early_exit_min_bid: float = 0.992
    early_exit_min_profit_usd: float = 0.05
    lifecycle_every_s: float = 20.0
    journal_path: str = "data/journal.jsonl"
    state_path: str = "data/state.json"


@dataclass
class EngineConfig:
    rest_sweep_s: float = 6.0
    eval_interval_ms: float = 50.0
    use_websocket: bool = True
    ws_max_targets: int = 300
    ws_assets_per_conn: int = 400
    report_every_s: float = 60.0


@dataclass
class TelegramConfig:
    notify_executions: bool = True
    notify_flips_redeems: bool = True
    notify_risk: bool = True
    notify_opportunities: bool = False
    opportunity_min_profit_usd: float = 1.0
    summary_hours: float = 4.0


@dataclass
class Config:
    convert: ConvertConfig = field(default_factory=ConvertConfig)
    yes_basket: YesBasketConfig = field(default_factory=YesBasketConfig)
    ladder: LadderConfig = field(default_factory=LadderConfig)
    twin: TwinConfig = field(default_factory=TwinConfig)
    metar: MetarConfig = field(default_factory=MetarConfig)
    binance: BinanceClockConfig = field(default_factory=BinanceClockConfig)
    crypto_short: CryptoShortConfig = field(default_factory=CryptoShortConfig)
    maker: MakerArbConfig = field(default_factory=MakerArbConfig)
    universe: UniverseConfig = field(default_factory=UniverseConfig)
    risk: RiskConfig = field(default_factory=RiskConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    engine: EngineConfig = field(default_factory=EngineConfig)
    tg: TelegramConfig = field(default_factory=TelegramConfig)


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


def env_overrides(environ: Mapping[str, str] | None = None) -> dict[str, Any]:
    """``POLYARB__RISK__MAX_TRADE_USD=50`` -> ``{"risk.max_trade_usd": 50}``; values are parsed as YAML scalars."""
    environ = os.environ if environ is None else environ
    out: dict[str, Any] = {}
    for key, raw in environ.items():
        if key.startswith(ENV_PREFIX) and len(key) > len(ENV_PREFIX):
            dotted = ".".join(part.lower() for part in key[len(ENV_PREFIX):].split("__"))
            out[dotted] = yaml.safe_load(raw) if raw.strip() else ""
    return out


def use_data_dir(cfg: Config, data_dir: str | Path, mode: str) -> None:
    """Keep paper and live books apart: ``<data>/<mode>/journal.jsonl``, ``<data>/<mode>/state.json``, ``<data>/KILL``."""
    root = Path(data_dir)
    cfg.execution.journal_path = str(root / mode / "journal.jsonl")
    cfg.execution.state_path = str(root / mode / "state.json")
    if cfg.risk.kill_file:
        cfg.risk.kill_file = str(root / Path(cfg.risk.kill_file).name)
