"""Polymarket RTDS Chainlink USD ticks — 5m up/down settlement stream.

wss://ws-live-data.polymarket.com  topic=crypto_prices_chainlink
No auth. PING every 5s. Symbols are slash pairs (btc/usd). One subscribe
frame per symbol.

Window-open PTB is the first tick at or after the 5m start on this
same stream. Running TWAP is time-weighted over the last `lookback` seconds.
15m leftover inventory can still snapshot; 1H Binance candles are not this topic.
"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.twap import (
    HORIZON_SECONDS,
    TWAP_LOOKBACK,
    TwapSnap,
    asset_from_symbol,
    fair_p_up,
    lead_bps,
    parse_window,
    time_weighted_twap,
)

RTDS_URL = "wss://ws-live-data.polymarket.com"
CHAINLINK_TOPIC = "crypto_prices_chainlink"
CHAINLINK_TOPICS = {"crypto_prices_chainlink", "crypto_prices", "prices.crypto.chainlink"}
KEEP_SECONDS = 1800.0
PING_EVERY = 5.0


def _unix(ts) -> float:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return time.time()
    if v > 1e12:
        return v / 1000.0
    return v


@dataclass
class Tick:
    ts: float
    price: float


class ChainlinkTape:
    def __init__(self, symbols: tuple[str, ...] = ("btc/usd", "eth/usd")):
        self.symbols = tuple(symbols)
        self.ticks: dict[str, deque[Tick]] = defaultdict(lambda: deque(maxlen=4000))
        self.ptb: dict[str, float] = {}
        self.last_recv: dict[str, float] = {}
        self.connected = False
        self.last_msg_ts = 0.0
        self.last_error = ""
        self.msg_n = 0
        self.persist_ptb = None

    def subscribe_frame_for(self, symbol: str) -> str:
        return json.dumps(
            {
                "action": "subscribe",
                "subscriptions": [
                    {
                        "topic": CHAINLINK_TOPIC,
                        "type": "*",
                        "filters": json.dumps({"symbol": symbol}, separators=(",", ":")),
                    }
                ],
            },
            separators=(",", ":"),
        )

    def subscribe_frames(self) -> list[str]:
        # Compact filters are required. `json.dumps` default spacing
        # (`{"symbol": "btc/usd"}`) only gets a snapshot, no live updates.
        # One symbol per *socket* at runtime: many frames on one RTDS
        # connection freeze every feed except one after a few minutes.
        return [self.subscribe_frame_for(sym) for sym in self.symbols]

    def subscribe_frame(self) -> str:
        frames = self.subscribe_frames()
        return frames[0] if frames else "{}"

    def age_ms(self, symbol: str | None = None) -> float:
        """Receive-age of the feed, not exchange-print age.

        Tick timestamps can lag wall clock; using them marked live XRP as
        fresh and frozen BTC as 348s stale even while last_msg_ts was 1s.
        """
        if symbol:
            recv = self.last_recv.get(symbol)
            if not recv:
                return 9e9
            return max(0.0, (time.time() - recv) * 1000.0)
        if not self.last_msg_ts:
            return 9e9
        return max(0.0, (time.time() - self.last_msg_ts) * 1000.0)

    def apply_message(self, raw) -> bool:
        if raw is None:
            return False
        if isinstance(raw, dict):
            msg = raw
        else:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", "replace")
            text = str(raw).strip()
            if not text or text in {"PONG", "PING"}:
                return False
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                return False
        if not isinstance(msg, dict):
            return False
        if str(msg.get("type") or "").lower() == "error":
            return False
        topic = str(msg.get("topic") or "")
        if topic and topic not in CHAINLINK_TOPICS:
            return False
        payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
        sym = str(payload.get("symbol") or payload.get("pair") or "").lower()
        if not sym or "/" not in sym:
            return False
        rows: list[tuple[float, float]] = []
        data = payload.get("data")
        if isinstance(data, list):
            for item in data:
                if not isinstance(item, dict):
                    continue
                try:
                    px = float(item.get("value") or item.get("price") or 0)
                except (TypeError, ValueError):
                    continue
                if px <= 0:
                    continue
                rows.append((_unix(item.get("timestamp") or payload.get("timestamp") or msg.get("timestamp")), px))
        else:
            try:
                px = float(payload.get("value") or payload.get("price") or 0)
            except (TypeError, ValueError):
                px = 0.0
            if px > 0:
                rows.append((_unix(payload.get("timestamp") or msg.get("timestamp") or time.time()), px))
        if not rows:
            return False
        rows.sort(key=lambda x: x[0])
        q = self.ticks[sym]
        for ts, px in rows:
            q.append(Tick(ts, px))
            self._maybe_ptb(sym, ts, px)
        cutoff = rows[-1][0] - KEEP_SECONDS
        while q and q[0].ts < cutoff:
            q.popleft()
        now = time.time()
        self.last_recv[sym] = now
        self.last_msg_ts = now
        self.msg_n += 1
        return True

    def _window_key(self, asset: str, horizon: str, start: int) -> str:
        return f"{asset}-updown-{horizon}-{int(start)}"

    def _maybe_ptb(self, symbol: str, ts: float, px: float) -> None:
        asset = asset_from_symbol(symbol)
        if not asset:
            return
        win = int(HORIZON_SECONDS["5m"])
        start = int(ts) - (int(ts) % win)
        self.ensure_ptb(self._window_key(asset, "5m", start))

    def ensure_ptb(self, slug: str) -> float | None:
        """PTB = first tick at/after T0, only if we saw a tick *before* T0.

        Joining mid-window would otherwise treat the first live print as the
        open. Skip until the next window open.
        """
        parsed = parse_window(slug)
        if not parsed:
            return None
        key = parsed.slug
        if key in self.ptb:
            return self.ptb[key]
        symbol = parsed.symbol
        if not symbol:
            return None
        q = list(self.ticks.get(symbol) or ())
        if not any(t.ts < parsed.start - 1e-9 for t in q):
            return None
        after = next((t for t in q if t.ts + 1e-9 >= parsed.start), None)
        if after is None or after.ts > parsed.start + 5.0:
            return None
        self.ptb[key] = float(after.price)
        if self.persist_ptb is not None:
            try:
                self.persist_ptb(key, self.ptb[key])
            except Exception:
                pass
        return self.ptb[key]

    def load_ptb(self, mapping: dict[str, float]) -> int:
        """Restore window-open PTB after a restart. Slugs are unique per T0."""
        n = 0
        for slug, px in (mapping or {}).items():
            parsed = parse_window(str(slug or ""))
            try:
                price = float(px)
            except (TypeError, ValueError):
                continue
            if parsed is None or price <= 0:
                continue
            if parsed.slug not in self.ptb:
                self.ptb[parsed.slug] = price
                n += 1
        return n

    def _vol(self, symbol: str, now: float, window: int = 120) -> float | None:
        """1s log-return std in bps — same unit as research realized_vol_bps_sqrt_s."""
        q = self.ticks.get(symbol)
        if not q:
            return None
        start = now - window
        by_sec: dict[int, float] = {}
        for t in q:
            if t.ts < start - 2 or t.ts > now + 1e-9:
                continue
            by_sec[int(t.ts)] = t.price
        rets = []
        prev = None
        for sec in range(int(start), int(now) + 1):
            p = by_sec.get(sec)
            if p is None:
                continue
            if prev is not None and prev > 0 and p > 0:
                rets.append(math.log(p / prev))
            prev = p
        if len(rets) < 30:
            return None
        mu = sum(rets) / len(rets)
        var = sum((x - mu) ** 2 for x in rets) / max(len(rets) - 1, 1)
        std = math.sqrt(max(var, 0.0))
        return std * 10000.0

    def snapshot(self, slug: str, *, now: float | None = None, lookback: int = TWAP_LOOKBACK, left: float | None = None) -> TwapSnap | None:
        parsed = parse_window(slug)
        if not parsed:
            return None
        asset, start = parsed.asset, parsed.start
        symbol = parsed.symbol
        if not symbol:
            return None
        now = time.time() if now is None else float(now)
        q = self.ticks.get(symbol)
        if not q:
            return None
        ticks = [(t.ts, t.price) for t in q]
        tw = time_weighted_twap(ticks, now, lookback)
        spot = q[-1].price
        ptb = self.ensure_ptb(slug)
        if tw is None or ptb is None:
            return None
        lead = lead_bps(tw, ptb)
        if lead is None:
            return None
        vol = self._vol(symbol, now)
        if left is None:
            left = float(start + parsed.window_seconds) - now
        fair = fair_p_up(lead, vol, float(left), lookback=lookback)
        return TwapSnap(
            symbol=symbol,
            slug=slug,
            asset=asset,
            start=start,
            ptb=float(ptb),
            twap=float(tw),
            spot=float(spot),
            lead_bps=float(lead),
            vol_bps_sqrt_s=vol,
            fair_p_up=fair,
            lookback=int(lookback),
            age_ms=self.age_ms(symbol),
            tick_n=len(q),
            connected=bool(self.connected) and self.age_ms(symbol) < 8000,
        )

    def public(self) -> dict:
        out = {
            "connected": self.connected,
            "age_ms": None if not self.last_msg_ts else round(self.age_ms(), 1),
            "msg_n": self.msg_n,
            "error": self.last_error,
            "symbols": {},
        }
        for sym in self.symbols:
            q = self.ticks.get(sym)
            out["symbols"][sym] = {
                "n": 0 if not q else len(q),
                "px": None if not q else q[-1].price,
                "age_ms": None if not q else round(self.age_ms(sym), 1),
            }
        return out
