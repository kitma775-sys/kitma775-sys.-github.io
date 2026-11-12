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
import os
from decimal import ROUND_DOWN, Decimal
from typing import Any, Iterable, Mapping, Sequence

from polyarb.execution.base import LegResult, LimitOrderResult, OrderState, SellResult
from polyarb.execution.settlement import (
    NEG_RISK_CTF_COLLATERAL_ADAPTER,
    ChainReader,
    encode_convert_positions,
    expected_convert_proceeds,
    to_base_units,
)
from polyarb.fees import fee_per_share
from polyarb.models import FEE_FREE, FeeSchedule, Leg, Market, Outcome, Side
from polyarb.risk import REGION_REJECTED

BATCH_LIMIT = 15
log = logging.getLogger(__name__)


def _region_refused(status: Any, message: Any) -> bool:
    """HTTP 403 on the order API is a refusal of this server (region policy or a WAF block), never transient."""
    return status == 403 or "restricted in your region" in str(message or "").lower()


def _tick_decimals(tick: float) -> int:
    return max(0, -int(math.floor(math.log10(tick) + 1e-9)))


def _price(value: float, tick: float) -> Decimal:
    steps = math.floor(value / tick + 1e-9)
    return Decimal(str(round(steps * tick, _tick_decimals(tick))))


def _cents(value: float) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def _shares_dec(value: float) -> Decimal:
    return Decimal(str(math.floor(value * 100 + 1e-7) / 100)).quantize(Decimal("0.01"), rounding=ROUND_DOWN)


def resolve_gateway_api_key(private_key: str, environ: Mapping[str, str] | None = None) -> Any:
    """Build RelayerApiKey or BuilderApiKey from environment variables when configured."""
    env = os.environ if environ is None else environ
    relayer_key = (env.get("POLYMARKET_RELAYER_API_KEY") or "").strip()
    if relayer_key:
        from polymarket import RelayerApiKey

        addr = (env.get("POLYMARKET_RELAYER_ADDRESS") or env.get("POLYMARKET_RELAYER_API_KEY_ADDRESS") or "").strip()
        if not addr:
            from eth_account import Account

            addr = Account.from_key(private_key).address
        return RelayerApiKey(key=relayer_key, address=addr)
    builder_key = (env.get("POLYMARKET_BUILDER_API_KEY") or "").strip()
    builder_secret = (env.get("POLYMARKET_BUILDER_SECRET") or "").strip()
    builder_pass = (env.get("POLYMARKET_BUILDER_PASSPHRASE") or "").strip()
    if builder_key and builder_secret and builder_pass:
        from polymarket import BuilderApiKey

        return BuilderApiKey(builder_key, builder_secret, builder_pass)
    return None


