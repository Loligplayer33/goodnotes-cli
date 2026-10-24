"""Compact, bounded output for agent callers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, is_dataclass
from dataclasses import fields as dataclass_fields
from pathlib import Path
from typing import Any

import typer

from .config import Settings
from .errors import GoodnotesError
from .models import Item, Page, PageText, SearchHit


@dataclass(frozen=True)
class Projection:
    """Command identity and enabled flags used to validate a field projection."""

    command_name: str
    enabled_options: frozenset[str] = frozenset()


@dataclass(frozen=True)
class _ProjectionSpec:
    fields: tuple[str, ...]
    row_key: str | None = None
    optional_fields: tuple[str, ...] = ()
    conditional_fields: tuple[tuple[str, str], ...] = ()
    example: str | None = None


def _model_fields(model: type[Any]) -> tuple[str, ...]:
    return tuple(field.name for field in dataclass_fields(model))


_ITEM_FIELDS = _model_fields(Item)
_PROJECTIONS: dict[str, _ProjectionSpec] = {
    "ls": _ProjectionSpec(
        _model_fields(Item),
        row_key="items",
        example="goodnotes --fields id,name,kind,path ls",
    ),
    "tree": _ProjectionSpec(
        _model_fields(Item),
        row_key="items",
        example="goodnotes --fields id,name,kind,path tree",
    ),
    "info": _ProjectionSpec(
        (*_ITEM_FIELDS, "snapshot"), example='goodnotes --fields id,name,path info "Course Notes"'
    ),
    "pages": _ProjectionSpec(
        _model_fields(Page),
        row_key="pages",
        conditional_fields=(("recognition", "with_recognition"),),
        example='goodnotes --fields number,recognition pages "Course Notes" --with-recognition',
    ),
    "read": _ProjectionSpec(
        _model_fields(PageText),
        row_key="pages",
        example='goodnotes --fields page,parts,warnings,truncated read "Course Notes" --page 1',
    ),
    "search": _ProjectionSpec(
        (*_model_fields(SearchHit), "snippet"),
        row_key="hits",
        example='goodnotes --fields path,page,source,text search "term"',
    ),
    "scan-pdfs": _ProjectionSpec(
        (*_model_fields(SearchHit), "snippet"),
        row_key="hits",
        example='goodnotes --fields path,page,source,text scan-pdfs "term"',
    ),
    "sync": _ProjectionSpec(
        ("status", "snapshot", "snapshot_id", "checked_at", "source_changed_during_refresh"),
        optional_fields=("captured_at", "warnings", "capture_stats"),
    ),
    "watch": _ProjectionSpec(
        ("event", "iteration"),
        optional_fields=(
            "status",
            "snapshot",
            "snapshot_id",
            "checked_at",
            "captured_at",
            "source_changed_during_refresh",
            "warnings",
            "capture_stats",
            "error",
            "retry_in_seconds",
        ),
    ),
    "snapshot": _ProjectionSpec(("snapshot", "status")),
    "export": _ProjectionSpec(
        (
            "path",
            "document_id",
            "pages",
            "selected_pages",
            "layer",
            "source",
            "includes_handwriting",
            "annotation_status",
            "warnings",
            "snapshot",
        ),
        optional_fields=("native_annotations", "captured_at"),
    ),
    "render": _ProjectionSpec(
        (
            "path",
            "document_id",
            "page",
            "source_page",
            "dpi",
            "layer",
            "source",
            "includes_handwriting",
            "annotation_status",
            "warnings",
            "snapshot",
        ),
        optional_fields=("native_annotations", "captured_at"),
    ),
    "attach-pdf": _ProjectionSpec(
        ("document_id", "path", "pages", "registered", "warnings", "snapshot")
    ),
    "bundle": _ProjectionSpec(
        ("directory", "manifest", "pages", "layer", "truncated", "snapshot"),
        optional_fields=("captured_at",),
    ),
}


def output_contract(command_name: str) -> dict[str, Any] | None:
    """Describe the command's supported response or row projection."""
    spec = _PROJECTIONS.get(command_name)
    if spec is None:
        return None
    if spec.row_key is None:
        result: dict[str, Any] = {"projection": "response", "fields": list(spec.fields)}
        if spec.optional_fields:
            result["optional_fields"] = {
                field: "May be absent from a successful response." for field in spec.optional_fields
            }
        if spec.example is not None:
            result["example"] = spec.example
        return result

    row_result: dict[str, Any] = {
        "projection": f"{spec.row_key}[]",
        "fields": list(spec.fields),
        "metadata": "Sibling metadata, including snapshot, is retained automatically.",
    }
    if spec.conditional_fields:
        row_result["optional_fields"] = {
            field: f"Requires --{option.replace('_', '-')}"
            for field, option in spec.conditional_fields
        }
    if spec.example is not None:
        row_result["example"] = spec.example
    return row_result


def validate_projection(raw_fields: str | None, projection: Projection) -> list[str] | None:
    """Validate fields before a command runs and return their parsed order."""
    fields = _parse_fields(raw_fields)
    if fields is None:
        return None
    spec = _PROJECTIONS.get(projection.command_name)
    if spec is not None:
        available = set(spec.fields) | set(spec.optional_fields)
        available.update(
            field
            for field, option in spec.conditional_fields
            if option in projection.enabled_options
        )
        _unknown_fields(
            fields,
            available,
            f"{spec.row_key}[]" if spec.row_key else "the response object",
        )
    return fields


def _unknown_fields(fields: list[str], available: Iterable[str], level: str) -> None:
    keys = set(available)
    unknown = set(fields) - keys
    if unknown:
        raise GoodnotesError(
            f"Unknown output fields: {', '.join(sorted(unknown))}. "
            f"Select fields from {level}: {', '.join(sorted(keys))}. "
            "Snapshot is retained automatically."
        )


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
    if settings.snapshot_id is not None and isinstance(result, dict):
        result["snapshot"] = settings.snapshot_id
    projection = settings.projection
    fields = (
        validate_projection(settings.fields, projection)
        if projection is not None
        else _parse_fields(settings.fields)
    )
    if fields is not None:
        if isinstance(result, dict):
            spec = _PROJECTIONS.get(projection.command_name) if projection is not None else None
            if (
                spec is not None
                and spec.row_key is not None
                and isinstance(result.get(spec.row_key), list)
            ):
                rows = result[spec.row_key]
                if not all(isinstance(row, dict) for row in rows):
                    raise GoodnotesError(f"Expected {spec.row_key} rows in the command response.")
                result[spec.row_key] = [
                    {field: row[field] for field in fields if field in row} for row in rows
                ]
            elif spec is not None and spec.row_key is None:
                result = {field: result[field] for field in fields if field in result}
            else:
                _unknown_fields(fields, result.keys(), "the response object")
                result = {field: result[field] for field in fields if field in result}
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


def emit_error(code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
    """Emit a structured error, optionally with machine-readable recovery context."""
    error: dict[str, Any] = {"code": code, "message": message}
    if details is not None:
        error["details"] = serializable(details)
    typer.echo(
        json.dumps(
            {"error": error},
            separators=(",", ":"),
        ),
        err=True,
    )


def _parse_fields(raw_fields: str | None) -> list[str] | None:
    if raw_fields is None:
        return None
    fields = [field.strip() for field in raw_fields.split(",") if field.strip()]
    if not fields:
        raise GoodnotesError("--fields must contain at least one field name.")
    return fields
