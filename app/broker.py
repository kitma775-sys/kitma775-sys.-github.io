from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.hunter import Setup


@dataclass
class FillResult:
    ok: bool
    status: str
    mode: str
    detail: str = ""
    payload: dict[str, Any] | None = None


class PaperBroker:
    mode = "paper"

    async def execute_pair(self, setup: Setup) -> FillResult:
        if setup.kind == "maker":
            return FillResult(
                ok=True,
                status="paper_resting",
                mode="paper",
                detail="紙盤記錄 maker 掛單，唔當即時成交",
                payload={"kind": setup.kind, "shares": setup.shares},
            )
        return FillResult(
            ok=True,
            status="paper_filled",
            mode="paper",
            detail=f"紙盤成交 {setup.shares:.1f} 對 @ {setup.up_price}+{setup.down_price}",
            payload={"kind": setup.kind, "shares": setup.shares, "net": setup.net},
        )

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
                    price=f"{price:.2f}",
                    size=f"{setup.shares:.2f}",
                )
                if setup.kind == "maker":
                    kwargs["post_only"] = True
                resp = await client.place_limit_order(**kwargs)
                ok = bool(getattr(resp, "ok", False))
                results.append({"token": token, "ok": ok, "status": getattr(resp, "status", None), "id": getattr(resp, "order_id", None)})
                if not ok:
                    return FillResult(False, "rejected", "live", str(getattr(resp, "message", "order rejected")), {"legs": results})
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
