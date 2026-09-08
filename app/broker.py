from __future__ import annotations

import json
import math
import re
import urllib.request
from dataclasses import dataclass
from typing import Any

from app.config import format_share_qty, normalize_private_key
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
                f"紙盤 taker 成交 {format_share_qty(setup.shares)} @ "
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
            f"紙盤掛單 {format_share_qty(setup.shares)} @ {setup.up_price}+{setup.down_price}，"
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


def _exc_http_status(exc: BaseException):
    status = getattr(exc, "status", None)
    if status is None:
        status = getattr(exc, "status_code", None)
    resp = getattr(exc, "response", None)
    if status is None and resp is not None:
        status = getattr(resp, "status_code", None)
    return status


def _exc_retry_after(exc: BaseException):
    for attr in ("retry_after", "retry_after_seconds"):
        val = getattr(exc, attr, None)
        if val is not None:
            return val
    return None


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


def redeem_not_ready(detail: str) -> bool:
    """CLOB already delisted the 5m book, or gasless redeem needs a Builder key.

    Tokens are still in the Safe; Polymarket auto-redeems on-chain. Retrying
    redeem_positions every loop only spams the journal.
    """
    text = (detail or "").lower()
    needles = (
        "no market found",
        "market not found",
        "builder api key",
        "relayer api key",
        "too early",
        "not resolved",
        "not yet resolved",
        "condition not found",
    )
    return any(n in text for n in needles)


def _looks_eth_address(raw: str | None) -> bool:
    text = str(raw or "").strip()
    return text.startswith("0x") and len(text) == 42


def _data_api_position_size(user: str, condition_id: str) -> float | None:
    """Public positions. None = request failed; 0 = wallet no longer holds the cid."""
    addr = str(user or "").strip()
    cid = str(condition_id or "").strip().lower()
    if not _looks_eth_address(addr) or not cid:
        return None
    url = f"https://data-api.polymarket.com/positions?user={addr}&sizeThreshold=0"
    req = urllib.request.Request(url, headers={"User-Agent": "surf-arb"})
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return None
    if not isinstance(data, list):
        return None
    total = 0.0
    for row in data:
        if not isinstance(row, dict):
            continue
        other = str(row.get("conditionId") or row.get("condition_id") or "").strip().lower()
        if other == cid:
            try:
                total += float(row.get("size") or 0)
            except (TypeError, ValueError):
                continue
    return total


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


def clob_sell_shares(shares: float) -> float:
    """CLOB SELL is 2dp. Floor so fill dust cannot round the order above the wallet."""
    return math.floor(max(0.0, float(shares)) * 100.0 + 1e-12) / 100.0


def token_balance_shares(detail: str) -> float | None:
    """Parse `balance: 5489794, order amount: 5490000` (6-decimal outcome tokens)."""
    m = re.search(r"balance:\s*(\d+)\s*,\s*order amount:\s*(\d+)", str(detail or ""), re.I)
    if not m:
        return None
    try:
        return int(m.group(1)) / 1_000_000.0
    except (TypeError, ValueError):
        return None


def sell_size_dust(detail: str) -> bool:
    text = str(detail or "").lower()
    return "not enough balance" in text or "balance is not enough" in text


