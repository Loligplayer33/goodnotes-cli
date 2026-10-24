"""Compact, bounded output for agent callers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

import typer

from .config import Settings
from .errors import GoodnotesError


def serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return serializable(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def emit(value: Any, settings: Settings) -> None:
    result = serializable(value)
    if settings.fields:
        fields = [field.strip() for field in settings.fields.split(",") if field.strip()]
        if not fields:
            raise GoodnotesError("--fields must contain at least one field name.")
        if isinstance(result, dict):
            for key in ("items", "pages", "hits"):
                if isinstance(result.get(key), list):
                    for row in result[key]:
                        unknown = set(fields) - row.keys()
                        if unknown:
                            raise GoodnotesError(
                                f"Unknown output fields: {', '.join(sorted(unknown))}"
                            )
                    result[key] = [{field: row[field] for field in fields} for row in result[key]]
                    break
            else:
                unknown = set(fields) - result.keys()
                if unknown:
                    raise GoodnotesError(f"Unknown output fields: {', '.join(sorted(unknown))}")
                result = {field: result[field] for field in fields}
    if settings.snapshot_id is not None and isinstance(result, dict):
        result["snapshot"] = settings.snapshot_id
    typer.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if settings.text else None,
            separators=None if settings.text else (",", ":"),
        )
    )


def window(items: list[Any], limit: int, offset: int) -> dict[str, Any]:
    selected = items[offset : offset + limit]
    next_offset = offset + limit if offset + limit < len(items) else None
    return {"items": selected, "next_offset": next_offset}


def select_pages(expression: str, page_count: int, *, maximum: int = 20) -> list[int]:
    """Parse an ordered selection, rejecting duplicates and unbounded ranges."""
    result: list[int] = []
    try:
        for part in expression.split(","):
            if not part.strip():
                raise ValueError
            if "-" in part:
                first, last = (int(number) for number in part.split("-"))
                if first > last or last - first + 1 > maximum:
                    raise ValueError
                numbers: Iterable[int] = range(first, last + 1)
            else:
                numbers = [int(part)]
            for number in numbers:
                if number < 1 or number > page_count or number in result:
                    raise ValueError
                result.append(number)
                if len(result) > maximum:
                    raise ValueError
    except ValueError as exc:
        raise GoodnotesError(
            f"Select 1 to {maximum} distinct pages within 1-{page_count}, e.g. '1-3,7'."
        ) from exc
    return result


def bound_page_text(pages: list[dict[str, Any]], max_chars: int) -> bool:
    """Apply one text budget across all selected page parts, recording every cut."""
    remaining = max_chars
    truncated = any(page.get("truncated", False) for page in pages)
    for page in pages:
        for part in page["parts"]:
            original = part["text"]
            part["text"] = original[:remaining]
            part["truncated"] = part.get("truncated", False) or len(original) > remaining
            truncated |= part["truncated"]
            remaining -= len(part["text"])
    return truncated


def snippet_hits(hits: Any, query: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = serializable(hits)
    for hit in rows:
        original = hit["text"]
        start = max(0, original.casefold().find(query.casefold()) - 100)
        hit["text"] = original[start : start + 320]
        hit["snippet"] = start > 0 or start + 320 < len(original)
    return rows


def emit_error(code: str, message: str) -> None:
    typer.echo(
        json.dumps({"error": {"code": code, "message": message}}, separators=(",", ":")), err=True
    )
