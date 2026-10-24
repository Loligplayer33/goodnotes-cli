"""Read-only, set-oriented queries for stored text-index metadata."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from typing import Any, TypeVar

from .models import PageRef

_SQL_BIND_LIMIT = 400
_TEXT_TABLES = {
    "handwriting": "annotation_text_content",
    "textbox": "textbox_text_content",
    "pdf": "pdf_text_content",
}
_TEXT_LABELS = {"handwriting": "Handwriting", "textbox": "Textbox", "pdf": "PDF"}
_Value = TypeVar("_Value")


def table_exists(connection: sqlite3.Connection | None, name: str) -> bool:
    return (
        connection is not None
        and connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table', 'view') AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()}


def _parse_template_mapping(value: Any) -> tuple[str, int] | None:
    if not isinstance(value, str) or not value:
        return None
    attachment_id, separator, page_text = value.rpartition("_")
    if not separator or not attachment_id:
        return None
    try:
        page_number = int(page_text)
    except (TypeError, ValueError):
        return None
    return (attachment_id, page_number) if page_number > 0 else None


def page_meta_supports_search(connection: sqlite3.Connection | None) -> bool:
    return (
        connection is not None
        and table_exists(connection, "page_meta")
        and {
            "page_id",
            "document_id",
        }
        <= table_columns(connection, "page_meta")
    )


def page_meta_supports_pdf_mapping(connection: sqlite3.Connection | None) -> bool:
    """Whether page ids can be mapped to the current document catalog."""
    return page_meta_supports_search(connection)


def pdf_identity_schema_available(connection: sqlite3.Connection | None) -> bool:
    return (
        connection is not None
        and page_meta_supports_pdf_mapping(connection)
        and table_exists(connection, "pdf_text_content")
        and {
            "text_id",
            "text",
            "document_id",
            "attachment_id",
            "pdf_page_no",
        }
        <= table_columns(connection, "pdf_text_content")
    )


def search_source_available(connection: sqlite3.Connection, source: str) -> bool:
    table = _TEXT_TABLES[source]
    if not table_exists(connection, table):
        return False
    columns = table_columns(connection, table)
    required = {"text"}
    if source in {"handwriting", "textbox"}:
        required |= {"page_id", "text_id"}
    else:
        required.add("text_id")
    return required <= columns


def recognition_status(
    projection: sqlite3.Connection,
    fts: sqlite3.Connection | None,
    page_ids: list[str],
) -> dict[str, dict[str, Any]]:
    """Return per-page row counts and index flags without materializing text values."""
    if not page_ids:
        return {}
    needs_index_by_page: dict[str, Any] = {}
    if "needs_index" in table_columns(projection, "pages"):
        for batch in id_batches(page_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows = projection.execute(
                f"SELECT id, needs_index FROM pages WHERE id IN ({placeholders})",
                batch,
            )
            needs_index_by_page.update({row["id"]: row["needs_index"] for row in rows})

    text_rows_by_page: dict[str, tuple[int, int]] = {}
    content_available = table_exists(fts, "annotation_text_content")
    if content_available and fts is not None:
        columns = table_columns(fts, "annotation_text_content")
        content_available = {"page_id", "text"} <= columns
        if content_available:
            for batch in id_batches(page_ids):
                placeholders = ", ".join("?" for _ in batch)
                rows = fts.execute(
                    f"""
                    SELECT page_id, COUNT(*) AS text_rows,
                           SUM(length(COALESCE(text, ''))) AS text_chars
                    FROM annotation_text_content
                    WHERE page_id IN ({placeholders})
                      AND COALESCE(text, '') <> ''
                    GROUP BY page_id
                    """,
                    batch,
                )
                text_rows_by_page.update(
                    {
                        row["page_id"]: (int(row["text_rows"]), int(row["text_chars"] or 0))
                        for row in rows
                    }
                )

    result: dict[str, dict[str, Any]] = {}
    for page_id in page_ids:
        raw_pending = needs_index_by_page.get(page_id)
        index_state = (
            "unknown" if raw_pending is None else "pending" if bool(raw_pending) else "not-pending"
        )
        if not content_available:
            result[page_id] = {
                "source": "stored-handwriting-recognition",
                "text_rows": None,
                "text_chars": None,
                "availability": "unavailable",
                "index_state": index_state,
            }
        else:
            row_count, char_count = text_rows_by_page.get(page_id, (0, 0))
            result[page_id] = {
                "source": "stored-handwriting-recognition",
                "text_rows": row_count,
                "text_chars": char_count,
                "availability": "stored-text" if row_count else "no-stored-text",
                "index_state": index_state,
            }
    return result


def matches_pdf_identity(row: Mapping[str, Any], page_ref: PageRef) -> bool:
    """Match one stored PDF row to the identity derived from the projection."""
    if not page_ref.attachment_id or page_ref.pdf_page_no is None:
        return False
    try:
        content_page = int(row["pdf_page_no"])
    except (KeyError, TypeError, ValueError):
        return False
    return (
        row["document_id"] == page_ref.document_id
        and row["attachment_id"] == page_ref.attachment_id
        and content_page == page_ref.pdf_page_no
    )


def pdf_page_bindings(fts: sqlite3.Connection | None, page_refs: list[PageRef]) -> dict[str, Any]:
    """Bind active pages to indexed PDF rows only when full PDF identity matches."""
    bindings: dict[str, dict[str, Any]] = {
        ref.page_id: {
            "document_id": ref.document_id,
            "attachment_id": ref.attachment_id,
            "pdf_page_no": ref.pdf_page_no,
            "state": "unknown",
            "text_ids": [],
            "text_rows": None,
            "text_chars": None,
            "metadata_has_mapping": False,
            "mismatched_direct_pointer": False,
            "reason": "mapping-unavailable",
        }
        for ref in page_refs
    }
    unavailable = {"available": False, "pages": bindings}
    if not pdf_identity_schema_available(fts) or fts is None:
        return unavailable

    page_meta_columns = table_columns(fts, "page_meta")
    selected = ["page_id", "document_id"]
    for optional in (
        "pdf_text_id",
        "template_attachment_id_and_page_no",
        "is_doc_deleted",
        "is_page_deleted",
    ):
        if optional in page_meta_columns:
            selected.append(optional)
    metadata_by_page: dict[str, sqlite3.Row] = {}
    page_ids = [ref.page_id for ref in page_refs]
    for page_id_batch in id_batches(page_ids):
        placeholders = ", ".join("?" for _ in page_id_batch)
        rows = fts.execute(
            f"SELECT {', '.join(selected)} FROM page_meta WHERE page_id IN ({placeholders})",
            page_id_batch,
        )
        metadata_by_page.update({row["page_id"]: row for row in rows})

    eligible: dict[str, PageRef] = {}
    direct_id_to_pages: dict[Any, list[PageRef]] = {}
    for page_ref in page_refs:
        binding = bindings[page_ref.page_id]
        if not page_ref.attachment_id or page_ref.pdf_page_no is None:
            binding["reason"] = "projection-pdf-identity-unavailable"
            continue
        metadata = metadata_by_page.get(page_ref.page_id)
        if metadata is None:
            binding["reason"] = "page-metadata-missing"
            continue
        if metadata["document_id"] != page_ref.document_id:
            binding["reason"] = "page-metadata-document-mismatch"
            binding["mismatched_direct_pointer"] = bool(
                "pdf_text_id" in page_meta_columns and metadata["pdf_text_id"] is not None
            )
            continue
        if ("is_doc_deleted" in page_meta_columns and bool(metadata["is_doc_deleted"])) or (
            "is_page_deleted" in page_meta_columns and bool(metadata["is_page_deleted"])
        ):
            binding["reason"] = "page-metadata-marks-page-deleted"
            continue
        template_mapping = (
            metadata["template_attachment_id_and_page_no"]
            if ("template_attachment_id_and_page_no" in page_meta_columns)
            else None
        )
        if template_mapping:
            parsed = _parse_template_mapping(template_mapping)
            if parsed is None or parsed != (page_ref.attachment_id, page_ref.pdf_page_no):
                binding["reason"] = "page-metadata-pdf-identity-mismatch"
                continue
        direct_id = metadata["pdf_text_id"] if "pdf_text_id" in page_meta_columns else None
        binding["metadata_has_mapping"] = bool(direct_id is not None or template_mapping)
        binding["reason"] = None
        eligible[page_ref.page_id] = page_ref
        if direct_id is not None:
            direct_id_to_pages.setdefault(direct_id, []).append(page_ref)

    candidate_lengths: dict[str, dict[int, int]] = {page_id: {} for page_id in eligible}
    direct_ids = list(direct_id_to_pages)
    for direct_id_batch in batches(direct_ids):
        placeholders = ", ".join("?" for _ in direct_id_batch)
        rows = fts.execute(
            f"""
            SELECT text_id, document_id, attachment_id, pdf_page_no,
                   length(COALESCE(text, '')) AS text_length
            FROM pdf_text_content
            WHERE text_id IN ({placeholders})
            """,
            direct_id_batch,
        )
        rows_by_id = {row["text_id"]: row for row in rows}
        for direct_id in direct_id_batch:
            row = rows_by_id.get(direct_id)
            for page_ref in direct_id_to_pages[direct_id]:
                if row is not None and matches_pdf_identity(dict(row), page_ref):
                    text_length = int(row["text_length"] or 0)
                    if text_length:
                        candidate_lengths[page_ref.page_id][int(direct_id)] = text_length
                else:
                    bindings[page_ref.page_id]["mismatched_direct_pointer"] = True

    eligible_refs = [
        page_ref for page_ref in eligible.values() if not candidate_lengths[page_ref.page_id]
    ]
    ref_batch_size = max(1, _SQL_BIND_LIMIT // 4)
    page_meta_filter = page_meta_live_filter(page_meta_columns, alias="p")
    for start in range(0, len(eligible_refs), ref_batch_size):
        ref_batch = eligible_refs[start : start + ref_batch_size]
        scoped_values = ", ".join("(?, ?, ?, ?)" for _ in ref_batch)
        scoped_params = tuple(
            value
            for page_ref in ref_batch
            for value in (
                page_ref.page_id,
                page_ref.document_id,
                page_ref.attachment_id,
                page_ref.pdf_page_no,
            )
        )
        rows = fts.execute(
            f"""
            WITH expected(page_id, document_id, attachment_id, pdf_page_no) AS
                (VALUES {scoped_values})
            SELECT e.page_id, c.text_id, c.document_id, c.attachment_id, c.pdf_page_no,
                   length(COALESCE(c.text, '')) AS text_length
            FROM expected AS e
            JOIN page_meta AS p
              ON p.page_id = e.page_id AND p.document_id = e.document_id
            JOIN pdf_text_content AS c
              ON c.document_id = e.document_id
             AND c.attachment_id = e.attachment_id
             AND c.pdf_page_no = e.pdf_page_no
            WHERE COALESCE(c.text, '') <> '' {page_meta_filter}
            """,
            scoped_params,
        )
        ref_by_page = {ref.page_id: ref for ref in ref_batch}
        for row in rows:
            candidate_page_ref = ref_by_page.get(row["page_id"])
            if candidate_page_ref is not None and matches_pdf_identity(
                dict(row), candidate_page_ref
            ):
                candidate_lengths[candidate_page_ref.page_id][int(row["text_id"])] = int(
                    row["text_length"] or 0
                )

    for page_id, lengths in candidate_lengths.items():
        binding = bindings[page_id]
        text_ids = sorted(lengths)
        binding["text_ids"] = text_ids
        binding["text_rows"] = len(text_ids)
        binding["text_chars"] = sum(lengths.values())
        binding["state"] = "stored-text" if text_ids else "no-stored-text"
    return {"available": True, "pages": bindings}


def coverage(
    fts: sqlite3.Connection | None,
    page_refs: list[PageRef],
    requested_sources: list[str] | None = None,
) -> dict[str, Any]:
    """Return scoped mapping diagnostics and aggregates for requested text sources."""
    page_meta = _page_meta_diagnostics(fts, page_refs)
    sources: dict[str, dict[str, Any]] = {}
    warnings: list[str] = []
    if not page_meta["available"]:
        warnings.append("Page metadata cannot map stored text to active pages in this scope.")
    elif page_meta["active_pages_without_metadata"]:
        warnings.append(
            f"{page_meta['active_pages_without_metadata']} active pages in this scope have no "
            "usable page-metadata row."
        )
    if page_meta.get("stale_or_mismatched_pages", 0):
        warnings.append(
            f"{page_meta['stale_or_mismatched_pages']} active pages in this scope have stale or "
            "mismatched page metadata."
        )

    page_ids = [page_ref.page_id for page_ref in page_refs]
    source_names = list(_TEXT_TABLES) if requested_sources is None else requested_sources
    for source in source_names:
        table = _TEXT_TABLES[source]
        details, source_warnings = _source_coverage(fts, source, table, page_ids, page_refs)
        sources[source] = details
        warnings.extend(source_warnings)
        if details.get("metadata_without_nonempty_text_pages", 0):
            warnings.append(
                f"{details['metadata_without_nonempty_text_pages']} PDF page mappings in this "
                "scope have no non-empty indexed PDF text."
            )
    return {"page_meta": page_meta, "sources": sources, "warnings": list(dict.fromkeys(warnings))}


def _source_coverage(
    fts: sqlite3.Connection | None,
    source: str,
    table: str,
    page_ids: list[str],
    page_refs: list[PageRef],
) -> tuple[dict[str, Any], list[str]]:
    label = _TEXT_LABELS[source]
    details: dict[str, Any] = {
        "availability": "unavailable",
        "pages_with_stored_nonempty_text": None,
        "text_rows": None,
        "text_chars": None,
        "completeness": "unknown",
    }
    if not table_exists(fts, table) or fts is None:
        return details, [f"{label} text is unavailable in the stored search index."]

    columns = table_columns(fts, table)
    required = {"text"}
    if source in {"handwriting", "textbox"}:
        required.add("page_id")
    if not required <= columns:
        return details, [f"{label} text is unavailable because `{table}` lacks required columns."]

    details["availability"] = "available"
    if source in {"handwriting", "textbox"}:
        counts: dict[str, tuple[int, int]] = {}
        for batch in id_batches(page_ids):
            placeholders = ", ".join("?" for _ in batch)
            rows = fts.execute(
                f"""
                SELECT page_id, COUNT(*) AS text_rows,
                       SUM(length(COALESCE(text, ''))) AS text_chars
                FROM {table}
                WHERE page_id IN ({placeholders})
                  AND COALESCE(text, '') <> ''
                GROUP BY page_id
                """,
                batch,
            )
            counts.update(
                {
                    row["page_id"]: (int(row["text_rows"]), int(row["text_chars"] or 0))
                    for row in rows
                }
            )
        details["pages_with_stored_nonempty_text"] = len(counts)
        details["text_rows"] = sum(rows for rows, _ in counts.values())
        details["text_chars"] = sum(chars for _, chars in counts.values())
        return details, []

    pdf_stats, warnings = _pdf_coverage(fts, page_refs)
    details.update(pdf_stats)
    if pdf_stats["pages_with_stored_nonempty_text"] is None:
        details["availability"] = "unavailable"
    return details, warnings


def _pdf_coverage(
    fts: sqlite3.Connection | None, page_refs: list[PageRef]
) -> tuple[dict[str, Any], list[str]]:
    empty_stats = {
        "pages_with_stored_nonempty_text": None,
        "text_rows": None,
        "text_chars": None,
        "metadata_without_nonempty_text_pages": None,
        "unverifiable_pages": len(page_refs),
        "mismatched_direct_pointers": None,
    }
    binding_result = pdf_page_bindings(fts, page_refs)
    if not binding_result["available"]:
        return empty_stats, [
            "PDF text identity cannot be verified because required page or content metadata "
            "is unavailable."
        ]
    bindings = binding_result["pages"]
    values = list(bindings.values())
    mapped = [binding for binding in values if binding["state"] == "stored-text"]
    no_text_with_mapping = [
        binding
        for binding in values
        if binding["state"] == "no-stored-text" and binding["metadata_has_mapping"]
    ]
    unverifiable = [binding for binding in values if binding["state"] == "unknown"]
    mismatched = [binding for binding in values if binding["mismatched_direct_pointer"]]
    warnings: list[str] = []
    if unverifiable:
        warnings.append(
            f"PDF identity could not be verified for {len(unverifiable)} active pages in this "
            "scope."
        )
    if mismatched:
        warnings.append(
            f"{len(mismatched)} PDF direct pointers did not match the current projection "
            "identity; only independently verified text is counted."
        )
    return {
        "pages_with_stored_nonempty_text": len(mapped),
        "text_rows": sum(int(binding["text_rows"] or 0) for binding in mapped),
        "text_chars": sum(int(binding["text_chars"] or 0) for binding in mapped),
        "metadata_without_nonempty_text_pages": len(no_text_with_mapping),
        "unverifiable_pages": len(unverifiable),
        "mismatched_direct_pointers": len(mismatched),
    }, warnings


def _page_meta_diagnostics(
    fts: sqlite3.Connection | None, page_refs: list[PageRef]
) -> dict[str, Any]:
    active_page_ids = {page_ref.page_id for page_ref in page_refs}
    empty = {
        "available": False,
        "active_pages": len(active_page_ids),
        "mapped_active_pages": None,
        "active_pages_without_metadata": None,
        "stale_or_mismatched_pages": None,
    }
    if not table_exists(fts, "page_meta") or fts is None:
        return empty
    columns = table_columns(fts, "page_meta")
    if "page_id" not in columns:
        return empty
    valid_mapping = {"page_id", "document_id"} <= columns
    doc_by_page = {page_ref.page_id: page_ref.document_id for page_ref in page_refs}
    found: set[str] = set()
    stale: set[str] = set()
    selected = ["page_id"]
    for optional in ("document_id", "is_doc_deleted", "is_page_deleted"):
        if optional in columns:
            selected.append(optional)
    for batch in id_batches(list(active_page_ids)):
        placeholders = ", ".join("?" for _ in batch)
        rows = fts.execute(
            f"SELECT {', '.join(selected)} FROM page_meta WHERE page_id IN ({placeholders})",
            batch,
        )
        for row in rows:
            page_id = row["page_id"]
            found.add(page_id)
            mismatch = "document_id" in columns and row["document_id"] != doc_by_page.get(page_id)
            deleted = ("is_doc_deleted" in columns and bool(row["is_doc_deleted"])) or (
                "is_page_deleted" in columns and bool(row["is_page_deleted"])
            )
            if mismatch or deleted:
                stale.add(page_id)
    return {
        "available": valid_mapping,
        "active_pages": len(active_page_ids),
        "mapped_active_pages": len(found - stale),
        "active_pages_without_metadata": len(active_page_ids - found),
        "stale_or_mismatched_pages": len(stale),
    }


def page_meta_live_filter(columns: set[str], *, alias: str) -> str:
    filters = [
        f"COALESCE({alias}.{column}, 0) = 0"
        for column in ("is_doc_deleted", "is_page_deleted")
        if column in columns
    ]
    return "AND " + " AND ".join(filters) if filters else ""


def search_pdf_rows(
    fts: sqlite3.Connection,
    binding_result: dict[str, Any],
    query: str,
    document_ids: list[str] | None = None,
) -> Iterator[sqlite3.Row]:
    """Search only rows already bound to projection-derived page identities."""
    if not binding_result["available"]:
        return
    selected_documents = set(document_ids) if document_ids is not None else None
    bound_rows = [
        (
            page_id,
            binding["document_id"],
            binding["attachment_id"],
            binding["pdf_page_no"],
            text_id,
        )
        for page_id, binding in binding_result["pages"].items()
        if binding["state"] == "stored-text"
        and (selected_documents is None or binding["document_id"] in selected_documents)
        for text_id in binding["text_ids"]
    ]
    batch_size = max(1, _SQL_BIND_LIMIT // 6)
    for batch in batches(bound_rows, batch_size):
        values_sql = ", ".join("(?, ?, ?, ?, ?)" for _ in batch)
        parameters = tuple(value for row in batch for value in row) + (query,)
        yield from fts.execute(
            f"""
            WITH bound_rows(page_id, document_id, attachment_id, pdf_page_no, text_id)
                AS (VALUES {values_sql})
            SELECT bound.document_id, bound.page_id, content.text,
                   content.text_id AS sort_id
            FROM bound_rows AS bound
            JOIN pdf_text_content AS content
              ON content.text_id = bound.text_id
             AND content.document_id = bound.document_id
             AND content.attachment_id = bound.attachment_id
             AND content.pdf_page_no = bound.pdf_page_no
            WHERE COALESCE(content.text, '') <> ''
              AND instr(casefold(content.text), casefold(?)) > 0
            ORDER BY content.text_id
            """,
            parameters,
        )


def id_batches(values: list[str]) -> Iterator[list[str]]:
    yield from batches(values)


def batches(values: list[_Value], size: int = _SQL_BIND_LIMIT) -> Iterator[list[_Value]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
