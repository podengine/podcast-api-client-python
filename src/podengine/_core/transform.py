"""Pure request/response helpers shared by the sync and async transports.

No I/O, no network — just turning Python call values into the JSON/query shapes the API
expects. Date/UUID/model handling is delegated to ``pydantic_core.to_jsonable_python`` so a
caller can pass ``datetime`` objects, generated pydantic models, or plain dicts/lists
interchangeably.
"""

from __future__ import annotations

from typing import Any

from pydantic_core import to_jsonable_python


def to_jsonable(value: Any) -> Any:
    """Convert an arbitrary call value (pydantic model, datetime, dict, ...) to a
    JSON-serializable structure, emitting model fields under their wire aliases."""
    return to_jsonable_python(value, by_alias=True, exclude_none=False)


def _stringify_query_value(value: Any) -> str:
    if isinstance(value, bool):
        # Match the wire convention used by the TypeScript SDK (lowercase booleans).
        return "true" if value else "false"
    return str(value)


def build_query_params(query: dict[str, Any]) -> list[tuple[str, str]]:
    """Serialize a flat mapping into repeated query items (``?id=a&id=b``).

    ``None`` entries are skipped, arrays are repeated, and ``datetime``/``UUID`` values are
    rendered via their JSON form (ISO-8601 / canonical string).
    """
    items: list[tuple[str, str]] = []
    for key, raw in query.items():
        if raw is None:
            continue
        value = to_jsonable(raw)
        if isinstance(value, (list, tuple)):
            for item in value:
                if item is None:
                    continue
                items.append((key, _stringify_query_value(item)))
            continue
        items.append((key, _stringify_query_value(value)))
    return items


def extract_error_message(body: Any, fallback: str) -> str:
    """Pull the most useful human-readable message out of the various error envelope
    shapes the API may return, falling back to the raw text / status."""
    if isinstance(body, str):
        return body or fallback
    if isinstance(body, dict):
        data = body.get("data") if isinstance(body.get("data"), dict) else None

        def pick(key: str) -> Any:
            if data is not None and data.get(key) is not None:
                return data.get(key)
            return body.get(key)

        for key in ("message", "error", "errorMessage", "errorDetails"):
            candidate = pick(key)
            if isinstance(candidate, str) and candidate:
                return candidate
            if candidate is not None:
                import json

                return json.dumps(candidate)
    return fallback
