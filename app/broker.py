from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.hunter import Setup
from app.paper_sim import simulate_taker


@dataclass
class FillResult:
    ok: bool
    status: str
    mode: str
    detail: str = ""
    payload: dict[str, Any] | None = None


def paper_execute(setup: Setup) -> FillResult:
    """Synchronous paper fill rules. Maker never assumed-fills."""
    if setup.kind == "taker":
        slip = int(setup.extra.get("paper_slip_ticks") or 0)
        sim = simulate_taker(setup, slip_ticks=slip)
        payload = {
            "kind": "taker",
            "shares": setup.shares,
            "up_price": sim.up_price,
            "down_price": sim.down_price,
            "net": sim.net,
            "cost": sim.cost,
            "fees": sim.fees,
            "slipped": sim.slipped,
            "assumed_fill": False,
            "orders": setup_buy_orders(setup),
        }
        if not sim.ok:
            return FillResult(
                False,
                "paper_missed",
                "paper",
                f"紙盤 taker 掃唔到正期望：{sim.up_price}+{sim.down_price} 淨利 ${sim.net:.2f}",
                payload,
            )
        return FillResult(
            True,
            "paper_filled",
            "paper",
            (
                f"紙盤 taker 成交 {setup.shares:.1f} 對 @ "
                f"{sim.up_price}+{sim.down_price} 成本 ${sim.cost:.2f}"
                + (
                    f" 未結算期望 ${sim.net:.2f}"
                    if str((setup.extra or {}).get("strategy") or "") in {"twap", "favorite"}
                    else f" 淨利 ${sim.net:.2f}"
                )
            ),
            payload,
        )
    reserved = round(float(setup.up_price) * setup.shares + float(setup.down_price) * setup.shares, 6)
    return FillResult(
        True,
        "paper_resting",
        "paper",
        (
            f"紙盤掛單 {setup.shares:.1f} 對 @ {setup.up_price}+{setup.down_price}，"
            f"鎖 ${reserved:.2f}，等到盤口碰到先成交"
        ),
        {
            "kind": "maker",
            "shares": setup.shares,
            "net": setup.net,
            "cost": setup.cost,
            "reserved": reserved,
            "assumed_fill": False,
        },
    )


def already_redeemed(detail: str) -> bool:
    """True when the CLOB/relayer has nothing left to redeem for this condition."""
    text = (detail or "").lower()
    needles = (
        "you have no positions",
        "no positions",
        "nothing to redeem",
        "already redeem",
        "no outcome tokens",
        "balance is zero",
        "insufficient token",
    )
    return any(n in text for n in needles)


def buy_fak_kwargs(*, token_id: str, price: float, shares: float) -> dict:
    """Official CLOB BUY FAK: USDC `amount` + `max_price`. Shares are illegal on BUY."""
    px = max(float(price), 0.01)
    sh = max(float(shares), 0.0)
    amount = round(sh * px, 4)
    return {
        "token_id": str(token_id),
        "side": "BUY",
        "amount": f"{amount:.4f}",
        "max_price": f"{px:.4f}",
        "order_type": "FAK",
    }


def sell_fak_kwargs(*, token_id: str, shares: float, min_price: float) -> dict:
    """Official CLOB SELL FAK: `shares` + `min_price`. Amount is illegal on SELL."""
    return {
        "token_id": str(token_id),
        "side": "SELL",
        "shares": f"{max(float(shares), 0.0):.2f}",
        "min_price": f"{max(float(min_price), 0.01):.4f}",
        "order_type": "FAK",
    }


def order_fill_amounts(resp, *, side: str, price: float, shares: float) -> dict:
    """Read actual FAK fill from an AcceptedOrder. Mocks without amounts keep the request size."""
    taking = getattr(resp, "taking_amount", None)
    making = getattr(resp, "making_amount", None)
    try:
        taking_f = None if taking is None else float(taking)
        making_f = None if making is None else float(making)
    except (TypeError, ValueError):
        taking_f = making_f = None
    side_u = str(side or "").upper()
    req = max(float(shares), 0.0)
    px = max(float(price), 0.01)
    if side_u == "SELL":
        filled = making_f if making_f is not None else req
        cash = taking_f if taking_f is not None else round(filled * px, 6)
        return {"shares": round(max(filled, 0.0), 6), "proceeds": round(max(cash, 0.0), 6), "cost": 0.0}
    filled = taking_f if taking_f is not None else req
    spent = making_f if making_f is not None else round(filled * px, 6)
    return {"shares": round(max(filled, 0.0), 6), "cost": round(max(spent, 0.0), 6), "proceeds": 0.0}


