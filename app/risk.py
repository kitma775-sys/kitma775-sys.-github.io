from __future__ import annotations

from dataclasses import dataclass

from app.hunter import Setup


@dataclass(frozen=True)
class RiskDecision:
    ok: bool
    reason: str


def approve(
    setup: Setup,
    *,
    stale_leg: float,
    tail_confirm: float,
    max_imbalance: float,
    inventory_up: float,
    inventory_down: float,
    daily_pnl: float,
    daily_loss_limit: float,
    open_markets: int,
    max_open_markets: int,
    killed: bool,
    engine_running: bool,
    auto_execute: bool,
    cash: float | None = None,
    cost: float | None = None,
    unmatched_shares: float = 0.0,
    seconds_left: float | None = None,
    maker_window: float = 75.0,
    maker_min_leg: float = 0.22,
    maker_max_skew: float = 0.28,
) -> RiskDecision:
    if killed:
        return RiskDecision(False, "kill_switch")
    if not engine_running:
        return RiskDecision(False, "paused")
    if not auto_execute:
        return RiskDecision(False, "manual_only")
    if daily_loss_limit > 0 and daily_pnl <= -abs(daily_loss_limit):
        return RiskDecision(False, "daily_loss_limit")
    if setup.shares <= 0 or setup.fillable <= 0:
        return RiskDecision(False, "no_size")
    if setup.net <= 0:
        return RiskDecision(False, "non_positive_net")
    if cash is not None and cost is not None and cost > cash + 1e-9:
        return RiskDecision(False, "insufficient_cash")

    cheap = min(setup.up_price, setup.down_price)
    rich = max(setup.up_price, setup.down_price)
    if cheap < stale_leg and rich < tail_confirm:
        return RiskDecision(False, "stale_quote")
    if setup.kind == "maker":
        if cheap < max(stale_leg, maker_min_leg):
            return RiskDecision(False, "maker_unbalanced")
        if abs(setup.up_price - setup.down_price) > maker_max_skew:
            return RiskDecision(False, "maker_skew")
        if seconds_left is None or seconds_left > maker_window or seconds_left < 3:
            return RiskDecision(False, "maker_too_early")
        if unmatched_shares > 0.5:
            return RiskDecision(False, "unmatched_book")

    if abs(inventory_up - inventory_down) > max_imbalance:
        return RiskDecision(False, "already_naked")

    holding = inventory_up > 0.01 or inventory_down > 0.01
    if not holding and open_markets >= max_open_markets:
        return RiskDecision(False, "too_many_markets")

    return RiskDecision(True, "approved")
