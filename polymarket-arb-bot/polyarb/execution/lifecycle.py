"""Post-trade capital recycler: 0.995 maker flips, dominant-leg + middle-zone windfall harvest, auto-redeem, and reconciliation.

Why this matters for PnL & capital velocity:
1. Single-leg METAR locks resolve in <24h, and bonding bots regularly bid 0.992-0.999
   within minutes. Posting a resting ``postOnly`` SELL order (0 maker fee) turns
   positions back into liquid pUSD in minutes so the same capital compounds all day.
2. Multi-leg holds (``ladder``, ``maker_ladder``, ``negrisk_yes_basket``, ``maker_yes_basket``,
   ``twin_arb``, ``maker_twin``) exhibit the **Ladder Middle-Zone Singularity**:
   when a weak barrier is hit mid-month (or an earlier date rung expires NO), one leg
   alone reaches ``>= 0.992`` bid (or resolves to ``$1.00``), ALREADY covering 100% of
   the hold's cost + base profit with zero leg risk, while the sibling leg is still
   live and bidding ``$0.20-$0.85``. Selling/redeeming the winning leg first and
   immediately harvesting the live sibling leg turns a +1.5% risk-free trade into a
   +20% to +85% realized windfall AND frees capital weeks early!
3. ``reconcile_holds`` syncs ``state.holds`` with actual on-chain conditional token
   balances so externally sold/redeemed positions never block ``hold_exposure``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from typing import Any

import httpx

from polyarb.book import take
from polyarb.config import Config
from polyarb.data.gamma import GAMMA_URL, parse_event
from polyarb.data.store import BookStore
from polyarb.execution.base import Gateway
from polyarb.journal import Journal
from polyarb.models import FeeSchedule, Outcome, Side
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
        store: BookStore | None = None,
    ) -> None:
        self.cfg = cfg
        self.gateway = gateway
        self.risk = risk
        self.journal = journal
        self.wallet_funder = wallet_funder
        self.store: BookStore | None = store if store is not None else getattr(gateway, "books", None)
        self._own_http = http is None
        self._http = http or httpx.AsyncClient(timeout=20.0)
        self._task: asyncio.Task | None = None
        self._last_paper_redeem_at: float = 0.0
        self.flips_completed = 0
        self.early_harvests = 0
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
        await self._harvest_decided_holds()
        if self.gateway.mode == "live":
            if self.cfg.execution.auto_redeem:
                await self._auto_redeem_live()
            await self._reconcile_external()
        elif self.cfg.execution.auto_redeem and self.risk.state.holds:
            now = time.monotonic()
            has_fast_short = any(
                h.strategy in ("crypto_short", "maker_crypto_short", "maker_crypto_pair")
                for h in self.risk.state.holds
            )
            redeem_gap = 12.0 if has_fast_short else 60.0
            if now - self._last_paper_redeem_at >= redeem_gap:
                self._last_paper_redeem_at = now
                await self._auto_redeem_paper()

    async def _sell_sibling_legs(self, hold: HoldPosition, skip_tokens: set[str]) -> float:
        """Liquidate remaining live legs into resting bids once principal + profit is already banked."""
        if self.store is None or not hasattr(self.gateway, "sell"):
            return 0.0
        bonus = 0.0
        for leg in hold.legs:
            tok = str(leg.get("token") or "")
            if not tok or tok in skip_tokens:
                continue
            yes_tok = str(leg.get("yes_token") or tok)
            outcome = Outcome(str(leg.get("outcome") or "YES"))
            shares = float(leg.get("shares") or 0.0)
            min_sz = float(leg.get("min_size") or 5.0)
            tick = float(leg.get("tick") or 0.01)
            if shares < min_sz - 1e-6:
                continue
            book = self.store.raw(yes_tok)
            if book is None:
                continue
            levels = book.bids if outcome is Outcome.YES else book.no_bids()
            if not levels:
                continue
            fee = FeeSchedule(
                rate=float(leg.get("fee_rate") or 0.0),
                exponent=float(leg.get("fee_exponent") or 1.0),
            )
            est = take(levels, shares, fee)
            if est.shares <= 0.05 or (est.notional - est.fee) < 0.05:
                continue
            sr = await self.gateway.sell(tok, outcome, yes_tok, shares, tick, fee, tick)
            if sr.ok and sr.shares > 0:
                bonus += sr.net
        return bonus

    async def _harvest_decided_holds(self) -> None:
        """Atomic early exit on multi-leg holds when 1 dominant leg covers total cost + profit (plus sibling bonus!)."""
        ecfg = self.cfg.execution
        if not ecfg.early_exit_enabled or self.store is None or not hasattr(self.gateway, "sell"):
            return
        if self.risk.state.killed or (self.cfg.risk.kill_file and _file_exists(self.cfg.risk.kill_file)):
            return

        min_bid = ecfg.early_exit_min_bid
        min_profit = ecfg.early_exit_min_profit_usd

        for hold in list(self.risk.state.holds):
            if len(hold.legs) < 2:
                continue
            target_net = hold.cost + min_profit
            dominant_candidate: tuple[dict, Outcome, float, FeeSchedule, float] | None = None

            for leg in hold.legs:
                tok = str(leg.get("token") or "")
                yes_tok = str(leg.get("yes_token") or tok)
                outcome = Outcome(str(leg.get("outcome") or "YES"))
                shares = float(leg.get("shares") or 0.0)
                min_sz = float(leg.get("min_size") or 5.0)
                tick = float(leg.get("tick") or 0.01)
                if not tok or shares < min_sz - 1e-6:
                    continue
                book = self.store.raw(yes_tok)
                if book is None:
                    continue
                levels = book.bids if outcome is Outcome.YES else book.no_bids()
                if not levels or levels[0].price < min_bid - 1e-9:
                    continue
                good = [lvl for lvl in levels if lvl.price >= min_bid - 1e-9]
                fee = FeeSchedule(
                    rate=float(leg.get("fee_rate") or 0.0),
                    exponent=float(leg.get("fee_exponent") or 1.0),
                )
                fill = take(good, shares, fee)
                if fill.shares < shares - 0.05 or (fill.notional - fill.fee) < target_net - 1e-6:
                    continue
                floor_px = max(
                    min_bid,
                    math.ceil((target_net / shares) / tick - 1e-9) * tick,
                )
                if floor_px >= 1.0:
                    continue
                if dominant_candidate is None or (fill.notional - fill.fee) > dominant_candidate[4]:
                    dominant_candidate = (leg, outcome, floor_px, fee, fill.notional - fill.fee)

            if dominant_candidate is None:
                continue

            dom_leg, dom_out, floor_px, dom_fee, _ = dominant_candidate
            dom_tok = str(dom_leg["token"])
            dom_yes = str(dom_leg.get("yes_token") or dom_tok)
            dom_sh = float(dom_leg["shares"])
            dom_tick = float(dom_leg.get("tick") or 0.01)

            if hold.exit_order_id:
                await self.gateway.cancel(hold.exit_order_id)
                hold.exit_order_id = None

            dom_sell = await self.gateway.sell(dom_tok, dom_out, dom_yes, dom_sh, floor_px, dom_fee, dom_tick)
            if not dom_sell.ok or dom_sell.shares < dom_sh - 0.05:
                continue
            # Principal + base profit is now 100% locked in cash; harvest any live middle-zone sibling legs!
            bonus = await self._sell_sibling_legs(hold, {dom_tok})
            total_proceeds = dom_sell.net + bonus
            pnl = self.risk.close_hold(hold, total_proceeds)
            self.early_harvests += 1
            self.journal.write(
                "early_harvest",
                {
                    "key": hold.key,
                    "title": hold.title,
                    "strategy": hold.strategy,
                    "primary_usd": round(dom_sell.net, 4),
                    "bonus_usd": round(bonus, 4),
                    "proceeds": round(total_proceeds, 4),
                    "pnl": round(pnl, 4),
                },
            )
            log.info(
                "EARLY HARVEST %s primary=$%.2f bonus=$%.2f pnl=$%.3f",
                hold.title[:48],
                dom_sell.net,
                bonus,
                pnl,
            )
            await self.sync_wallet_balance()

    async def _manage_flip_orders(self) -> None:
        if not (self.cfg.metar.flip_enabled or self.cfg.binance.flip_enabled or self.cfg.crypto_short.flip_enabled):
            return
        changed = False
        for hold in list(self.risk.state.holds):
            if hold.strategy not in (
                "metar_lock",
                "binance_lock",
                "crypto_short",
                "maker_metar",
                "maker_binance",
                "maker_crypto_short",
            ) or len(hold.legs) != 1:
                continue
            if hold.strategy in ("metar_lock", "maker_metar") and not self.cfg.metar.flip_enabled:
                continue
            if hold.strategy in ("binance_lock", "maker_binance") and not self.cfg.binance.flip_enabled:
                continue
            if hold.strategy in ("crypto_short", "maker_crypto_short") and not self.cfg.crypto_short.flip_enabled:
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
            matched_tokens: set[str] = set()
            for leg in matched_legs:
                tok = str(leg["token"])
                matched_tokens.add(tok)
                cond, val = redeemables[tok]
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
                # If a middle-zone ladder rung redeemed early while a sibling leg is still live, monetize it too!
                bonus = 0.0
                if total_redeemed >= hold.cost - 0.01 and len(matched_tokens) < len(hold.legs):
                    bonus = await self._sell_sibling_legs(hold, matched_tokens)
                total_proceeds = total_redeemed + bonus
                pnl = self.risk.close_hold(hold, total_proceeds)
                self.redemptions += 1
                self.journal.write(
                    "redeemed",
                    {
                        "key": hold.key,
                        "title": hold.title,
                        "strategy": hold.strategy,
                        "proceeds": round(total_proceeds, 4),
                        "bonus_usd": round(bonus, 4),
                        "pnl": round(pnl, 4),
                        "txs": txs,
                    },
                )
                log.info(
                    "AUTO-REDEEMED %s proceeds=$%.2f (bonus=$%.2f) pnl=$%.3f",
                    hold.title[:50],
                    total_proceeds,
                    bonus,
                    pnl,
                )
                await self.sync_wallet_balance()

    async def _auto_redeem_paper(self) -> None:
        """Settle paper holds against real Gamma resolution outcomes (including partial-win ladder windfalls & twin events)."""
        by_event: dict[str, Any] = {}
        for hold in list(self.risk.state.holds):
            if not hold.event_id or not hold.legs:
                continue
            ev_ids = [hold.event_id, *(hold.extra_event_ids or ())]
            markets = []
            fetch_ok = True
            for eid in dict.fromkeys(ev_ids):
                if eid not in by_event:
                    try:
                        resp = await self._http.get(f"{GAMMA_URL}/events/{eid}")
                        by_event[eid] = parse_event(resp.json()) if resp.status_code == 200 else None
                    except Exception:
                        by_event[eid] = None
                ev = by_event.get(eid)
                if ev is None:
                    fetch_ok = False
                    break
                markets.extend(ev.markets)
            if not fetch_ok or not markets:
                continue

            mkt_by_id = {m.id: m for m in markets}
            mkt_by_tok = {t: m for m in markets for t in (m.yes_token, m.no_token)}
            resolved_all = True
            resolved_tokens: set[str] = set()
            proceeds = 0.0
            for leg in hold.legs:
                tok = str(leg.get("token") or "")
                m = mkt_by_id.get(str(leg.get("market_id") or "")) or mkt_by_tok.get(tok)
                if m is None or not m.closed or m.resolved_outcome not in ("YES", "NO"):
                    resolved_all = False
                    continue
                resolved_tokens.add(tok)
                if m.resolved_outcome == str(leg.get("outcome") or "").upper():
                    proceeds += float(leg.get("shares") or 0.0)

            # Settle either when all legs resolved OR when resolved winning leg(s) already cover full floor!
            early_win_locked = (not resolved_all) and proceeds >= max(hold.cost, hold.payout - 0.01)
            if not resolved_all and not early_win_locked:
                continue
            if hold.exit_order_id:
                await self.gateway.cancel(hold.exit_order_id)
                hold.exit_order_id = None
            bonus = 0.0
            if early_win_locked:
                bonus = await self._sell_sibling_legs(hold, resolved_tokens)
            total_proceeds = proceeds + bonus
            pnl = self.risk.close_hold(hold, total_proceeds)
            self.redemptions += 1
            self.journal.write(
                "redeemed",
                {
                    "key": hold.key,
                    "title": hold.title,
                    "strategy": hold.strategy,
                    "proceeds": round(total_proceeds, 4),
                    "bonus_usd": round(bonus, 4),
                    "pnl": round(pnl, 4),
                    "txs": ["paper-gamma-verified"],
                },
            )
            log.info(
                "PAPER AUTO-REDEEMED %s proceeds=$%.2f (bonus=$%.2f) pnl=$%.3f",
                hold.title[:50],
                total_proceeds,
                bonus,
                pnl,
            )

    async def _reconcile_external(self) -> None:
        if not self.risk.state.holds:
            return
        open_tokens: set[str] | None = await self._fetch_open_position_tokens()
        if open_tokens is not None:
            async def _check_tok(token_id: str) -> float:
                return 1.0 if token_id in open_tokens else 0.0
        elif hasattr(self.gateway, "position"):
            _check_tok = self.gateway.position
        else:
            return
        removed: list[HoldPosition] = await self.risk.reconcile_holds(_check_tok)
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

    async def _fetch_open_position_tokens(self) -> set[str] | None:
        """Return token_ids with active open or winning-redeemable value (> $0.05), or None on API error."""
        if not self.wallet_funder:
            return None
        try:
            resp = await self._http.get(
                f"{DATA_API_URL}/v2/positions",
                params={"user": self.wallet_funder, "limit": 500},
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
            if (body.get("pagination") or {}).get("has_more"):
                return None
            rows = body.get("data") or []
        except Exception:
            return None
        active: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            tok = str(row.get("token_id") or "")
            val = float(row.get("current_value") or 0.0)
            status = str(row.get("status") or "").upper()
            if tok and (val >= 0.05 or status not in ("REDEEMABLE", "CLOSED")):
                active.add(tok)
        return active

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
            "early_harvests": self.early_harvests,
            "redemptions": self.redemptions,
            "reconciled": self.reconciled,
            "wallet_balance": None if self.risk.wallet_balance is None else round(self.risk.wallet_balance, 2),
        }


def _file_exists(path: str) -> bool:
    import os

    return bool(path and os.path.exists(path))
