from __future__ import annotations

import json
from typing import Any

try:
    import orjson
except ImportError:  # pragma: no cover - orjson is a declared dependency; stdlib keeps things working without it
    orjson = None


def loads(data: str | bytes) -> Any:
    return orjson.loads(data) if orjson is not None else json.loads(data)


def dumps(obj: Any) -> bytes:
    """Compact UTF-8 JSON; non-JSON values (datetimes, enums, sets) fall back to ``str``."""
    if orjson is not None:
        return orjson.dumps(obj, default=_default, option=orjson.OPT_NON_STR_KEYS)
    return json.dumps(obj, default=_default, separators=(",", ":"), ensure_ascii=False).encode()


def _default(value: Any) -> Any:
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)
