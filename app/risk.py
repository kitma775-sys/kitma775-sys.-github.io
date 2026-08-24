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
    if setup.kind == "maker" and cheap < max(stale_leg, 0.10):
        return RiskDecision(False, "maker_unbalanced")

    if abs(inventory_up - inventory_down) > max_imbalance:
        return RiskDecision(False, "already_naked")

    holding = inventory_up > 0.01 or inventory_down > 0.01
    if not holding and open_markets >= max_open_markets:
        return RiskDecision(False, "too_many_markets")

    return RiskDecision(True, "approved")