class PolymarketGateway:
    mode = "live"

    def __init__(self, client: Any, chain: ChainReader | None = None, delayed_wait_s: float = 0.5) -> None:
        self.client = client
        self.chain = chain or ChainReader()
        self.delayed_wait_s = delayed_wait_s
        self._fee_bips: dict[str, int] = {}

    @classmethod
    async def create(
        cls,
        private_key: str,
        wallet: str | None = None,
        setup_approvals: bool = True,
        environ: Mapping[str, str] | None = None,
    ) -> "PolymarketGateway":
        from polymarket import AsyncSecureClient

        api_key = resolve_gateway_api_key(private_key, environ)
        client = await AsyncSecureClient.create(private_key=private_key, wallet=wallet, api_key=api_key)
        if setup_approvals:
            try:
                # Includes ConditionalTokens.setApprovalForAll(NegRiskCtfCollateralAdapter) needed by convert.
                await client.setup_trading_approvals()
            except Exception as exc:
                if api_key is None and "Gasless transactions require" in str(exc):
                    log.warning("skipping gasless approval setup without relayer key (%s); existing approvals will be used", exc)
                else:
                    await client.close()
                    raise
        return cls(client)

    def wallet_info(self) -> dict[str, str]:
        """The wallet the SDK resolved (the one to fund) and its type; only EOA wallets pay their own gas."""
        return {"signer": str(self.client.signer), "funder": str(self.client.wallet), "type": str(self.client.wallet_type)}

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
                status = REGION_REJECTED if _region_refused(getattr(s, "status", None), s) else "sign_error"
                results[i] = LegResult(legs[i], False, status=status, error=str(s))
            else:
                to_post.append((i, s))
        batches = [to_post[i:i + BATCH_LIMIT] for i in range(0, len(to_post), BATCH_LIMIT)]
        responses = await asyncio.gather(
            *(self.client.post_orders([s for _, s in batch]) for batch in batches), return_exceptions=True
        )
        for batch, resp in zip(batches, responses):
            if isinstance(resp, BaseException):
                status = REGION_REJECTED if _region_refused(getattr(resp, "status", None), resp) else "post_error"
                for i, _ in batch:
                    results[i] = LegResult(legs[i], False, status=status, error=str(resp))
                continue
            for (i, _), r in zip(batch, resp):
                results[i] = self._leg_result(legs[i], r)
        uncertain = [i for i, r in enumerate(results) if r is not None and r.status in ("delayed", "live")]
        for i in uncertain:
            results[i] = await self._reconcile(legs[i], results[i])
        return [r for r in results if r is not None]

    def _leg_result(self, leg: Leg, resp: Any) -> LegResult:
        if not getattr(resp, "ok", False):
            message = getattr(resp, "message", None)
            status = REGION_REJECTED if _region_refused(None, message) else str(getattr(resp, "code", "rejected"))
            return LegResult(leg, False, status=status, error=message)
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

    async def place_limit(
        self,
        token_id: str,
        outcome: Outcome,
        yes_token: str,
        side: Side,
        price: float,
        shares: float,
        tick: float = 0.01,
        post_only: bool = True,
    ) -> LimitOrderResult:
        qty = _shares_dec(shares)
        if qty <= 0:
            return LimitOrderResult(False, status="dust", error="dust")
        px = _price(max(tick, min(price, 1.0 - tick)), tick)
        try:
            resp = await self.client.place_limit_order(
                token_id=token_id,
                side=side.value,
                price=px,
                size=qty,
                post_only=post_only,
            )
        except Exception as exc:
            status = REGION_REJECTED if _region_refused(getattr(exc, "status", None), exc) else "post_error"
            return LimitOrderResult(False, status=status, error=str(exc))
        if not getattr(resp, "ok", False):
            msg = getattr(resp, "message", None)
            code = REGION_REJECTED if _region_refused(None, msg) else str(getattr(resp, "code", "rejected"))
            return LimitOrderResult(False, status=code, error=msg)
        matched = float(resp.taking_amount) if side is Side.BUY else float(resp.making_amount)
        return LimitOrderResult(True, order_id=str(resp.order_id), status=str(resp.status), matched_shares=matched)

    async def get_order_state(self, order_id: str) -> OrderState | None:
        try:
            info = await self.client.get_order(order_id=order_id)
        except Exception as exc:
            log.debug("get_order %s failed: %s", order_id, exc)
            return None
        return OrderState(
            order_id=str(getattr(info, "id", order_id)),
            status=str(getattr(info, "status", "")).upper(),
            original_size=float(getattr(info, "original_size", 0)),
            size_matched=float(getattr(info, "size_matched", 0)),
            price=float(getattr(info, "price", 0)),
        )

    async def cancel(self, order_id: str) -> bool:
        try:
            await self.client.cancel_order(order_id=order_id)
            return True
        except Exception as exc:
            log.debug("cancel %s: %s", order_id, exc)
            return False

    async def redeem(self, condition_id: str) -> tuple[bool, str | None]:
        try:
            handle = await self.client.redeem_positions(condition_id=condition_id, metadata="polyarb auto redeem")
            outcome = await handle.wait()
        except Exception as exc:
            log.warning("redeem %s failed: %s", condition_id, exc)
            return False, None
        tx = getattr(outcome, "transaction_hash", None)
        return True, str(tx) if tx else None

    async def merge(self, condition_id: str, shares: float) -> tuple[bool, str | None]:
        amount = to_base_units(shares)
        if amount <= 0:
            return False, None
        try:
            handle = await self.client.merge_positions(condition_id=condition_id, amount=amount, metadata="polyarb merge")
            outcome = await handle.wait()
        except Exception as exc:
            log.warning("merge %s failed: %s", condition_id, exc)
            return False, None
        tx = getattr(outcome, "transaction_hash", None)
        return True, str(tx) if tx else None

    def warm_markets(self, markets: Iterable[Market]) -> int:
        """Seed the SDK's order-metadata cache from Gamma Market rows to avoid 2 HTTP calls per cold token."""
        try:
            ctx = getattr(self.client, "_ctx", None)
            cache = getattr(ctx, "order_metadata", None)
            if cache is None:
                return 0
            from polymarket._internal.actions.orders.market_data import _ALLOWED_TICK_SIZES, MarketInfo, PlatformFeeInfo

            seeded = 0
            for m in markets:
                cid = m.condition_id
                if not (cid and cid.startswith("0x") and len(cid) == 66 and m.yes_token and m.no_token):
                    continue
                tick_dec = Decimal(str(m.tick_size))
                if tick_dec not in _ALLOWED_TICK_SIZES:
                    continue
                info = MarketInfo(
                    fee_info=PlatformFeeInfo(rate=Decimal(str(m.fee.rate)), exponent=Decimal(str(m.fee.exponent))),
                    neg_risk=bool(m.neg_risk),
                    tick_size=tick_dec,
                    token_ids=frozenset((m.yes_token, m.no_token)),
                )
                cache._conditions.set(m.yes_token, cid)
                cache._conditions.set(m.no_token, cid)
                cache._markets.set(cid, info)
                seeded += 1
            return seeded
        except Exception as exc:
            log.debug("warm_markets skipped: %s", exc)
            return 0
