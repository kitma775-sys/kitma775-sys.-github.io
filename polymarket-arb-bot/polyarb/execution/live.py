"""Live gateway on the official ``polymarket-client`` SDK (pip install 'polyarb[live]').

Orders are marketable FOK BUYs: ``amount`` = the pUSD notional of the planned
size at the current book and ``max_price`` = the worst level touched, so an
unchanged book fills exactly the planned shares and a worse book kills the leg
instead of filling at a bad price. All legs of an opportunity are signed in
parallel and posted together (batches of 15, the ``POST /orders`` limit).
"""

from __future__ import annotations

import asyncio
import logging
import math
from decimal import ROUND_DOWN, Decimal
from typing import Any, Sequence

from polyarb.execution.base import LegResult, SellResult
from polyarb.execution.settlement import (
    NEG_RISK_CTF_COLLATERAL_ADAPTER,
    ChainReader,
    encode_convert_positions,
    expected_convert_proceeds,
    to_base_units,
)
from polyarb.fees import fee_per_share
from polyarb.models import FEE_FREE, FeeSchedule, Leg, Outcome

BATCH_LIMIT = 15
log = logging.getLogger(__name__)


def _tick_decimals(tick: float) -> int:
    return max(0, -int(math.floor(math.log10(tick) + 1e-9)))


def _price(value: float, tick: float) -> Decimal:
    steps = math.floor(value / tick + 1e-9)
    return Decimal(str(round(steps * tick, _tick_decimals(tick))))