def setup_buy_orders(setup: Setup) -> list[dict]:
    legs = []
    if float(setup.up_price) >= 0.01:
        legs.append(buy_fak_kwargs(token_id=setup.up_token, price=setup.up_price, shares=setup.shares))
    if float(setup.down_price) >= 0.01:
        legs.append(buy_fak_kwargs(token_id=setup.down_token, price=setup.down_price, shares=setup.shares))
    return legs


class PaperBroker:
    mode = "paper"

    async def execute_pair(self, setup: Setup) -> FillResult:
        return paper_execute(setup)

    async def merge(self, condition_id: str, shares: float) -> FillResult:
        return FillResult(True, "merged", "paper", f"紙盤 merge {shares:.1f}", {"shares": shares})

    async def redeem(self, condition_id: str) -> FillResult:
        return FillResult(True, "paper_settled", "paper", "紙盤 redeem 入帳", {"condition_id": condition_id})

    async def list_redeemable(self) -> list[dict]:
        return []

    async def execute_sell(self, token_id: str, shares: float, min_price: float) -> FillResult:
        order = sell_fak_kwargs(token_id=token_id, shares=shares, min_price=min_price)
        payload = {"token": token_id, "min_price": min_price, **order}
        payload["shares"] = float(shares)
        return FillResult(
            True,
            "paper_dumped",
            "paper",
            f"紙盤 dump {shares:.1f} @{min_price}",
            payload,
        )


