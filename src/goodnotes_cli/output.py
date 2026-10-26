"""Compact, bounded output for agent callers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, is_dataclass
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
    field_variants: tuple[tuple[str, tuple[str, ...]], ...] = ()
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
        (*_model_fields(Page), "recognition"),
        row_key="pages",
        example='goodnotes --fields number,recognition pages "Course Notes"',
    ),
    "read": _ProjectionSpec(
        _model_fields(PageText),
        row_key="pages",
        example='goodnotes --fields page,parts,warnings,truncated read "Course Notes" --pages 1',
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
        optional_fields=("native_annotations", "captured_at", "temporary"),
    ),
    "attach-pdf": _ProjectionSpec(
        ("document_id", "path", "pages", "registered", "warnings", "snapshot")
    ),
    "bundle": _ProjectionSpec(
        (
            "directory",
            "manifest",
            "pages",
            "layer",
            "text_source",
            "page_evidence",
            "truncated",
            "snapshot",
        ),
        optional_fields=("captured_at", "temporary", "status", "failed_pages", "warnings"),
    ),
    "doctor": _ProjectionSpec(
        (
            "library_root",
            "active_folders",
            "active_notebooks",
            "active_pages",
            "pages_outside_active_notebooks",
            "deleted_folders",
            "deleted_notebooks",
            "visible_folders",
            "visible_notebooks",
            "orphaned_folders",
            "orphaned_notebooks",
            "orphaned_items",
            "broken_links",
            "deleted_link_rows",
            "links_to_deleted_items",
            "duplicate_parent_items",
            "cycle_folders",
            "pages_without_index_metadata",
            "index_metadata_without_active_pages",
            "search_index_available",
            "search_content_tables",
            "catalog_reference_counts",
            "diagnostic_notes",
            "warnings",
            "storage",
            "native_runtime",
            "provenance",
        ),
        field_variants=(
            (
                "access_only",
                (
                    "scope",
                    "cache_dir",
                    "sandbox",
                    "library",
                    "scratch_dir",
                    "output_dir",
                    "artifact_dir",
                    "status",
                    "checks",
                    "errors",
                    "warnings",
                    "native_runtime",
                ),
            ),
            (
                "notebook",
                (
                    "scope",
                    "page_index",
                    "library",
                    "warnings",
                    "storage",
                    "native_runtime",
                    "provenance",
                ),
            ),
        ),
        example="goodnotes --fields active_pages,warnings doctor",
    ),
}

_BRIEF_COMMANDS = frozenset(
    {"sync", "watch", "ls", "tree", "info", "pages", "read", "render", "export", "bundle"}
)
_BRIEF_ITEM_FIELDS = ("id", "name", "kind", "path", "page_count")
_BRIEF_NATIVE_FIELDS = (
    "page",
    "page_id",
    "annotation_status",
    "coverage",
    "full_fidelity_verified",
    "page_mapping",
    "native_objects",
    "rendered_objects",
    "rendered_strokes",
    "rendered_paths",
    "rendered_images",
    "erased_objects",
    "deleted_objects",
    "unsupported_objects",
    "unsupported_reasons",
    "native_image_body_field_6_absent_records",
)


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
            if command_name == "bundle":
                result["optional_fields"].update(
                    {
                        "status": (
                            "Present as 'partial' when some images succeed and others fail."
                        ),
                        "failed_pages": "One-based pages without an image in a partial bundle.",
                        "warnings": "Explains why page images are missing in a partial bundle.",
                    }
                )
        result.setdefault("optional_fields", {}).update(
            {
                field: f"Requires --{option.replace('_', '-')}"
                for field, option in spec.conditional_fields
            }
        )
        for option, variant_fields in spec.field_variants:
            result.setdefault("optional_fields", {}).update(
                {
                    field: f"Requires --{option.replace('_', '-')}"
                    for field in variant_fields
                    if field not in spec.fields
                }
            )
        if not result["optional_fields"]:
            del result["optional_fields"]
        if spec.example is not None:
            result["example"] = spec.example
        if spec.field_variants:
            result["variants"] = {
                "default": {"fields": list(spec.fields)},
                **{
                    option: {"fields": list(variant_fields)}
                    for option, variant_fields in spec.field_variants
                },
            }
        if command_name in {"sync", "watch"}:
            result["semantics"] = {
                "status": "ready creates a new capture; unchanged reuses the existing capture.",
                "checked_at": "Time the source inventory was checked for this request.",
                "captured_at": "Time the selected copy was captured, not last note edit time.",
                "source_changed_during_refresh": (
                    "Compares source inventories during this refresh only. False does not mean "
                    "no edits since the previous capture, or that remote edits reached this Mac."
                ),
            }
        _describe_default(result, command_name)
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
    _describe_default(row_result, command_name)
    return row_result


def _describe_default(contract: dict[str, Any], command_name: str) -> None:
    if command_name not in _BRIEF_COMMANDS:
        return
    profile: dict[str, Any] = {
        "mode": "concise",
        "full_metadata_option": "--verbose",
        "field_selection": "--fields selects from the full contract and overrides concise output.",
    }
    if command_name in {"sync", "watch"}:
        profile["omitted_fields"] = ["snapshot", "capture_stats"]
        profile["metadata"] = "All other fields, including warnings and errors, are retained."
    elif command_name in {"ls", "tree", "info"}:
        profile["projection"] = "response" if command_name == "info" else "items[]"
        profile["fields"] = list(_BRIEF_ITEM_FIELDS)
        if command_name == "info":
            profile["optional_metadata"] = ["snapshot", "warnings", "captured_at"]
        else:
            profile["metadata"] = "Sibling metadata is retained."
    elif command_name in {"pages", "read"}:
        profile["projection"] = "pages[]"
        profile["fields"] = (
            ["id", "number", "annotation_count", "recognition"]
            if command_name == "pages"
            else ["page_id", "page", "parts", "warnings", "truncated"]
        )
        profile["shared_metadata"] = {
            "document_id": "Moved to the response when all rows share it; otherwise kept per row."
        }
        profile["metadata"] = "Sibling metadata and complete text-part source labels are retained."
        if command_name == "pages":
            profile["recognition"] = (
                "Always includes stored-index counts and state, never candidate text. "
                "No recognition does not mean no visible ink or diagrams."
            )
            profile["annotation_count"] = (
                "Projected note-item rows, not native ink objects. Null means unavailable."
            )
    elif command_name in {"render", "export"}:
        profile["omitted_fields"] = ["dpi"]
        profile["native_annotations"] = {
            "shape": "object" if command_name == "render" else "array",
            "fields": list(_BRIEF_NATIVE_FIELDS),
            "warnings": (
                "All distinct warnings are retained at response level. "
                "Export also keeps warnings per native page."
            ),
        }
        profile["metadata"] = "All other response fields are retained."
    elif command_name == "bundle":
        profile["omitted_fields"] = ["directory"]
        profile["page_evidence"] = {
            "projection": "page_evidence[]",
            "fields": [
                "page",
                "page_id",
                "image",
                "image_source",
                "annotation_status",
                "includes_handwriting",
                "native_annotations",
                "parts",
                "truncated",
                "warnings",
                "render_error",
            ],
            "native_annotations": {
                "fields": [
                    field for field in _BRIEF_NATIVE_FIELDS if field not in {"page", "page_id"}
                ],
                "availability": "Present only for native rendering.",
            },
            "shared_metadata": {
                "document_id": (
                    "Moved to the response when all pages share it; otherwise kept per page."
                )
            },
            "image": (
                "Absolute path to the published PNG, or null for failed pages in a partial bundle."
            ),
            "render_error": (
                "Structured native rendering error for failed pages in a partial bundle."
            ),
            "parts": "Unchanged source-labelled text parts.",
            "warnings": "All distinct render, native, and text warnings retained once per page.",
        }
        profile["metadata"] = (
            "Other response fields are retained. Full diagnostics remain in the saved text.json."
        )
    contract["default_output"] = profile


def validate_projection(raw_fields: str | None, projection: Projection) -> list[str] | None:
    """Validate fields before a command runs and return their parsed order."""
    fields = _parse_fields(raw_fields)
    if fields is None:
        return None
    spec = _PROJECTIONS.get(projection.command_name)
    if spec is not None:
        variants = dict(spec.field_variants)
        selected_variant = next(
            (variants[option] for option in projection.enabled_options if option in variants),
            spec.fields,
        )
        available = set(selected_variant) | set(spec.optional_fields)
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
