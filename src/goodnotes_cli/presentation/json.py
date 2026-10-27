"""Serialize responses and emit compact JSON."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import fields as dataclass_fields
from dataclasses import is_dataclass
from pathlib import Path
from typing import Any

import typer

from ..config import Settings
from ..errors import GoodnotesError
from .contracts import (
    _BRIEF_COMMANDS,
    _BRIEF_ITEM_FIELDS,
    _BRIEF_NATIVE_FIELDS,
    _PROJECTIONS,
    _parse_fields,
    _unknown_fields,
    validate_projection,
)


def serializable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: serializable(getattr(value, field.name))
            for field in dataclass_fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: serializable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [serializable(item) for item in value]
    return value


def _pick(value: dict[str, Any], fields: Iterable[str]) -> dict[str, Any]:
    """Keep present fields, including explicit unknown/null values."""
    return {field: value[field] for field in fields if field in value}


def _warnings(*values: Any) -> list[Any]:
    result: list[Any] = []
    for value in values:
        if isinstance(value, list):
            for warning in value:
                if warning not in result:
                    result.append(warning)
    return result


def _hoist_document(rows: list[dict[str, Any]], result: dict[str, Any]) -> None:
    if not rows or any("document_id" not in row for row in rows):
        return
    document_id = rows[0]["document_id"]
    if all(row["document_id"] == document_id for row in rows):
        result["document_id"] = document_id
        for row in rows:
            del row["document_id"]


def _brief_render(value: dict[str, Any]) -> dict[str, Any]:
    result = {key: item for key, item in value.items() if key != "dpi"}
    native = value.get("native_annotations")
    if isinstance(native, dict):
        result["native_annotations"] = _pick(native, _BRIEF_NATIVE_FIELDS)
        result["warnings"] = _warnings(value.get("warnings"), native.get("warnings"))
    elif isinstance(native, list):
        # Export warnings remain attached to each page as well as the overall result.
        result["native_annotations"] = [
            {**_pick(entry, _BRIEF_NATIVE_FIELDS), "warnings": _warnings(entry.get("warnings"))}
            if isinstance(entry, dict)
            else entry
            for entry in native
        ]
        result["warnings"] = _warnings(
            value.get("warnings"),
            *[entry.get("warnings") for entry in native if isinstance(entry, dict)],
        )
    return result


def _brief_evidence(entry: dict[str, Any]) -> dict[str, Any]:
    render = entry.get("render")
    text = entry["text"]
    result = {
        "page": entry["page"],
        **_pick(text, ("document_id", "page_id", "parts", "truncated")),
        "image": entry["image"],
    }
    if isinstance(render, dict):
        result.update(
            {
                ("image_source" if key == "source" else key): value
                for key, value in _pick(
                    render, ("source", "annotation_status", "includes_handwriting")
                ).items()
            }
        )
    render_error = entry.get("render_error")
    if isinstance(render_error, dict):
        result["render_error"] = render_error
    native = render.get("native_annotations") if isinstance(render, dict) else None
    if isinstance(native, dict):
        result["native_annotations"] = _pick(
            native, (field for field in _BRIEF_NATIVE_FIELDS if field not in {"page", "page_id"})
        )
    result["warnings"] = _warnings(
        entry.get("warnings"),
        render.get("warnings") if isinstance(render, dict) else None,
        native.get("warnings") if isinstance(native, dict) else None,
        text.get("warnings"),
    )
    return result


def brief_result(value: dict[str, Any], command_name: str) -> dict[str, Any]:
    """Apply the concise profile without changing unknown command responses."""
    if command_name not in _BRIEF_COMMANDS:
        return value
    result = dict(value)
    if command_name in {"sync", "watch"}:
        for field in ("snapshot", "capture_stats"):
            result.pop(field, None)
    elif command_name in {"ls", "tree"}:
        result["items"] = [_pick(row, _BRIEF_ITEM_FIELDS) for row in value["items"]]
    elif command_name == "info":
        result = _pick(value, (*_BRIEF_ITEM_FIELDS, "snapshot", "warnings", "captured_at"))
    elif command_name in {"pages", "read"}:
        fields = (
            ("id", "document_id", "number", "annotation_count", "recognition")
            if command_name == "pages"
            else ("document_id", "page_id", "page", "parts", "warnings", "truncated")
        )
        result["pages"] = [_pick(row, fields) for row in value["pages"]]
        _hoist_document(result["pages"], result)
    elif command_name in {"render", "export"}:
        result = _brief_render(value)
    elif command_name == "bundle":
        result.pop("directory", None)
        if "page_evidence" in value:
            result["page_evidence"] = [_brief_evidence(row) for row in value["page_evidence"]]
            _hoist_document(result["page_evidence"], result)
    return result


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
    if settings.brief and fields is None and projection is not None and isinstance(result, dict):
        result = brief_result(result, projection.command_name)
    typer.echo(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2 if settings.text else None,
            separators=None if settings.text else (",", ":"),
        )
    )


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
