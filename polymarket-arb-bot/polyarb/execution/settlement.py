"""On-chain settlement helpers for v1 NegRisk markets (pUSD collateral era).

``NegRiskCtfCollateralAdapter.convertPositions(marketId, indexSet, amount)``
pulls ``amount`` NO tokens for every question whose bit is set in
``indexSet``, converts them through the legacy NegRiskAdapter, returns YES for
the remaining questions and wraps the released USDC.e into pUSD:

    pUSD out = (popcount(indexSet) - 1) * amount * (1 - feeBips / 10_000)

The wallet must have ``ConditionalTokens.setApprovalForAll(adapter, true)``,
which ``AsyncSecureClient.setup_trading_approvals()`` grants.
"""

from __future__ import annotations

import httpx

POLYGON_RPC = "https://polygon.drpc.org"
CONDITIONAL_TOKENS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
NEG_RISK_ADAPTER = "0xd91E80cF2E7be2e162c6513ceD06f1dD0dA35296"
NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xadA2005600Dec949baf300f4C6120000bDB6eAab"
PUSD = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"
PUSD_DECIMALS = 6

SEL_CONVERT_POSITIONS = "c64748c4"   # convertPositions(bytes32,uint256,uint256)
SEL_GET_FEE_BIPS = "2582cb5e"        # getFeeBips(bytes32)
SEL_GET_QUESTION_COUNT = "b7f75d2c"  # getQuestionCount(bytes32)
SEL_GET_POSITION_ID = "752b5ba5"     # getPositionId(bytes32,bool)


def _word(value: int) -> str:
    if value < 0 or value >= 1 << 256:
        raise ValueError("uint256 out of range")
    return value.to_bytes(32, "big").hex()


def _bytes32(hex_str: str) -> str:
    body = hex_str[2:] if hex_str.lower().startswith("0x") else hex_str
    if len(body) != 64:
        raise ValueError(f"expected 32-byte hex, got {len(body) // 2} bytes")
    bytes.fromhex(body)
    return body.lower()


def to_base_units(shares: float) -> int:
    return int(round(shares * 10**PUSD_DECIMALS))


def index_set(indices: list[int]) -> int:
    out = 0
    for i in indices:
        if not 0 <= i < 256:
            raise ValueError(f"question index {i} out of range")
        out |= 1 << i
    return out


def encode_convert_positions(market_id: str, index_set_value: int, amount_base_units: int) -> str:
    if index_set_value <= 0:
        raise ValueError("index set must be non-empty")
    if bin(index_set_value).count("1") < 2:
        raise ValueError("converting a single NO position returns nothing")
    if amount_base_units <= 0:
        raise ValueError("amount must be positive")
    return "0x" + SEL_CONVERT_POSITIONS + _bytes32(market_id) + _word(index_set_value) + _word(amount_base_units)


def expected_convert_proceeds(index_set_value: int, amount_base_units: int, fee_bips: int = 0) -> int:
    n = bin(index_set_value).count("1")
    return (n - 1) * (amount_base_units - amount_base_units * fee_bips // 10_000)


class ChainReader:
    """Minimal eth_call reader so paper mode needs no web3 dependency."""

    def __init__(self, rpc_url: str = POLYGON_RPC, http: httpx.AsyncClient | None = None) -> None:
        self.rpc_url = rpc_url
        self._own = http is None
        self._http = http or httpx.AsyncClient(timeout=15)

    async def close(self) -> None:
        if self._own:
            await self._http.aclose()

    async def call(self, to: str, data: str) -> str:
        resp = await self._http.post(
            self.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": "eth_call", "params": [{"to": to, "data": data}, "latest"]},
        )
        resp.raise_for_status()
        body = resp.json()
        if "error" in body:
            raise RuntimeError(f"eth_call failed: {body['error']}")
        return body["result"]

    async def fee_bips(self, market_id: str) -> int:
        return int(await self.call(NEG_RISK_ADAPTER, "0x" + SEL_GET_FEE_BIPS + _bytes32(market_id)), 16)

    async def question_count(self, market_id: str) -> int:
        return int(await self.call(NEG_RISK_ADAPTER, "0x" + SEL_GET_QUESTION_COUNT + _bytes32(market_id)), 16)

    async def position_id(self, question_id: str, outcome_yes: bool) -> int:
        data = "0x" + SEL_GET_POSITION_ID + _bytes32(question_id) + _word(1 if outcome_yes else 0)
        return int(await self.call(NEG_RISK_ADAPTER, data), 16)