def sell_fak_kwargs(*, token_id: str, shares: float, min_price: float) -> dict:
    """Official CLOB SELL FAK: `shares` + `min_price`. Amount is illegal on SELL."""
    return {
        "token_id": str(token_id),
        "side": "SELL",
        "shares": f"{clob_sell_shares(shares):.2f}",
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
        return FillResult(True, "merged", "paper", f"紙盤 merge {format_share_qty(shares)}", {"shares": shares})

    async def redeem(self, condition_id: str) -> FillResult:
        return FillResult(True, "paper_settled", "paper", "紙盤 redeem 入帳", {"condition_id": condition_id})

    async def list_redeemable(self) -> list[dict]:
        return []

    async def collateral_usdc(self) -> float | None:
        return None

    async def execute_sell(self, token_id: str, shares: float, min_price: float) -> FillResult:
        order = sell_fak_kwargs(token_id=token_id, shares=shares, min_price=min_price)
        payload = {"token": token_id, "min_price": min_price, **order}
        payload["shares"] = float(shares)
        return FillResult(
            True,
            "paper_dumped",
            "paper",
            f"紙盤 dump {format_share_qty(shares)} @{min_price}",
            payload,
        )


class LiveBroker:
    mode = "live"

    def __init__(self, private_key: str, wallet: str | None = None):
        self.private_key = normalize_private_key(private_key)
        self.wallet = (wallet or "").strip() or None
        self._client = None

    async def _client_ready(self):
        if self._client is not None:
            return self._client
        try:
            from polymarket import AsyncSecureClient
        except ImportError as exc:
            raise RuntimeError("未安裝 polymarket-client，無法實盤") from exc
        kw: dict[str, Any] = {"private_key": self.private_key}
        if self.wallet:
            kw["wallet"] = self.wallet
        self._client = await AsyncSecureClient.create(**kw)
        return self._client

    async def collateral_usdc(self) -> float | None:
        client = await self._client_ready()
        bal = await client.get_balance_allowance(asset_type="COLLATERAL")
        return int(getattr(bal, "balance", 0) or 0) / 1_000_000

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
            payload: dict[str, Any] = {"legs": results}
            status_code = _exc_http_status(exc)
            if status_code is not None:
                payload["http_status"] = status_code
            retry_after = _exc_retry_after(exc)
            if retry_after is not None:
                payload["retry_after"] = retry_after
            return FillResult(False, "error", "live", str(exc)[:300], payload)
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
        size = clob_sell_shares(shares)
        if size < 0.01:
            return FillResult(False, "rejected", "live", "sell size 0", {"shares": shares})
        kw = sell_fak_kwargs(token_id=token_id, shares=size, min_price=min_price)
        try:
            resp = await client.place_market_order(**kw)
        except Exception as exc:
            detail = str(exc)
            held = token_balance_shares(detail)
            retry_sz = clob_sell_shares(held) if held is not None else 0.0
            if sell_size_dust(detail) and retry_sz >= 0.01 and retry_sz + 1e-12 < size:
                kw = sell_fak_kwargs(token_id=token_id, shares=retry_sz, min_price=min_price)
                size = retry_sz
                try:
                    resp = await client.place_market_order(**kw)
                except Exception as exc2:
                    return self._sell_exc(exc2, kw)
            else:
                return self._sell_exc(exc, kw)
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
        fill = order_fill_amounts(resp, side="SELL", price=min_price, shares=size)
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

    def _sell_exc(self, exc: BaseException, kw: dict) -> FillResult:
        payload: dict[str, Any] = {"order": kw}
        status_code = _exc_http_status(exc)
        if status_code is not None:
            payload["http_status"] = status_code
        retry_after = _exc_retry_after(exc)
        if retry_after is not None:
            payload["retry_after"] = retry_after
        return FillResult(False, "error", "live", str(exc)[:300], payload)

    async def merge(self, condition_id: str, shares: float) -> FillResult:
        client = await self._client_ready()
        try:
            tx = await client.merge_positions(condition_id=condition_id, amount="max")
            await tx.wait()
            return FillResult(True, "merged", "live", "merge 完成", {"condition_id": condition_id})
        except Exception as exc:
            return FillResult(False, "merge_error", "live", str(exc)[:300], {})

    def _wallet_address(self, client=None) -> str | None:
        if _looks_eth_address(self.wallet):
            return str(self.wallet).strip()
        obj = client if client is not None else self._client
        if obj is None:
            return None
        ctx = getattr(obj, "_ctx", None)
        wallet = getattr(ctx, "wallet", None) if ctx is not None else None
        for cand in (wallet, getattr(obj, "wallet", None), getattr(obj, "funder", None)):
            if cand is None:
                continue
            if isinstance(cand, str) and _looks_eth_address(cand):
                return cand.strip()
            for attr in ("address", "safe_address", "proxy_address", "funder"):
                val = getattr(cand, attr, None)
                if _looks_eth_address(str(val or "")):
                    return str(val).strip()
        return None

    async def condition_token_size(self, condition_id: str) -> float | None:
        """Shares still held for this condition. 0 means sqlite should settle."""
        cid = str(condition_id or "").strip()
        if not cid:
            return None
        client = self._client
        addr = self._wallet_address(client)
        if not addr and client is None:
            try:
                client = await self._client_ready()
            except Exception:
                client = None
            addr = self._wallet_address(client)
        if addr:
            held = _data_api_position_size(addr, cid)
            if held is not None:
                return held
        return None

    async def _held_or_none(self, cid: str) -> float | None:
        try:
            return await self.condition_token_size(cid)
        except Exception:
            return None

    async def redeem(self, condition_id: str) -> FillResult:
        cid = str(condition_id or "").strip()
        if not cid:
            return FillResult(False, "redeem_error", "live", "missing condition_id", {})
        held = await self._held_or_none(cid)
        if held is not None and held < 0.01:
            return FillResult(
                True,
                "redeemed",
                "live",
                "already empty",
                {"condition_id": cid, "already": True},
            )
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
            if held is None:
                held = await self._held_or_none(cid)
            if held is not None and held < 0.01:
                return FillResult(
                    True,
                    "redeemed",
                    "live",
                    "already empty",
                    {"condition_id": cid, "already": True},
                )
            if redeem_not_ready(detail):
                return FillResult(
                    False,
                    "redeem_wait",
                    "live",
                    detail,
                    {"condition_id": cid, "wait": True, "held": held},
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