class LiveBroker:
    mode = "live"

    def __init__(self, private_key: str):
        self.private_key = private_key
        self._client = None

    async def _client_ready(self):
        if self._client is not None:
            return self._client
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:
            raise RuntimeError("未安裝 polymarket-client，無法實盤") from exc
        self._client = await AsyncSecureClient.create(private_key=self.private_key)
        return self._client

    async def execute_pair(self, setup: Setup) -> FillResult:
        client = await self._client_ready()
        results: list[dict] = []
        try:
            legs = [
                (token, price)
                for token, price in ((setup.up_token, setup.up_price), (setup.down_token, setup.down_price))
                if float(price) >= 0.01
            ]
            if not legs:
                return FillResult(False, "rejected", "live", "no priced legs", {"legs": []})
            for token, price in legs:
                if setup.kind == "maker":
                    resp = await client.place_limit_order(
                        token_id=token,
                        side="BUY",
                        price=f"{price:.4f}",
                        size=f"{setup.shares:.2f}",
                        post_only=True,
                    )
                else:
                    kw = buy_fak_kwargs(token_id=token, price=price, shares=setup.shares)
                    resp = await client.place_market_order(**kw)
                ok = bool(getattr(resp, "ok", False))
                status = str(getattr(resp, "status", "") or getattr(resp, "code", "") or "").lower()
                order_id = getattr(resp, "order_id", None)
                results.append(
                    {
                        "token": token,
                        "ok": ok,
                        "status": getattr(resp, "status", None) or getattr(resp, "code", None),
                        "id": order_id,
                        "message": str(getattr(resp, "message", "") or ""),
                    }
                )
                if not ok:
                    return FillResult(
                        False,
                        "rejected",
                        "live",
                        str(getattr(resp, "message", status or "order rejected")),
                        {"legs": results},
                    )
                fill = order_fill_amounts(resp, side="BUY", price=price, shares=setup.shares)
                results[-1]["shares"] = fill["shares"]
                results[-1]["cost"] = fill["cost"]
                if setup.kind == "taker" and status != "matched":
                    if status == "live" and order_id:
                        try:
                            await client.cancel_order(order_id=str(order_id))
                        except Exception:
                            try:
                                await client.cancel_all()
                            except Exception:
                                pass
                    return FillResult(
                        False,
                        "rejected",
                        "live",
                        f"taker FAK not matched ({status})",
                        {"legs": results},
                    )
                if setup.kind == "taker" and fill["shares"] <= 0.01:
                    return FillResult(
                        False,
                        "rejected",
                        "live",
                        "taker FAK matched 0 shares",
                        {"legs": results},
                    )
        except Exception as exc:
            return FillResult(False, "error", "live", str(exc)[:300], {"legs": results})
        filled_shares = float(results[-1].get("shares") or setup.shares) if results else float(setup.shares)
        filled_cost = float(results[-1].get("cost") or 0.0) if results else 0.0
        return FillResult(
            True,
            "filled" if setup.kind == "taker" else "resting",
            "live",
            "已提交",
            {
                "legs": results,
                "orders": setup_buy_orders(setup) if setup.kind == "taker" else [],
                "shares": filled_shares,
                "cost": filled_cost,
            },
        )

    async def execute_sell(self, token_id: str, shares: float, min_price: float) -> FillResult:
        client = await self._client_ready()
        kw = sell_fak_kwargs(token_id=token_id, shares=shares, min_price=min_price)
        try:
            resp = await client.place_market_order(**kw)
        except Exception as exc:
            return FillResult(False, "error", "live", str(exc)[:300], {"order": kw})
        ok = bool(getattr(resp, "ok", False))
        status = str(getattr(resp, "status", "") or getattr(resp, "code", "") or "").lower()
        order_id = getattr(resp, "order_id", None)
        payload = {
            "order": kw,
            "ok": ok,
            "status": getattr(resp, "status", None) or getattr(resp, "code", None),
            "id": order_id,
            "message": str(getattr(resp, "message", "") or ""),
        }
        fill = order_fill_amounts(resp, side="SELL", price=min_price, shares=shares)
        payload.update(fill)
        if not ok:
            return FillResult(False, "rejected", "live", str(getattr(resp, "message", status or "sell rejected")), payload)
        if status != "matched":
            if status == "live" and order_id:
                try:
                    await client.cancel_order(order_id=str(order_id))
                except Exception:
                    try:
                        await client.cancel_all()
                    except Exception:
                        pass
            return FillResult(False, "rejected", "live", f"sell FAK not matched ({status})", payload)
        if fill["shares"] <= 0.01:
            return FillResult(False, "rejected", "live", "sell FAK matched 0 shares", payload)
        return FillResult(True, "dumped", "live", "已出貨", payload)

    async def merge(self, condition_id: str, shares: float) -> FillResult:
        client = await self._client_ready()
        try:
            tx = await client.merge_positions(condition_id=condition_id, amount="max")
            await tx.wait()
            return FillResult(True, "merged", "live", "merge 完成", {"condition_id": condition_id})
        except Exception as exc:
            return FillResult(False, "merge_error", "live", str(exc)[:300], {})

    async def redeem(self, condition_id: str) -> FillResult:
        cid = str(condition_id or "").strip()
        if not cid:
            return FillResult(False, "redeem_error", "live", "missing condition_id", {})
        try:
            client = await self._client_ready()
            tx = await client.redeem_positions(condition_id=cid)
            await tx.wait()
            return FillResult(True, "redeemed", "live", "redeem 完成", {"condition_id": cid})
        except Exception as exc:
            detail = str(exc)[:300]
            if already_redeemed(detail):
                return FillResult(
                    True,
                    "redeemed",
                    "live",
                    "already empty",
                    {"condition_id": cid, "already": True},
                )
            return FillResult(False, "redeem_error", "live", detail, {"condition_id": cid})

    async def list_redeemable(self) -> list[dict]:
        """Wallet positions the data API marks redeemable. Empty on auth/network failure."""
        try:
            client = await self._client_ready()
            paginator = client.list_positions(redeemable=True, page_size=50)
            found: dict[str, dict] = {}
            n = 0
            async for pos in paginator.iter_items():
                cid = str(getattr(pos, "condition_id", "") or "")
                if cid and cid not in found:
                    found[cid] = {
                        "condition_id": cid,
                        "slug": str(getattr(pos, "event_slug", None) or getattr(pos, "slug", None) or ""),
                        "size": float(getattr(pos, "size", 0) or 0),
                    }
                n += 1
                if n >= 80:
                    break
            return list(found.values())
        except Exception:
            return []

    async def cancel_open_orders(self) -> int:
        try:
            client = await self._client_ready()
            resp = await client.cancel_all()
            canceled = getattr(resp, "canceled", ()) or ()
            return len(tuple(canceled))
        except Exception:
            return 0