def _cents(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


class PolymarketGateway:
    mode = "live"

    def __init__(self, client: Any, chain: ChainReader | None = None, delayed_wait_s: float = 0.5) -> None:
        self.client = client
        self.chain = chain or ChainReader()
        self.delayed_wait_s = delayed_wait_s
        self._fee_bips: dict[str, int] = {}

    @classmethod
    async def create(cls, private_key: str, wallet: str | None = None, setup_approvals: bool = True) -> "PolymarketGateway":
        from polymarket import AsyncSecureClient

        client = await AsyncSecureClient.create(private_key=private_key, wallet=wallet)
        if setup_approvals:
            # Includes ConditionalTokens.setApprovalForAll(NegRiskCtfCollateralAdapter) needed by convert.
            await client.setup_trading_approvals()
        return cls(client)

    async def close(self) -> None:
        await self.client.close()
        await self.chain.close()

    async def balance(self) -> float:
        info = await self.client.get_balance_allowance(asset_type="COLLATERAL")
        return info.balance / 1e6

    async def position(self, token_id: str) -> float:
        info = await self.client.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
        return info.balance / 1e6

    async def _sign_buy(self, leg: Leg, slippage_ticks: int) -> Any:
        limit = min(leg.limit_price + slippage_ticks * leg.tick_size, 1.0 - leg.tick_size)
        return await self.client.create_market_order(
            token_id=leg.token_id,
            side="BUY",
            amount=_cents(leg.notional),
            max_price=_price(limit, leg.tick_size),
            order_type="FOK",
        )

    async def buy(self, legs: Sequence[Leg], slippage_ticks: int = 0) -> list[LegResult]:
        signed = await asyncio.gather(*(self._sign_buy(leg, slippage_ticks) for leg in legs), return_exceptions=True)
        results: list[LegResult | None] = [None] * len(legs)
        to_post: list[tuple[int, Any]] = []
        for i, s in enumerate(signed):
            if isinstance(s, BaseException):
                results[i] = LegResult(legs[i], False, status="sign_error", error=str(s))
            else:
                to_post.append((i, s))
        batches = [to_post[i:i + BATCH_LIMIT] for i in range(0, len(to_post), BATCH_LIMIT)]
        responses = await asyncio.gather(
            *(self.client.post_orders([s for _, s in batch]) for batch in batches), return_exceptions=True
        )
        for batch, resp in zip(batches, responses):
            if isinstance(resp, BaseException):
                for i, _ in batch:
                    results[i] = LegResult(legs[i], False, status="post_error", error=str(resp))
                continue
            for (i, _), r in zip(batch, resp):
                results[i] = self._leg_result(legs[i], r)
        uncertain = [i for i, r in enumerate(results) if r is not None and r.status in ("delayed", "live")]
        for i in uncertain:
            results[i] = await self._reconcile(legs[i], results[i])
        return [r for r in results if r is not None]

    def _leg_result(self, leg: Leg, resp: Any) -> LegResult:
        if not getattr(resp, "ok", False):
            return LegResult(leg, False, status=str(getattr(resp, "code", "rejected")), error=getattr(resp, "message", None))
        shares = float(resp.taking_amount)
        spent = float(resp.making_amount)
        avg = spent / shares if shares > 0 else leg.avg_price
        return LegResult(
            leg,
            resp.status == "matched" and shares > 0,
            shares=shares,
            spent=spent,
            fee=shares * fee_per_share(leg.fee_schedule, avg),
            order_id=str(resp.order_id),
            status=str(resp.status),
        )

    async def _reconcile(self, leg: Leg, result: LegResult) -> LegResult:
        """Delayed / resting responses: cancel any remainder, then read how much actually matched."""
        if not result.order_id:
            return LegResult(leg, False, status=result.status, error="no order id to reconcile")
        if result.status == "delayed":
            # Taker-delay markets (crypto ~150 ms, sports games 1-3 s) queue the order before matching it.
            await asyncio.sleep(leg.seconds_delay + self.delayed_wait_s)
        try:
            await self.client.cancel_order(order_id=result.order_id)
        except Exception as exc:
            log.warning("cancel %s failed: %s", result.order_id, exc)
        try:
            shares = float((await self.client.get_order(order_id=result.order_id)).size_matched)
        except Exception as exc:
            # The wallet may already hold this token, so the balance is capped at the planned size.
            log.warning("order lookup %s failed (%s); using token balance", result.order_id, exc)
            shares = min(await self.position(leg.token_id), leg.shares * 1.05)
        if shares <= 0:
            return LegResult(leg, False, status=result.status, order_id=result.order_id, error="no fill after reconcile")
        spent = shares * leg.avg_price
        return LegResult(leg, True, shares=shares, spent=spent, fee=shares * fee_per_share(leg.fee_schedule, leg.avg_price), order_id=result.order_id, status="reconciled")

    async def sell(
        self,
        token_id: str,
        outcome: Outcome,
        yes_token: str,
        shares: float,
        min_price: float,
        fee: FeeSchedule = FEE_FREE,
        tick: float = 0.01,
    ) -> SellResult:
        qty = Decimal(str(math.floor(shares * 100) / 100))
        if qty <= 0:
            return SellResult(token_id, outcome, False, error="dust")
        try:
            resp = await self.client.place_market_order(
                token_id=token_id, side="SELL", shares=qty, min_price=_price(max(min_price, tick), tick), order_type="FAK"
            )
        except Exception as exc:
            return SellResult(token_id, outcome, False, error=str(exc))
        if not getattr(resp, "ok", False):
            return SellResult(token_id, outcome, False, error=getattr(resp, "message", "rejected"))
        sold = float(resp.making_amount)
        proceeds = float(resp.taking_amount)
        avg = proceeds / sold if sold > 0 else 0.0
        return SellResult(token_id, outcome, sold > 0, shares=sold, proceeds=proceeds, fee=sold * fee_per_share(fee, avg))

    async def fee_bips(self, market_id: str) -> int:
        if market_id not in self._fee_bips:
            self._fee_bips[market_id] = await self.chain.fee_bips(market_id)
        return self._fee_bips[market_id]

    async def convert(self, neg_risk_market_id: str, index_set: int, shares: float) -> tuple[bool, str | None, float]:
        from polymarket import TransactionCall

        amount = to_base_units(shares)
        bips = await self.fee_bips(neg_risk_market_id)
        call = TransactionCall(
            to=NEG_RISK_CTF_COLLATERAL_ADAPTER,
            data=encode_convert_positions(neg_risk_market_id, index_set, amount),
        )
        try:
            handle = await self.client.execute_transaction(calls=[call], metadata="polyarb negrisk convert")
            outcome = await handle.wait()
        except Exception as exc:
            log.error("convert failed: %s", exc)
            return False, None, 0.0
        tx = getattr(outcome, "transaction_hash", None)
        return True, str(tx) if tx else None, expected_convert_proceeds(index_set, amount, bips) / 1e6
