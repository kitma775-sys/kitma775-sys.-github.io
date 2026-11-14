"""Paper sleeve for 5-minute crypto Up/Down markets.

Settlement uses the Chainlink 60-second TWAP at the end of the window versus
the same stream at the start (see the market description). With ``r`` seconds
left, that ending average can still move by about ``(r/60)`` times a price
shock. ``locked_side`` is the side that survives a shock of ``shock_bps``.

A locked side is only bought when its ask is still at or below ``max_ask``.
At 92c the taker fee is about 0.5c, so the fill is cheap relative to a lock.
When the book has already moved to 98c, there is nothing to do.
"""

from __future__ import annotations

from dataclasses import dataclass

WINDOW_S = 300
TWAP_S = 60
SHOCK_BPS = 40.0
MAX_ASK = 0.92
MIN_LEFT_S = 8
MAX_LEFT_S = 25
STAKE_USD = 20.0
DAILY_STOP_USD = -40.0


def locked_side(
    open_px: float,
    now_px: float,
    seconds_left: float,
    shock_bps: float = SHOCK_BPS,
    twap_s: float = TWAP_S,
) -> str | None:
    """``up`` / ``down`` when the ending 60s TWAP cannot cross the open under the shock, else None."""
    if open_px <= 0 or now_px <= 0 or seconds_left <= 0 or twap_s <= 0:
        return None
    adverse = now_px * (shock_bps / 10_000.0) * (seconds_left / twap_s)
    if now_px - adverse >= open_px:
        return "up"
    if now_px + adverse <= open_px:
        return "down"
    return None


@dataclass(frozen=True, slots=True)
class LockOrder:
    side: str
    ask: float
    shares: float


def order_if_cheap(
    twap_open: float,
    twap_now: float,
    spot_open: float,
    spot_now: float,
    seconds_left: float,
    ask_up: float | None,
    ask_down: float | None,
    stake_usd: float = STAKE_USD,
    max_ask: float = MAX_ASK,
    min_left: float = MIN_LEFT_S,
    max_left: float = MAX_LEFT_S,
) -> LockOrder | None:
    """Buy only when the TWAP lock and the spot move agree, and the ask is still cheap."""
    if not min_left <= seconds_left <= max_left:
        return None
    side = locked_side(twap_open, twap_now, seconds_left)
    if side is None or side != locked_side(spot_open, spot_now, seconds_left):
        return None
    ask = ask_up if side == "up" else ask_down
    if ask is None or not 0.02 <= ask <= max_ask:
        return None
    shares = stake_usd / ask
    if shares < 5:
        return None
    return LockOrder(side, ask, shares)
