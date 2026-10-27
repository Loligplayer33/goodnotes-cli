"""Schema metadata, source selection, and bounded SQL batching."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from typing import TypeVar

from ..errors import GoodnotesError

SQL_BIND_LIMIT = 400


TEXT_TABLES = {
    "handwriting": "annotation_text_content",
    "textbox": "textbox_text_content",
    "pdf": "pdf_text_content",
}


TEXT_LABELS = {"handwriting": "Handwriting", "textbox": "Textbox", "pdf": "PDF"}


_TEXT_SOURCE_CHOICES = ("all", "text", *TEXT_TABLES)


_Value = TypeVar("_Value")


def selected_text_sources(source: str) -> tuple[str, ...]:
    """Expand a source selector into the concrete stored-text sources it requests."""
    normalized = source.casefold() if isinstance(source, str) else ""
    if normalized == "all":
        return tuple(TEXT_TABLES)
    if normalized == "text":
        return ("pdf", "textbox")
    if normalized in TEXT_TABLES:
        return (normalized,)
    choices = ", ".join(_TEXT_SOURCE_CHOICES)
    raise GoodnotesError(f"Text source must be one of: {choices}.")


class SchemaCache:
    """Cache immutable schema metadata for one read-only SQLite session."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection
        self._tables: set[str] | None = None
        self._columns: dict[str, set[str]] = {}

    def table_exists(self, name: str) -> bool:
        if self._tables is None:
            self._tables = {
                str(row[0])
                for row in self.connection.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                )
            }
        return name in self._tables

    def table_columns(self, table: str) -> set[str]:
        if table not in self._columns:
            if not self.table_exists(table):
                self._columns[table] = set()
            else:
                quoted = '"' + table.replace('"', '""') + '"'
                self._columns[table] = {
                    str(row[1]) for row in self.connection.execute(f"PRAGMA table_info({quoted})")
                }
        return set(self._columns[table])


def table_exists(
    connection: sqlite3.Connection | None,
    name: str,
    *,
    schema: SchemaCache | None = None,
) -> bool:
    return connection is not None and (
        schema.table_exists(name)
        if schema is not None
        else SchemaCache(connection).table_exists(name)
    )


def table_columns(
    connection: sqlite3.Connection,
    table: str,
    *,
    schema: SchemaCache | None = None,
) -> set[str]:
    return (schema or SchemaCache(connection)).table_columns(table)


def page_meta_supports_search(
    connection: sqlite3.Connection | None, *, schema: SchemaCache | None = None
) -> bool:
    return (
        connection is not None
        and table_exists(connection, "page_meta", schema=schema)
        and {
            "page_id",
            "document_id",
        }
        <= table_columns(connection, "page_meta", schema=schema)
    )


def page_meta_supports_pdf_mapping(
    connection: sqlite3.Connection | None, *, schema: SchemaCache | None = None
) -> bool:
    """Whether page ids can be mapped to the current document catalog."""
    return page_meta_supports_search(connection, schema=schema)


def pdf_identity_schema_available(
    connection: sqlite3.Connection | None, *, schema: SchemaCache | None = None
) -> bool:
    return (
        connection is not None
        and page_meta_supports_pdf_mapping(connection, schema=schema)
        and table_exists(connection, "pdf_text_content", schema=schema)
        and {
            "text_id",
            "text",
            "document_id",
            "attachment_id",
            "pdf_page_no",
        }
        <= table_columns(connection, "pdf_text_content", schema=schema)
    )


def search_source_available(
    connection: sqlite3.Connection,
    source: str,
    *,
    schema: SchemaCache | None = None,
) -> bool:
    table = TEXT_TABLES[source]
    if not table_exists(connection, table, schema=schema):
        return False
    columns = table_columns(connection, table, schema=schema)
    required = {"text"}
    if source in {"handwriting", "textbox"}:
        required |= {"page_id", "text_id"}
    else:
        required.add("text_id")
    return required <= columns


def page_meta_live_filter(columns: set[str], *, alias: str) -> str:
    filters = [
        f"COALESCE({alias}.{column}, 0) = 0"
        for column in ("is_doc_deleted", "is_page_deleted")
        if column in columns
    ]
    return "AND " + " AND ".join(filters) if filters else ""


def sql_batch_size(connection: sqlite3.Connection, *, per_row: int = 1, fixed: int = 0) -> int:
    """Return a row count that fits both the configured and SQLite bind limits."""
    limit = min(SQL_BIND_LIMIT, connection.getlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER))
    size = (limit - fixed) // per_row
    if size < 1:
        raise GoodnotesError("SQLite variable limit is too low for the requested text index query.")
    return size


def id_batches(values: list[str], size: int = SQL_BIND_LIMIT) -> Iterator[list[str]]:
    yield from batches(values, size)


def batches(values: list[_Value], size: int = SQL_BIND_LIMIT) -> Iterator[list[_Value]]:
    for start in range(0, len(values), size):
        yield values[start : start + size]
