"""Bound page selections and text returned to callers."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..errors import GoodnotesError
from .json import serializable


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
            page["truncated"] = page.get("truncated", False) or part["truncated"]
            truncated |= part["truncated"]
            remaining -= len(part["text"])
    return truncated


def snippet_hits(hits: Any, query: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = serializable(hits)
    folded_query = query.casefold()
    for hit in rows:
        original = hit["text"]
        folded_offset = original.casefold().find(folded_query)
        start = 0
        if folded_offset > 0:
            folded_position = 0
            for original_position, character in enumerate(original):
                folded_length = len(character.casefold())
                if folded_position <= folded_offset < folded_position + folded_length:
                    start = max(0, original_position - 100)
                    break
                folded_position += folded_length
        hit["text"] = original[start : start + 320]
        hit["snippet"] = start > 0 or start + 320 < len(original)
    return rows
