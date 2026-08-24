from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DEFAULT_SETTINGS


def _utc_day() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


SCHEMA = """
CREATE TABLE IF NOT EXISTS kv (
  k TEXT PRIMARY KEY,
  v TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS scans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  slug TEXT,
  kind TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  slug TEXT,
  kind TEXT,
  shares REAL,
  up_price REAL,
  down_price REAL,
  net REAL,
  mode TEXT,
  status TEXT,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  level TEXT,
  message TEXT
);
CREATE TABLE IF NOT EXISTS inventory (
  condition_id TEXT PRIMARY KEY,
  slug TEXT,
  up REAL NOT NULL DEFAULT 0,
  down REAL NOT NULL DEFAULT 0,
  updated REAL NOT NULL
);
"""


class Store:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._ensure_settings()

    def _ensure_settings(self) -> None:
        cur = self._get("settings")
        if cur is None:
            self._set("settings", json.dumps(DEFAULT_SETTINGS))
        else:
            merged = dict(DEFAULT_SETTINGS)
            merged.update(json.loads(cur))
            self._set("settings", json.dumps(merged))

    def _get(self, k: str) -> str | None:
        row = self._conn.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return None if row is None else row["v"]

    def _set(self, k: str, v: str) -> None:
        self._conn.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
        self._conn.commit()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            return json.loads(self._get("settings") or json.dumps(DEFAULT_SETTINGS))

    def patch_settings(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            data = json.loads(self._get("settings") or json.dumps(DEFAULT_SETTINGS))
            data.update(kwargs)
            self._set("settings", json.dumps(data))
            return data

    def owner_id(self) -> int | None:
        raw = self._get("owner_id")
        return int(raw) if raw else None

    def set_owner_id(self, user_id: int) -> None:
        with self._lock:
            if self._get("owner_id"):
                return
            self._set("owner_id", str(user_id))

    def add_scan(self, slug: str, kind: str, payload: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO scans(ts,slug,kind,payload) VALUES(?,?,?,?)",
                (time.time(), slug, kind, json.dumps(payload)),
            )
            self._conn.execute("DELETE FROM scans WHERE id NOT IN (SELECT id FROM scans ORDER BY id DESC LIMIT 400)")
            self._conn.commit()

    def add_trade(self, **row: Any) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO trades(ts,slug,kind,shares,up_price,down_price,net,mode,status,payload) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    time.time(),
                    row.get("slug"),
                    row.get("kind"),
                    row.get("shares"),
                    row.get("up_price"),
                    row.get("down_price"),
                    row.get("net"),
                    row.get("mode"),
                    row.get("status"),
                    json.dumps(row.get("payload") or {}),
                ),
            )
            self._conn.commit()

    def add_event(self, level: str, message: str) -> None:
        with self._lock:
            self._conn.execute("INSERT INTO events(ts,level,message) VALUES(?,?,?)", (time.time(), level, message))
            self._conn.execute("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY id DESC LIMIT 500)")
            self._conn.commit()

    def recent_scans(self, n: int = 20) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM scans ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def recent_trades(self, n: int = 30) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload"])} for r in rows]

    def recent_events(self, n: int = 30) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM events ORDER BY id DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]

    def inventory(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM inventory ORDER BY updated DESC").fetchall()
        return [dict(r) for r in rows]

    def inventory_one(self, condition_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM inventory WHERE condition_id=?", (condition_id,)).fetchone()
        if row:
            return dict(row)
        return {"condition_id": condition_id, "slug": "", "up": 0.0, "down": 0.0}

    def add_inventory(self, condition_id: str, slug: str, up: float, down: float) -> dict:
        with self._lock:
            cur = self.inventory_one(condition_id)
            nu, nd = cur["up"] + up, cur["down"] + down
            self._conn.execute(
                "INSERT INTO inventory(condition_id,slug,up,down,updated) VALUES(?,?,?,?,?) ON CONFLICT(condition_id) DO UPDATE SET slug=excluded.slug, up=excluded.up, down=excluded.down, updated=excluded.updated",
                (condition_id, slug or cur.get("slug") or "", nu, nd, time.time()),
            )
            self._conn.commit()
            return {"condition_id": condition_id, "slug": slug, "up": nu, "down": nd}

    def merge_inventory(self, condition_id: str, shares: float) -> dict:
        with self._lock:
            cur = self.inventory_one(condition_id)
            take = min(shares, cur["up"], cur["down"])
            nu, nd = cur["up"] - take, cur["down"] - take
            self._conn.execute(
                "UPDATE inventory SET up=?, down=?, updated=? WHERE condition_id=?",
                (nu, nd, time.time(), condition_id),
            )
            self._conn.commit()
            return {"condition_id": condition_id, "merged": take, "up": nu, "down": nd}

    def today_pnl(self) -> float:
        """UTC-day sum of recorded trade nets. Prefer paper_state()['today_pnl'] for the cash book."""
        start = time.time() - (time.time() % 86400)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(net),0) AS s FROM trades WHERE ts>=? AND status IN ('filled','paper_filled','merged')",
            (start,),
        ).fetchone()
        return float(row["s"] if row else 0.0)

    def _paper_default(self, starting: float) -> dict:
        start = round(float(starting), 6)
        return {
            "starting": start,
            "cash": start,
            "realized_pnl": 0.0,
            "day": _utc_day(),
            "day_start_equity": start,
        }

    def _inventory_matched_usd(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN up < down THEN up ELSE down END), 0) AS v FROM inventory"
        ).fetchone()
        return float(row["v"] or 0.0)

    def _paper_view(self, data: dict) -> dict:
        inv = round(self._inventory_matched_usd(), 6)
        cash = round(float(data.get("cash") or 0), 6)
        starting = round(float(data.get("starting") or 0), 6)
        equity = round(cash + inv, 6)
        day = _utc_day()
        if data.get("day") != day:
            data["day"] = day
            data["day_start_equity"] = equity
            self._set("paper", json.dumps(data))
        today_pnl = round(equity - float(data.get("day_start_equity") or equity), 6)
        return {
            "starting": starting,
            "cash": cash,
            "realized_pnl": round(float(data.get("realized_pnl") or 0), 6),
            "inventory_value": inv,
            "equity": equity,
            "total_pnl": round(equity - starting, 6),
            "today_pnl": today_pnl,
            "day": data["day"],
        }

    def ensure_paper(self, starting: float) -> dict:
        with self._lock:
            raw = self._get("paper")
            if raw is None:
                data = self._paper_default(starting)
                self._set("paper", json.dumps(data))
            else:
                data = json.loads(raw)
                data.setdefault("starting", float(starting))
                data.setdefault("cash", float(data["starting"]))
                data.setdefault("realized_pnl", 0.0)
                data.setdefault("day", _utc_day())
                data.setdefault("day_start_equity", float(data.get("cash") or starting))
                self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_state(self) -> dict:
        with self._lock:
            raw = self._get("paper")
            if raw is None:
                data = self._paper_default(500.0)
                self._set("paper", json.dumps(data))
            else:
                data = json.loads(raw)
            return self._paper_view(data)

    def reset_paper(self, starting: float) -> dict:
        with self._lock:
            self._conn.execute("DELETE FROM inventory")
            self._conn.commit()
            data = self._paper_default(starting)
            self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_apply_buy(self, cost: float) -> dict:
        with self._lock:
            raw = self._get("paper")
            data = json.loads(raw) if raw else self._paper_default(500.0)
            self._paper_view(data)
            data = json.loads(self._get("paper") or json.dumps(data))
            cost = round(float(cost), 6)
            if float(data["cash"]) + 1e-9 < cost:
                raise ValueError("insufficient_cash")
            data["cash"] = round(float(data["cash"]) - cost, 6)
            self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_apply_merge(self, shares: float, net: float) -> dict:
        with self._lock:
            raw = self._get("paper")
            data = json.loads(raw) if raw else self._paper_default(500.0)
            self._paper_view(data)
            data = json.loads(self._get("paper") or json.dumps(data))
            data["cash"] = round(float(data["cash"]) + float(shares), 6)
            data["realized_pnl"] = round(float(data.get("realized_pnl") or 0) + float(net), 6)
            self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def stats(self) -> dict:
        scans = self._conn.execute("SELECT COUNT(*) c FROM scans WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        trades = self._conn.execute("SELECT COUNT(*) c FROM trades WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        paper = self.paper_state()
        return {
            "scans_24h": scans,
            "trades_24h": trades,
            "today_pnl": paper["today_pnl"],
            "open_markets": self._conn.execute(
                "SELECT COUNT(*) c FROM inventory WHERE up>0.01 OR down>0.01"
            ).fetchone()["c"],
            "starting": paper["starting"],
            "cash": paper["cash"],
            "equity": paper["equity"],
            "total_pnl": paper["total_pnl"],
            "inventory_value": paper["inventory_value"],
            "realized_pnl": paper["realized_pnl"],
        }
