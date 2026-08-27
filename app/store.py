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
CREATE TABLE IF NOT EXISTS resting (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts REAL NOT NULL,
  slug TEXT NOT NULL,
  condition_id TEXT NOT NULL,
  title TEXT,
  up_token TEXT,
  down_token TEXT,
  shares REAL NOT NULL,
  up_price REAL NOT NULL,
  down_price REAL NOT NULL,
  up_filled INTEGER NOT NULL DEFAULT 0,
  down_filled INTEGER NOT NULL DEFAULT 0,
  reserved REAL NOT NULL DEFAULT 0,
  reserved_up REAL NOT NULL DEFAULT 0,
  reserved_down REAL NOT NULL DEFAULT 0,
  net REAL,
  end TEXT,
  status TEXT NOT NULL DEFAULT 'open',
  payload TEXT NOT NULL DEFAULT '{}'
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
        self._migrate()
        self._ensure_settings()

    def _migrate(self) -> None:
        cols = {str(r[1]) for r in self._conn.execute("PRAGMA table_info(inventory)").fetchall()}
        if "kind" not in cols:
            self._conn.execute("ALTER TABLE inventory ADD COLUMN kind TEXT NOT NULL DEFAULT 'pair'")
        if "cost" not in cols:
            self._conn.execute("ALTER TABLE inventory ADD COLUMN cost REAL NOT NULL DEFAULT 0")
        self._conn.commit()

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

    def inventory_open(self) -> list[dict]:
        return [r for r in self.inventory() if float(r.get("up") or 0) > 0.01 or float(r.get("down") or 0) > 0.01]

    def prune_empty_inventory(self) -> int:
        with self._lock:
            cur = self._conn.execute("DELETE FROM inventory WHERE up<=0.01 AND down<=0.01")
            self._conn.commit()
            return int(cur.rowcount or 0)

    def inventory_one(self, condition_id: str) -> dict:
        row = self._conn.execute("SELECT * FROM inventory WHERE condition_id=?", (condition_id,)).fetchone()
        if row:
            return dict(row)
        return {"condition_id": condition_id, "slug": "", "up": 0.0, "down": 0.0, "kind": "pair", "cost": 0.0}

    def add_inventory(
        self,
        condition_id: str,
        slug: str,
        up: float,
        down: float,
        *,
        kind: str | None = None,
        cost: float = 0.0,
    ) -> dict:
        with self._lock:
            return self._add_inventory_unlocked(condition_id, slug, up, down, kind=kind, cost=cost)

    def merge_inventory(self, condition_id: str, shares: float) -> dict:
        with self._lock:
            return self._merge_inventory_unlocked(condition_id, shares)

    def _add_inventory_unlocked(
        self,
        condition_id: str,
        slug: str,
        up: float,
        down: float,
        *,
        kind: str | None = None,
        cost: float = 0.0,
    ) -> dict:
        cur = self.inventory_one(condition_id)
        nu, nd = float(cur["up"]) + float(up), float(cur["down"]) + float(down)
        new_kind = kind or cur.get("kind") or "pair"
        if (cur.get("kind") or "pair") == "favorite" or kind == "favorite":
            new_kind = "favorite"
        new_cost = round(float(cur.get("cost") or 0) + float(cost or 0), 6)
        return self._write_inventory_unlocked(condition_id, slug or cur.get("slug") or "", nu, nd, kind=new_kind, cost=new_cost)

    def _merge_inventory_unlocked(self, condition_id: str, shares: float) -> dict:
        cur = self.inventory_one(condition_id)
        take = min(shares, cur["up"], cur["down"])
        nu, nd = float(cur["up"]) - take, float(cur["down"]) - take
        written = self._write_inventory_unlocked(
            condition_id, cur.get("slug") or "", nu, nd, kind=cur.get("kind") or "pair", cost=float(cur.get("cost") or 0)
        )
        return {"condition_id": condition_id, "merged": take, "up": written["up"], "down": written["down"]}

    def take_inventory(self, condition_id: str, up: float = 0.0, down: float = 0.0) -> dict:
        with self._lock:
            cur = self.inventory_one(condition_id)
            take_up = min(max(float(up), 0.0), float(cur["up"]))
            take_dn = min(max(float(down), 0.0), float(cur["down"]))
            nu, nd = float(cur["up"]) - take_up, float(cur["down"]) - take_dn
            written = self._write_inventory_unlocked(condition_id, cur.get("slug") or "", nu, nd, kind=cur.get("kind"), cost=float(cur.get("cost") or 0))
            return {"condition_id": condition_id, "up": written["up"], "down": written["down"], "took_up": take_up, "took_down": take_dn}

    def _write_inventory_unlocked(
        self,
        condition_id: str,
        slug: str,
        up: float,
        down: float,
        *,
        kind: str | None = "pair",
        cost: float = 0.0,
    ) -> dict:
        up, down = float(up), float(down)
        kind = str(kind or "pair")
        cost = round(float(cost or 0), 6)
        if up <= 0.01 and down <= 0.01:
            self._conn.execute("DELETE FROM inventory WHERE condition_id=?", (condition_id,))
            self._conn.commit()
            return {"condition_id": condition_id, "slug": slug, "up": 0.0, "down": 0.0, "kind": "pair", "cost": 0.0}
        self._conn.execute(
            """INSERT INTO inventory(condition_id,slug,up,down,updated,kind,cost) VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(condition_id) DO UPDATE SET
                 slug=excluded.slug, up=excluded.up, down=excluded.down,
                 updated=excluded.updated, kind=excluded.kind, cost=excluded.cost""",
            (condition_id, slug, up, down, time.time(), kind, cost),
        )
        self._conn.commit()
        return {"condition_id": condition_id, "slug": slug, "up": up, "down": down, "kind": kind, "cost": cost}

    def unmatched_shares(self) -> float:
        total = 0.0
        for row in self.inventory():
            total += abs(float(row["up"] or 0) - float(row["down"] or 0))
        return round(total, 6)

    def latest_resting(self, condition_id: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM resting WHERE condition_id=? ORDER BY id DESC LIMIT 1",
            (condition_id,),
        ).fetchone()
        return None if row is None else self._decode_resting(row)

    def today_pnl(self) -> float:
        """UTC-day sum of recorded trade nets. Prefer paper_state()['today_pnl'] for the cash book."""
        start = time.time() - (time.time() % 86400)
        row = self._conn.execute(
            "SELECT COALESCE(SUM(net),0) AS s FROM trades WHERE ts>=? AND status IN ('filled','paper_filled','merged')",
            (start,),
        ).fetchone()
        return float(row["s"] if row else 0.0)

    def paper_exists(self) -> bool:
        with self._lock:
            return self._get("paper") is not None

    def _planned_starting_unlocked(self) -> float:
        try:
            s = json.loads(self._get("settings") or "{}")
            return float(s.get("paper_starting_cash") or 500)
        except (TypeError, ValueError, json.JSONDecodeError):
            return 500.0

    def _paper_default(self, starting: float) -> dict:
        start = round(float(starting), 6)
        return {
            "starting": start,
            "cash": start,
            "reserved": 0.0,
            "realized_pnl": 0.0,
            "day": _utc_day(),
            "day_start_equity": start,
        }

    def _inventory_matched_usd(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN up < down THEN up ELSE down END), 0) AS v FROM inventory WHERE kind!='favorite' OR kind IS NULL"
        ).fetchone()
        return float(row["v"] or 0.0)

    def _inventory_favorite_usd(self) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(cost), 0) AS v FROM inventory WHERE kind='favorite'"
        ).fetchone()
        return float(row["v"] or 0.0)

    def _paper_view(self, data: dict) -> dict:
        inv = round(self._inventory_matched_usd() + self._inventory_favorite_usd(), 6)
        cash = round(float(data.get("cash") or 0), 6)
        reserved = round(float(data.get("reserved") or 0), 6)
        starting = round(float(data.get("starting") or 0), 6)
        equity = round(cash + reserved + inv, 6)
        day = _utc_day()
        if data.get("day") != day:
            data["day"] = day
            data["day_start_equity"] = equity
            self._set("paper", json.dumps(data))
        today_pnl = round(equity - float(data.get("day_start_equity") or equity), 6)
        return {
            "starting": starting,
            "cash": cash,
            "reserved": reserved,
            "realized_pnl": round(float(data.get("realized_pnl") or 0), 6),
            "inventory_value": inv,
            "equity": equity,
            "total_pnl": round(equity - starting, 6),
            "today_pnl": today_pnl,
            "day": data["day"],
            "resting": self._resting_open_count(),
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
                data.setdefault("reserved", 0.0)
                data.setdefault("realized_pnl", 0.0)
                data.setdefault("day", _utc_day())
                data.setdefault("day_start_equity", float(data.get("cash") or starting))
                self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_state(self) -> dict:
        with self._lock:
            raw = self._get("paper")
            if raw is None:
                data = self._paper_default(self._planned_starting_unlocked())
                self._set("paper", json.dumps(data))
            else:
                data = json.loads(raw)
            return self._paper_view(data)

    def reset_paper(self, starting: float) -> dict:
        with self._lock:
            self._conn.execute("DELETE FROM inventory")
            self._conn.execute("DELETE FROM resting")
            self._conn.commit()
            data = self._paper_default(starting)
            self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_apply_buy(self, cost: float) -> dict:
        with self._lock:
            return self._paper_apply_buy_unlocked(cost)

    def paper_apply_credit(self, amount: float) -> dict:
        with self._lock:
            data = self._load_paper_unlocked()
            data["cash"] = round(float(data["cash"]) + max(0.0, float(amount)), 6)
            self._set("paper", json.dumps(data))
            return self._paper_view(data)

    def paper_reserve(self, amount: float) -> dict:
        with self._lock:
            return self._paper_reserve_unlocked(amount)

    def paper_release(self, amount: float) -> dict:
        with self._lock:
            return self._paper_release_unlocked(amount)

    def paper_consume_reserve(self, amount: float) -> dict:
        with self._lock:
            return self._paper_consume_reserve_unlocked(amount)

    def _load_paper_unlocked(self) -> dict:
        raw = self._get("paper")
        data = json.loads(raw) if raw else self._paper_default(self._planned_starting_unlocked())
        data.setdefault("reserved", 0.0)
        self._paper_view(data)
        return json.loads(self._get("paper") or json.dumps(data))

    def _paper_apply_buy_unlocked(self, cost: float) -> dict:
        data = self._load_paper_unlocked()
        cost = round(float(cost), 6)
        if float(data["cash"]) + 1e-9 < cost:
            raise ValueError("insufficient_cash")
        data["cash"] = round(float(data["cash"]) - cost, 6)
        self._set("paper", json.dumps(data))
        return self._paper_view(data)

    def _paper_reserve_unlocked(self, amount: float) -> dict:
        data = self._load_paper_unlocked()
        amount = round(float(amount), 6)
        if amount < 0:
            raise ValueError("negative_reserve")
        if float(data["cash"]) + 1e-9 < amount:
            raise ValueError("insufficient_cash")
        data["cash"] = round(float(data["cash"]) - amount, 6)
        data["reserved"] = round(float(data.get("reserved") or 0) + amount, 6)
        self._set("paper", json.dumps(data))
        return self._paper_view(data)

    def _paper_release_unlocked(self, amount: float) -> dict:
        data = self._load_paper_unlocked()
        amount = round(min(float(amount), float(data.get("reserved") or 0)), 6)
        if amount <= 0:
            return self._paper_view(data)
        data["reserved"] = round(float(data.get("reserved") or 0) - amount, 6)
        data["cash"] = round(float(data["cash"]) + amount, 6)
        self._set("paper", json.dumps(data))
        return self._paper_view(data)

    def _paper_consume_reserve_unlocked(self, amount: float) -> dict:
        data = self._load_paper_unlocked()
        amount = round(min(float(amount), float(data.get("reserved") or 0)), 6)
        if amount <= 0:
            return self._paper_view(data)
        data["reserved"] = round(float(data.get("reserved") or 0) - amount, 6)
        self._set("paper", json.dumps(data))
        return self._paper_view(data)

    def paper_apply_merge(self, shares: float, net: float) -> dict:
        with self._lock:
            return self._paper_apply_merge_unlocked(shares, net)

    def _paper_apply_merge_unlocked(self, shares: float, net: float) -> dict:
        data = self._load_paper_unlocked()
        data["cash"] = round(float(data["cash"]) + float(shares), 6)
        data["realized_pnl"] = round(float(data.get("realized_pnl") or 0) + float(net), 6)
        self._set("paper", json.dumps(data))
        return self._paper_view(data)

    def _decode_resting(self, row: sqlite3.Row) -> dict:
        d = dict(row)
        try:
            d["payload"] = json.loads(d.get("payload") or "{}")
        except json.JSONDecodeError:
            d["payload"] = {}
        d["up_filled"] = bool(d.get("up_filled"))
        d["down_filled"] = bool(d.get("down_filled"))
        return d

    def _resting_open_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) c FROM resting WHERE status='open'").fetchone()
        return int(row["c"] if row else 0)

    def resting_open(self) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM resting WHERE status='open' ORDER BY id ASC").fetchall()
        return [self._decode_resting(r) for r in rows]

    def resting_by_slug(self, slug: str) -> dict | None:
        row = self._conn.execute(
            "SELECT * FROM resting WHERE slug=? AND status='open' ORDER BY id DESC LIMIT 1",
            (slug,),
        ).fetchone()
        return None if row is None else self._decode_resting(row)

    def has_open_resting(self, slug: str) -> bool:
        return self.resting_by_slug(slug) is not None

    def add_resting(
        self,
        *,
        slug: str,
        condition_id: str,
        title: str,
        up_token: str,
        down_token: str,
        shares: float,
        up_price: float,
        down_price: float,
        net: float,
        end: str | None = None,
        payload: dict | None = None,
    ) -> dict:
        with self._lock:
            if self.resting_by_slug(slug) is not None:
                raise ValueError("already_resting")
            reserved_up = round(float(up_price) * float(shares), 6)
            reserved_down = round(float(down_price) * float(shares), 6)
            reserved = round(reserved_up + reserved_down, 6)
            self._paper_reserve_unlocked(reserved)
            self._conn.execute(
                """INSERT INTO resting(
                    ts,slug,condition_id,title,up_token,down_token,shares,
                    up_price,down_price,up_filled,down_filled,reserved,
                    reserved_up,reserved_down,net,end,status,payload
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    time.time(),
                    slug,
                    condition_id,
                    title,
                    up_token,
                    down_token,
                    shares,
                    up_price,
                    down_price,
                    0,
                    0,
                    reserved,
                    reserved_up,
                    reserved_down,
                    net,
                    end,
                    "open",
                    json.dumps(payload or {}),
                ),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM resting WHERE id=last_insert_rowid()").fetchone()
            return self._decode_resting(row)

    def fill_resting_leg(self, rid: int, side: str) -> dict:
        if side not in {"up", "down"}:
            raise ValueError("bad_side")
        with self._lock:
            row = self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ValueError("missing_resting")
            cur = self._decode_resting(row)
            if cur["status"] != "open":
                return cur
            flag = f"{side}_filled"
            if cur[flag]:
                return cur
            cost = round(float(cur[f"{side}_price"]) * float(cur["shares"]), 6)
            reserved_key = f"reserved_{side}"
            take = min(cost, float(cur.get(reserved_key) or 0))
            self._paper_consume_reserve_unlocked(take)
            up_add = float(cur["shares"]) if side == "up" else 0.0
            down_add = float(cur["shares"]) if side == "down" else 0.0
            inv_kind = "favorite" if (cur.get("payload") or {}).get("strategy") == "favorite" else "pair"
            self._add_inventory_unlocked(cur["condition_id"], cur["slug"], up_add, down_add, kind=inv_kind, cost=take)
            both = (side == "up" and cur["down_filled"]) or (side == "down" and cur["up_filled"])
            status = "filled" if both else "open"
            leftover = round(float(cur["reserved"]) - take, 6)
            self._conn.execute(
                f"UPDATE resting SET {flag}=1, {reserved_key}=0, reserved=?, status=? WHERE id=?",
                (max(leftover, 0.0), status, rid),
            )
            self._conn.commit()
            return self._decode_resting(self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone())

    def complete_resting(self, rid: int, reason: str) -> dict:
        with self._lock:
            row = self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone()
            if row is None:
                raise ValueError("missing_resting")
            cur = self._decode_resting(row)
            leftover = round(float(cur.get("reserved") or 0), 6)
            if leftover > 0:
                self._paper_release_unlocked(leftover)
            payload = dict(cur.get("payload") or {})
            payload["complete_reason"] = reason
            self._conn.execute(
                "UPDATE resting SET status=?, reserved=0, reserved_up=0, reserved_down=0, payload=? WHERE id=?",
                ("filled", json.dumps(payload), rid),
            )
            self._conn.commit()
            return self._decode_resting(self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone())

    def cancel_resting(self, rid: int, reason: str) -> dict:
        with self._lock:
            return self._cancel_resting_unlocked(rid, reason)

    def _cancel_resting_unlocked(self, rid: int, reason: str) -> dict:
        row = self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone()
        if row is None:
            raise ValueError("missing_resting")
        cur = self._decode_resting(row)
        if cur["status"] != "open":
            return cur
        leftover = round(float(cur.get("reserved") or 0), 6)
        if leftover > 0:
            self._paper_release_unlocked(leftover)
        payload = dict(cur.get("payload") or {})
        payload["cancel_reason"] = reason
        self._conn.execute(
            "UPDATE resting SET status=?, reserved=0, reserved_up=0, reserved_down=0, payload=? WHERE id=?",
            ("cancelled", json.dumps(payload), rid),
        )
        self._conn.commit()
        return self._decode_resting(self._conn.execute("SELECT * FROM resting WHERE id=?", (rid,)).fetchone())

    def cancel_all_resting(self, reason: str) -> int:
        with self._lock:
            rows = self._conn.execute("SELECT id FROM resting WHERE status='open'").fetchall()
            n = 0
            for row in rows:
                self._cancel_resting_unlocked(int(row["id"]), reason)
                n += 1
            return n

    def _open_market_count(self) -> int:
        ids = set()
        for row in self._conn.execute("SELECT condition_id FROM inventory WHERE up>0.01 OR down>0.01"):
            ids.add(row["condition_id"])
        for row in self._conn.execute("SELECT condition_id FROM resting WHERE status='open'"):
            ids.add(row["condition_id"])
        return len(ids)

    def stats(self) -> dict:
        scans = self._conn.execute("SELECT COUNT(*) c FROM scans WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        fills = self._conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE ts>=? AND status IN ('filled','paper_filled','merged')",
            (time.time() - 86400,),
        ).fetchone()["c"]
        hedges = self._conn.execute(
            "SELECT COUNT(*) c FROM trades WHERE ts>=? AND status IN ('paper_hedged','paper_dumped','paper_settled')",
            (time.time() - 86400,),
        ).fetchone()["c"]
        trades = self._conn.execute("SELECT COUNT(*) c FROM trades WHERE ts>=?", (time.time() - 86400,)).fetchone()["c"]
        paper = self.paper_state()
        return {
            "scans_24h": scans,
            "trades_24h": fills,
            "hedges_24h": hedges,
            "orders_24h": trades,
            "today_pnl": paper["today_pnl"],
            "open_markets": self._open_market_count(),
            "starting": paper["starting"],
            "cash": paper["cash"],
            "reserved": paper["reserved"],
            "equity": paper["equity"],
            "total_pnl": paper["total_pnl"],
            "inventory_value": paper["inventory_value"],
            "realized_pnl": paper["realized_pnl"],
            "resting": paper["resting"],
            "unmatched_shares": self.unmatched_shares(),
        }
