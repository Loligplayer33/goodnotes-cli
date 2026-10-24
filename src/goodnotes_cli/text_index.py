"""Read-only, set-oriented queries for stored text-index metadata."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import Any

_SQL_ID_BATCH = 400
_TEXT_TABLES = {
    "handwriting": "annotation_text_content",
    "textbox": "textbox_text_content",
    "pdf": "pdf_text_content",
}
_TEXT_LABELS = {"handwriting": "Handwriting", "textbox": "Textbox", "pdf": "PDF"}


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
    return (
        connection is not None
        and table_exists(connection, "page_meta")
        and {
            "page_id",
            "document_id",
            "pdf_text_id",
            "template_attachment_id_and_page_no",
        }
        <= table_columns(connection, "page_meta")
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
        for batch in _chunks(page_ids):
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
            for batch in _chunks(page_ids):
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


def coverage(
    fts: sqlite3.Connection | None,
    page_refs: list[tuple[str, str]],
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

    page_ids = [page_id for _, page_id in page_refs]
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
    return {"page_meta": page_meta, "sources": sources, "warnings": _unique(warnings)}


def _source_coverage(
    fts: sqlite3.Connection | None,
    source: str,
    table: str,
    page_ids: list[str],
    page_refs: list[tuple[str, str]],
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
        for batch in _chunks(page_ids):
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
    fts: sqlite3.Connection, page_refs: list[tuple[str, str]]
) -> tuple[dict[str, Any], list[str]]:
    empty_stats = {
        "pages_with_stored_nonempty_text": None,
        "text_rows": None,
        "text_chars": None,
        "metadata_without_nonempty_text_pages": None,
    }
    if not page_meta_supports_pdf_mapping(fts):
        return empty_stats, [
            "PDF page metadata cannot be mapped because usable `page_meta` mapping is unavailable."
        ]
    pdf_columns = table_columns(fts, "pdf_text_content")
    if not {"text_id", "text"} <= pdf_columns:
        return empty_stats, [
            "PDF text is unavailable because `pdf_text_content` lacks required columns."
        ]
    meta_columns = table_columns(fts, "page_meta")
    template_columns = table_columns(fts, "template_meta")
    has_template_mapping = (
        table_exists(fts, "template_meta")
        and {"attachment_id", "pdf_page_no"} <= template_columns
        and {"attachment_id", "pdf_page_no", "document_id"} <= pdf_columns
    )
    mapping_filter = page_meta_live_filter(meta_columns, alias="p")
    counts: dict[str, tuple[int, int, bool]] = {}
    pair_batch_size = max(1, _SQL_ID_BATCH // 2)
    for start in range(0, len(page_refs), pair_batch_size):
        batch = page_refs[start : start + pair_batch_size]
        scoped_values = ", ".join("(?, ?)" for _ in batch)
        scoped_params = tuple(
            value for document_id, page_id in batch for value in (page_id, document_id)
        )
        direct_rows = fts.execute(
            f"""
            WITH scoped_pages(page_id, document_id) AS (VALUES {scoped_values})
            SELECT p.page_id, COUNT(c.text_id) AS text_rows,
                   SUM(length(COALESCE(c.text, ''))) AS text_chars,
                   MAX(CASE WHEN p.pdf_text_id IS NOT NULL
                                 OR (p.template_attachment_id_and_page_no IS NOT NULL
                                     AND p.template_attachment_id_and_page_no <> '')
                            THEN 1 ELSE 0 END) AS has_mapping
            FROM page_meta AS p
            JOIN scoped_pages AS scope
              ON scope.page_id = p.page_id AND scope.document_id = p.document_id
            LEFT JOIN pdf_text_content AS c
              ON c.text_id = p.pdf_text_id AND COALESCE(c.text, '') <> ''
            WHERE 1 = 1 {mapping_filter}
            GROUP BY p.page_id
            """,
            scoped_params,
        )
        for row in direct_rows:
            counts[row["page_id"]] = (
                int(row["text_rows"]),
                int(row["text_chars"] or 0),
                bool(row["has_mapping"]),
            )
        if has_template_mapping:
            template_rows = fts.execute(
                f"""
                WITH scoped_pages(page_id, document_id) AS (VALUES {scoped_values})
                SELECT page_id, COUNT(text_id) AS text_rows,
                       SUM(text_length) AS text_chars, MAX(has_mapping) AS has_mapping
                FROM (
                    SELECT DISTINCT p.page_id, c.text_id,
                               p.document_id,
                           length(COALESCE(c.text, '')) AS text_length,
                           CASE WHEN p.template_attachment_id_and_page_no IS NOT NULL
                                      AND p.template_attachment_id_and_page_no <> ''
                                THEN 1 ELSE 0 END AS has_mapping
                    FROM page_meta AS p
                        JOIN scoped_pages AS scope
                          ON scope.page_id = p.page_id AND scope.document_id = p.document_id
                    LEFT JOIN template_meta AS t
                      ON p.template_attachment_id_and_page_no =
                         t.attachment_id || '_' || t.pdf_page_no
                    LEFT JOIN pdf_text_content AS c
                      ON c.attachment_id = t.attachment_id
                     AND c.pdf_page_no = t.pdf_page_no
                     AND c.document_id = p.document_id
                     AND COALESCE(c.text, '') <> ''
                        WHERE p.pdf_text_id IS NULL {mapping_filter}
                )
                    GROUP BY page_id
                """,
                scoped_params,
            )
            for row in template_rows:
                page_id = row["page_id"]
                prior = counts.get(page_id, (0, 0, False))
                counts[page_id] = (
                    prior[0] + int(row["text_rows"]),
                    prior[1] + int(row["text_chars"] or 0),
                    prior[2] or bool(row["has_mapping"]),
                )
    pages_with_text = sum(1 for rows, _, _ in counts.values() if rows)
    text_rows = sum(rows for rows, _, _ in counts.values())
    text_chars = sum(chars for _, chars, _ in counts.values())
    metadata_without_text = sum(
        1 for rows, _, has_mapping in counts.values() if has_mapping and not rows
    )
    if not has_template_mapping and "template_attachment_id_and_page_no" in meta_columns:
        warnings = [
            "Template-based PDF text cannot be counted because `template_meta` mapping is "
            "unavailable."
        ]
    else:
        warnings = []
    return {
        "pages_with_stored_nonempty_text": pages_with_text,
        "text_rows": text_rows,
        "text_chars": text_chars,
        "metadata_without_nonempty_text_pages": metadata_without_text,
    }, warnings


def _page_meta_diagnostics(
    fts: sqlite3.Connection | None, page_refs: list[tuple[str, str]]
) -> dict[str, Any]:
    active_page_ids = {page_id for _, page_id in page_refs}
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
    doc_by_page = {page_id: document_id for document_id, page_id in page_refs}
    found: set[str] = set()
    stale: set[str] = set()
    selected = ["page_id"]
    for optional in ("document_id", "is_doc_deleted", "is_page_deleted"):
        if optional in columns:
            selected.append(optional)
    for batch in _chunks(list(active_page_ids)):
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


def _chunks(values: list[str]) -> Iterator[list[str]]:
    for start in range(0, len(values), _SQL_ID_BATCH):
        yield values[start : start + _SQL_ID_BATCH]


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
