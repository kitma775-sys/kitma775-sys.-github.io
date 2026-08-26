"""In-memory CLOB books from the public market websocket.

HTTP 2s polls cannot win the 80–200ms MM requote window. While the socket
is up the last snapshot of each token *is* the live book until a delta
arrives. Hunt uses a long hold (60s) so a price_change on one leg can
still pair against the other leg's last book.
"""

from __future__ import annotations

import json
import time
from typing import Any

from app.hunter import Level

WS_MARKET = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _parse_ts_ms(raw: Any) -> float:
    if raw is None or raw == "":
        return time.time() * 1000.0
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return time.time() * 1000.0
    if v < 1e12:
        return v * 1000.0
    return v


def _levels(raw: Any, *, asks: bool) -> list[Level]:
    if not isinstance(raw, list):
        return []
    out: list[Level] = []
    for row in raw:
        if not isinstance(row, dict):
            continue
        try:
            px = float(row.get("price"))
            sz = float(row.get("size"))
        except (TypeError, ValueError):
            continue
        if px > 0 and sz > 0:
            out.append(Level(px, sz))
    return sorted(out, key=lambda x: x.price, reverse=not asks)


def _empty() -> dict:
    return {"asks": [], "bids": [], "ts": 0.0, "source": ""}


class BookCache:
    def __init__(self) -> None:
        self.books: dict[str, dict] = {}
        self.wanted: tuple[str, ...] = ()
        self.connected = False
        self.last_msg_ts = 0.0

    def set_wanted(self, tokens: list[str]) -> bool:
        nxt = tuple(sorted({t for t in tokens if t}))
        changed = nxt != self.wanted
        self.wanted = nxt
        drop = [k for k in self.books if k not in set(nxt)]
        for k in drop:
            self.books.pop(k, None)
        return changed

    def put(self, token: str, asks: list[Level], bids: list[Level], *, ts_ms: float, source: str) -> None:
        if not token:
            return
        self.books[token] = {"asks": asks, "bids": bids, "ts": float(ts_ms), "source": source}
        self.last_msg_ts = time.time()

    def age_ms(self, token: str, now_ms: float | None = None) -> float | None:
        row = self.books.get(token)
        if not row:
            return None
        stamp = now_ms if now_ms is not None else time.time() * 1000.0
        return stamp - float(row.get("ts") or 0)

    def pair(self, up_token: str, down_token: str, *, max_age_ms: float) -> dict | None:
        now_ms = time.time() * 1000.0
        up, dn = self.books.get(up_token), self.books.get(down_token)
        if not up or not dn:
            return None
        if now_ms - float(up["ts"]) > max_age_ms or now_ms - float(dn["ts"]) > max_age_ms:
            return None
        return {
            "up": up,
            "down": dn,
            "age_ms": max(now_ms - float(up["ts"]), now_ms - float(dn["ts"])),
            "source": up.get("source") or dn.get("source") or "ws",
        }

    def apply_message(self, raw: str | bytes | dict | list) -> list[str]:
        """Apply a WS frame. Returns token ids that changed."""
        if raw == "PONG" or raw == "PING" or raw == b"PONG" or raw == b"PING":
            self.last_msg_ts = time.time()
            return []
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            text = raw.strip()
            if text in {"PONG", "PING", ""}:
                self.last_msg_ts = time.time()
                return []
            try:
                data = json.loads(text)
            except json.JSONDecodeError:
                return []
        else:
            data = raw
        changed: list[str] = []
        if isinstance(data, list):
            for item in data:
                changed.extend(self._apply_one(item))
        elif isinstance(data, dict):
            changed.extend(self._apply_one(data))
        return changed

    def _apply_one(self, msg: dict) -> list[str]:
        if not isinstance(msg, dict):
            return []
        kind = str(msg.get("event_type") or msg.get("type") or "")
        ts = _parse_ts_ms(msg.get("timestamp"))
        if kind == "book":
            token = str(msg.get("asset_id") or msg.get("tokenId") or "")
            payload = msg.get("payload") if isinstance(msg.get("payload"), dict) else msg
            token = token or str(payload.get("asset_id") or payload.get("tokenId") or "")
            self.put(
                token,
                _levels(payload.get("asks"), asks=True),
                _levels(payload.get("bids"), asks=False),
                ts_ms=ts,
                source="ws",
            )
            return [token] if token else []
        if kind == "best_bid_ask":
            token = str(msg.get("asset_id") or "")
            return self._top(token, msg.get("best_ask"), msg.get("best_bid"), ts)
        if kind == "price_change":
            out: list[str] = []
            for ch in msg.get("price_changes") or []:
                if not isinstance(ch, dict):
                    continue
                token = str(ch.get("asset_id") or "")
                if ch.get("best_ask") is not None or ch.get("best_bid") is not None:
                    out.extend(self._top(token, ch.get("best_ask"), ch.get("best_bid"), ts))
                    continue
                out.extend(self._delta(token, ch, ts))
            return out
        return []

    def _top(self, token: str, ask: Any, bid: Any, ts: float) -> list[str]:
        if not token:
            return []
        cur = self.books.get(token) or _empty()
        asks, bids = list(cur.get("asks") or []), list(cur.get("bids") or [])
        try:
            if ask is not None and float(ask) > 0:
                top_sz = asks[0].size if asks else 50.0
                asks = [Level(float(ask), top_sz)] + [lv for lv in asks if lv.price > float(ask) + 1e-12]
        except (TypeError, ValueError):
            pass
        try:
            if bid is not None and float(bid) > 0:
                top_sz = bids[0].size if bids else 50.0
                bids = [Level(float(bid), top_sz)] + [lv for lv in bids if lv.price < float(bid) - 1e-12]
        except (TypeError, ValueError):
            pass
        self.put(token, _levels([{"price": lv.price, "size": lv.size} for lv in asks], asks=True), _levels([{"price": lv.price, "size": lv.size} for lv in bids], asks=False), ts_ms=ts, source="ws")
        return [token]

    def _delta(self, token: str, ch: dict, ts: float) -> list[str]:
        if not token:
            return []
        try:
            px = float(ch["price"])
            sz = float(ch.get("size") or 0)
        except (KeyError, TypeError, ValueError):
            return []
        side = str(ch.get("side") or "").upper()
        cur = self.books.get(token) or _empty()
        key = "bids" if side == "BUY" else "asks"
        levels = [lv for lv in (cur.get(key) or []) if abs(lv.price - px) > 1e-12]
        if sz > 0:
            levels.append(Level(px, sz))
        if key == "asks":
            cur["asks"] = sorted(levels, key=lambda x: x.price)
            cur["bids"] = list(cur.get("bids") or [])
        else:
            cur["bids"] = sorted(levels, key=lambda x: x.price, reverse=True)
            cur["asks"] = list(cur.get("asks") or [])
        self.put(token, cur["asks"], cur["bids"], ts_ms=ts, source="ws")
        return [token]
