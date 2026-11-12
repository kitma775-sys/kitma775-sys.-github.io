"""Post-trade capital recycler: resting 0.995 flip exits, gasless auto-redemption, and hold reconciliation.

Why this matters for PnL:
1. Single-leg METAR locks resolve in <24h, but bonding bots regularly bid 0.992-0.999
   within minutes. Posting a resting ``postOnly`` SELL order (0 maker fee) turns
   positions back into liquid pUSD in minutes so the same capital compounds all day.
2. When a held position (ladder, YES basket, or METAR lock) resolves on-chain,
   ``AutoRedeemer`` cancels any resting exit order and calls ``redeem_positions``
   to return $1.00/sh pUSD directly to the wallet without waiting days or needing
   manual clicks on polymarket.com.
3. ``reconcile_holds`` syncs ``state.holds`` with actual on-chain conditional token
   balances so externally sold/redeemed positions never block ``hold_exposure``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from polyarb.config import Config
from polyarb.execution.base import Gateway
from polyarb.journal import Journal
from polyarb.models import Outcome, Side
from polyarb.risk import HoldPosition, RiskManager

DATA_API_URL = "https://data-api.polymarket.com"
log = logging.getLogger(__name__)


class LifecycleManager:
    def __init__(
        self,
        cfg: Config,
        gateway: Gateway,
        risk: RiskManager,
        journal: Journal,
        wallet_funder: str | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.gateway = gateway
        self.risk = risk
        self.journal = journal
        self.wallet_funder = wallet_funder
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._task: asyncio.Task | None = None
        self.flips_completed = 0
        self.redemptions = 0
        self.reconciled = 0

    async def sync_wallet_balance(self) -> float | None:
        if self.gateway.mode != "live" or not hasattr(self.gateway, "balance"):
            return None
        try:
            bal = float(await self.gateway.balance())
            self.risk.wallet_balance = bal
            return bal
        except Exception as exc:
            log.warning("wallet balance sync failed: %s", exc)
            return self.risk.wallet_balance

    async def step(self) -> None:
        await self.sync_wallet_balance()
        await self._manage_flip_orders()
        if self.gateway.mode == "live":
            if self.cfg.execution.auto_redeem:
                await self._auto_redeem_live()
            await self._reconcile_external()

    async def _manage_flip_orders(self) -> None:
        if not self.cfg.metar.flip_enabled:
            return
        changed = False
        for hold in list(self.risk.state.holds):
            if hold.strategy != "metar_lock" or len(hold.legs) != 1:
                continue
            leg = hold.legs[0]
            token = str(leg.get("token") or "")
            yes_token = str(leg.get("yes_token") or token)
            outcome = Outcome(str(leg.get("outcome") or "YES"))
            shares = float(leg.get("shares") or hold.shares)
            tick = float(leg.get("tick") or 0.001)
            if not token or shares < 1.0:
                continue

            if hold.exit_order_id:
                state = await self.gateway.get_order_state(hold.exit_order_id)
                if state is None:
                    continue
                if state.status == "MATCHED" or (state.original_size > 0 and state.size_matched >= state.original_size - 0.05):
                    px = hold.exit_price or state.price
                    proceeds = state.size_matched * px  # maker limit order => 0 maker fee
                    pnl = self.risk.close_hold(hold, proceeds)
                    self.flips_completed += 1
                    self.journal.write(
                        "flip_closed",
                        {
                            "key": hold.key,
                            "title": hold.title,
                            "shares": round(state.size_matched, 2),
                            "price": px,
                            "proceeds": round(proceeds, 4),
                            "pnl": round(pnl, 4),
                        },
                    )
                    log.info("FLIP CLOSED %s %.2f sh @ %.3f pnl=$%.3f", hold.title[:50], state.size_matched, px, pnl)
                    await self.sync_wallet_balance()
                    continue
                if state.status == "CANCELED":
                    hold.exit_order_id = None
                    changed = True

            # Place a resting postOnly maker SELL order when none is open and we aren't paused/killed.
            if hold.exit_order_id is None and not self.risk.state.killed:
                if self.cfg.risk.kill_file and _file_exists(self.cfg.risk.kill_file):
                    continue
                min_profitable_px = (hold.cost + self.cfg.metar.flip_min_profit_usd) / shares
                target_px = max(self.cfg.metar.flip_ask_price, round(min_profitable_px + tick, 4))
                if target_px >= 1.0:
                    continue
                res = await self.gateway.place_limit(
                    token_id=token,
                    outcome=outcome,
                    yes_token=yes_token,
                    side=Side.SELL,
                    price=target_px,
                    shares=shares,
                    tick=tick,
                    post_only=True,
                )
                if res.ok and res.order_id:
                    hold.exit_order_id = res.order_id
                    hold.exit_price = target_px
                    changed = True
                    log.info("placed resting flip SELL %s %.2f sh @ %.3f (id=%s)", hold.title[:45], shares, target_px, res.order_id)
        if changed:
            self.risk.save()

    async def _fetch_winning_redeemables(self) -> dict[str, tuple[str, float]]:
        """Return token_id -> (condition_id, current_value_usd) for resolved winning positions (> $0.05)."""
        if not self.wallet_funder:
            return {}
        try:
            resp = await self._http.get(
                f"{DATA_API_URL}/v2/positions",
                params={"user": self.wallet_funder, "redeemable": "true", "limit": 200},
            )
            if resp.status_code != 200:
                return {}
            rows = (resp.json().get("data")) or []
        except Exception as exc:
            log.debug("redeemable lookup failed: %s", exc)
            return {}
        out: dict[str, tuple[str, float]] = {}
        for row in rows:
            if not isinstance(row, dict) or not row.get("redeemable"):
                continue
            val = float(row.get("current_value") or 0.0)
            px = float(row.get("current_price") or 0.0)
            tok = str(row.get("token_id") or "")
            cond = str(row.get("condition_id") or "")
            if val >= 0.05 and px >= 0.5 and tok and cond:
                out[tok] = (cond, val)
        return out

    async def _auto_redeem_live(self) -> None:
        if not self.risk.state.holds or not self.wallet_funder:
            return
        redeemables = await self._fetch_winning_redeemables()
        if not redeemables:
            return
        for hold in list(self.risk.state.holds):
            matched_legs = [leg for leg in hold.legs if str(leg.get("token") or "") in redeemables]
            if not matched_legs:
                continue
            if hold.exit_order_id:
                await self.gateway.cancel(hold.exit_order_id)
                hold.exit_order_id = None
            conds: dict[str, float] = {}
            for leg in matched_legs:
                cond, val = redeemables[str(leg["token"])]
                conds[cond] = conds.get(cond, 0.0) + val
            total_redeemed = 0.0
            all_ok = True
            txs: list[str] = []
            for cond, val in conds.items():
                ok, tx = await self.gateway.redeem(cond)
                if ok:
                    total_redeemed += val
                    if tx:
                        txs.append(tx)
                else:
                    all_ok = False
            if all_ok and total_redeemed > 0:
                pnl = self.risk.close_hold(hold, total_redeemed)
                self.redemptions += 1
                self.journal.write(
                    "redeemed",
                    {
                        "key": hold.key,
                        "title": hold.title,
                        "strategy": hold.strategy,
                        "proceeds": round(total_redeemed, 4),
                        "pnl": round(pnl, 4),
                        "txs": txs,
                    },
                )
                log.info("AUTO-REDEEMED %s proceeds=$%.2f pnl=$%.3f", hold.title[:50], total_redeemed, pnl)
                await self.sync_wallet_balance()

    async def _reconcile_external(self) -> None:
        if not hasattr(self.gateway, "position") or not self.risk.state.holds:
            return
        removed: list[HoldPosition] = await self.risk.reconcile_holds(self.gateway.position)
        for hold in removed:
            self.reconciled += 1
            self.journal.write(
                "hold_reconciled",
                {
                    "key": hold.key,
                    "title": hold.title,
                    "strategy": hold.strategy,
                    "cost": round(hold.cost, 2),
                },
            )
            log.warning("reconciled externally closed hold %s (cost=$%.2f freed from exposure)", hold.title[:55], hold.cost)
        if removed:
            await self.sync_wallet_balance()

    async def start(self) -> None:
        await self.stop()
        await self.step()
        self._task = asyncio.create_task(self._loop(), name="lifecycle")

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.cfg.execution.lifecycle_every_s)
            try:
                await self.step()
            except Exception as exc:
                log.warning("lifecycle step error: %s", exc)

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    async def close(self) -> None:
        await self.stop()
        if self._own_http:
            await self._http.aclose()

    def stats(self) -> dict[str, Any]:
        open_flips = sum(1 for h in self.risk.state.holds if h.exit_order_id)
        return {
            "open_flips": open_flips,
            "flips_completed": self.flips_completed,
            "redemptions": self.redemptions,
            "reconciled": self.reconciled,
            "wallet_balance": None if self.risk.wallet_balance is None else round(self.risk.wallet_balance, 2),
        }


def _file_exists(path: str) -> bool:
    import os

    return bool(path and os.path.exists(path))
