from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from app.config import DEFAULT_SETTINGS


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
        start = time.time() - (time.time() % 86400)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(net),0) AS s FROM trades WHERE ts>=? AND status IN ('filled','paper_filled','merged')",
            (start,),
        ).fetchone()
        return float(row["s"] if row else 0.0)

    def stats(self) -> dict:
        scans = self._conn.execute("SELECT COUNT(*) c FROM scans WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        trades = self._conn.execute("SELECT COUNT(*) c FROM trades WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        return {
            "scans_24h": scans,
            "trades_24h": trades,
            "today_pnl": self.today_pnl(),
            "open_markets": self._conn.execute("SELECT COUNT(*) c FROM inventory WHERE up>0.01 OR down>0.01").fetchone()["c"],
        }
