from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

log = logging.getLogger(__name__)


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


class Journal:
    """Append-only JSONL audit log of every opportunity, decision and fill, rotated by size."""

    def __init__(self, path: str | Path | None, max_bytes: int = 50_000_000, backups: int = 3) -> None:
        self.path = Path(path) if path else None
        self.max_bytes = max_bytes
        self.backups = backups
        self.listeners: list[Callable[[dict[str, Any]], None]] = []
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        record = {"ts": round(time.time(), 3), "kind": kind, **payload}
        if self.path:
            line = json.dumps(record, default=_default, ensure_ascii=False)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
                size = fh.tell()
            if self.max_bytes and size > self.max_bytes:
                self._rotate()
        for listener in self.listeners:
            try:
                listener(record)
            except Exception:
                log.exception("journal listener failed")

    def _rotate(self) -> None:
        assert self.path is not None
        if self.backups <= 0:
            self.path.unlink(missing_ok=True)
            return
        for i in range(self.backups - 1, 0, -1):
            older = self.path.with_name(f"{self.path.name}.{i}")
            if older.exists():
                older.replace(self.path.with_name(f"{self.path.name}.{i + 1}"))
        self.path.replace(self.path.with_name(f"{self.path.name}.1"))

    def read(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if kind is None or r.get("kind") == kind]

    def tail(self, max_bytes: int = 4_000_000) -> list[dict[str, Any]]:
        """Most recent records without reading a large journal in full."""
        if not self.path or not self.path.exists():
            return []
        with self.path.open("rb") as fh:
            size = fh.seek(0, 2)
            fh.seek(max(0, size - max_bytes))
            lines = fh.read().split(b"\n")
        if size > max_bytes:
            lines = lines[1:]
        out = []
        for line in lines:
            if line.strip():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
        return out
