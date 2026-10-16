from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any


def _default(obj: Any) -> Any:
    if isinstance(obj, datetime):
        return obj.isoformat()
    if hasattr(obj, "value"):
        return obj.value
    return str(obj)


class Journal:
    """Append-only JSONL audit log of every opportunity, decision and fill."""

    def __init__(self, path: str | Path | None) -> None:
        self.path = Path(path) if path else None
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, kind: str, payload: dict[str, Any]) -> None:
        if not self.path:
            return
        line = json.dumps({"ts": round(time.time(), 3), "kind": kind, **payload}, default=_default, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read(self, kind: str | None = None) -> list[dict[str, Any]]:
        if not self.path or not self.path.exists():
            return []
        rows = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return [r for r in rows if kind is None or r.get("kind") == kind]
