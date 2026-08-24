"""Official Polymarket taker fee curve.

fee = C × feeRate × p × (1 − p)
Makers pay 0. Crypto default feeRate is 0.07.
"""

from __future__ import annotations


def clamp_price(price: float) -> float:
    return min(max(float(price), 0.0), 1.0)


def taker_fee(shares: float, price: float, fee_rate: float) -> float:
    p = clamp_price(price)
    c = max(float(shares), 0.0)
    rate = max(float(fee_rate), 0.0)
    return round(c * rate * p * (1.0 - p), 5)


def pair_taker_fee(shares: float, up_price: float, down_price: float, fee_rate: float) -> float:
    return taker_fee(shares, up_price, fee_rate) + taker_fee(shares, down_price, fee_rate)


def gross_edge(up_price: float, down_price: float) -> float:
    return 1.0 - (float(up_price) + float(down_price))


def taker_net(shares: float, up_price: float, down_price: float, fee_rate: float) -> float:
    return round(gross_edge(up_price, down_price) * shares - pair_taker_fee(shares, up_price, down_price, fee_rate), 5)
