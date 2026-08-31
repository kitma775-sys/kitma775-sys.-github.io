from __future__ import annotations

from dataclasses import dataclass

from app.hunter import Setup, in_favorite_window, is_favorite_setup, is_twap_setup, parse_favorite_dir


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
    favorite_min_price: float = 0.97,
    favorite_max_price: float = 0.98,
    favorite_window_seconds: float = 60.0,
    favorite_dir: str = "auto",
    max_usd_per_trade: float = 25.0,
    favorite_spent: float = 0.0,
    twap_min_price: float = 0.45,
    twap_max_price: float = 0.55,
    twap_min_left: float = 120.0,
    twap_max_left: float = 280.0,
    twap_late_left: float = 180.0,
    twap_late_min_price: float = 0.50,
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
    fav = is_favorite_setup(setup)
    twap = is_twap_setup(setup)
    one = fav or twap
    if twap:
        lo = min(float(twap_min_price), float(twap_max_price))
        hi = max(float(twap_min_price), float(twap_max_price))
        if rich + 1e-12 < lo or rich - 1e-12 > hi:
            return RiskDecision(False, "twap_out_of_band")
        if seconds_left is None or seconds_left < float(twap_min_left) or seconds_left > float(twap_max_left) + 1e-9:
            return RiskDecision(False, "twap_window")
        if seconds_left < float(twap_late_left) and rich + 1e-12 < float(twap_late_min_price):
            return RiskDecision(False, "twap_late_cheap")
        spent = float(favorite_spent or 0) if (inventory_up > 0.01 or inventory_down > 0.01) else 0.0
        new_cost = float(cost) if cost is not None else float(setup.cost)
        if spent + new_cost > float(max_usd_per_trade) + 0.05:
            return RiskDecision(False, "twap_stack_cap")
    elif fav:
        lo = min(float(favorite_min_price), float(favorite_max_price))
        hi = max(float(favorite_min_price), float(favorite_max_price))
        band_lo = lo - 0.01 if setup.kind == "maker" else lo
        if rich + 1e-12 < band_lo or rich - 1e-12 > hi:
            return RiskDecision(False, "favorite_out_of_band")
        if not in_favorite_window(seconds_left, favorite_window_seconds):
            return RiskDecision(False, "favorite_too_early")
        want = parse_favorite_dir(favorite_dir)
        leg = str((setup.extra or {}).get("leg") or "")
        if want != "auto" and leg and leg != want:
            return RiskDecision(False, "favorite_wrong_dir")
        spent = float(favorite_spent or 0) if (inventory_up > 0.01 or inventory_down > 0.01) else 0.0
        new_cost = float(cost) if cost is not None else float(setup.cost)
        if spent + new_cost > float(max_usd_per_trade) + 0.05:
            return RiskDecision(False, "favorite_stack_cap")
        if setup.kind == "maker" and unmatched_shares > 0.5:
            return RiskDecision(False, "unmatched_book")
    else:
        if cheap < stale_leg and rich < tail_confirm:
            return RiskDecision(False, "stale_quote")
        if setup.kind == "maker":
            if cheap < max(stale_leg, maker_min_leg):
                return RiskDecision(False, "maker_unbalanced")
            if abs(setup.up_price - setup.down_price) > maker_max_skew:
                return RiskDecision(False, "maker_skew")
            if maker_window < 3:
                return RiskDecision(False, "maker_window_off")
            if seconds_left is None or seconds_left > maker_window or seconds_left < 3:
                return RiskDecision(False, "maker_too_early")
            if unmatched_shares > 0.5:
                return RiskDecision(False, "unmatched_book")

    if not one and abs(inventory_up - inventory_down) > max_imbalance:
        return RiskDecision(False, "already_naked")

    holding = inventory_up > 0.01 or inventory_down > 0.01
    if not holding and open_markets >= max_open_markets:
        return RiskDecision(False, "too_many_markets")

    return RiskDecision(True, "approved")
