"""Output field descriptors and projection validation."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import fields as dataclass_fields
from typing import Any

from ..errors import GoodnotesError
from ..models import Item, Page, PageText, SearchHit


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
        optional_fields=(
            "document_id",
            "captured_at",
            "temporary",
            "status",
            "failed_pages",
            "warnings",
        ),
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
        variant_options: dict[str, list[str]] = {}
        for option, variant_fields in spec.field_variants:
            for field in variant_fields:
                if field not in spec.fields:
                    variant_options.setdefault(field, []).append(f"--{option.replace('_', '-')}")
        result["optional_fields"].update(
            {
                field: f"Requires {' or '.join(options)}"
                for field, options in variant_options.items()
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
        selected_variant = next(
            (
                fields
                for option, fields in spec.field_variants
                if option in projection.enabled_options
            ),
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


def _parse_fields(raw_fields: str | None) -> list[str] | None:
    if raw_fields is None:
        return None
    fields = [field.strip() for field in raw_fields.split(",") if field.strip()]
    if not fields:
        raise GoodnotesError("--fields must contain at least one field name.")
    return fields
