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
                f"{sim.up_price}+{sim.down_price} 成本 ${sim.cost:.2f} 淨利 ${sim.net:.2f}"
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


class PaperBroker:
    mode = "paper"

    async def execute_pair(self, setup: Setup) -> FillResult:
        return paper_execute(setup)

    async def merge(self, condition_id: str, shares: float) -> FillResult:
        return FillResult(True, "merged", "paper", f"紙盤 merge {shares:.1f}", {"shares": shares})


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
        results = []
        try:
            for token, price in ((setup.up_token, setup.up_price), (setup.down_token, setup.down_price)):
                kwargs = dict(
                    token_id=token,
                    side="BUY",
                    price=f"{price:.4f}",
                    size=f"{setup.shares:.2f}",
                    order_type="FAK",  # live default taker; unused while FORCE_PAPER
                )
                if setup.kind == "maker":
                    kwargs.pop("order_type", None)
                    kwargs["post_only"] = True
                try:
                    resp = await client.place_limit_order(**kwargs)
                except TypeError:
                    kwargs.pop("order_type", None)
                    resp = await client.place_limit_order(**kwargs)
                ok = bool(getattr(resp, "ok", False))
                status = str(getattr(resp, "status", "") or "").lower()
                results.append({"token": token, "ok": ok, "status": getattr(resp, "status", None), "id": getattr(resp, "order_id", None)})
                if not ok or (setup.kind == "taker" and status in {"live", "unmatched", "delayed"}):
                    return FillResult(False, "rejected", "live", str(getattr(resp, "message", status or "order rejected")), {"legs": results})
        except Exception as exc:
            return FillResult(False, "error", "live", str(exc)[:300], {"legs": results})
        return FillResult(True, "filled" if setup.kind == "taker" else "resting", "live", "兩邊已提交", {"legs": results})

    async def merge(self, condition_id: str, shares: float) -> FillResult:
        client = await self._client_ready()
        try:
            tx = await client.merge_positions(condition_id=condition_id, amount="max")
            await tx.wait()
            return FillResult(True, "merged", "live", "merge 完成", {"condition_id": condition_id})
        except Exception as exc:
            return FillResult(False, "merge_error", "live", str(exc)[:300], {})
