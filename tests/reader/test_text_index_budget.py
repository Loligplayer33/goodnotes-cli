"""Generated cases for SQLite bind limits in text-index queries."""

import sqlite3

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.reader.index import sql_batch_size
from goodnotes_cli.reader.text import _PDFPageBindings, search_pdf_rows


def test_pdf_search_respects_runtime_limit_and_preserves_order() -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.create_function("casefold", 1, str.casefold)
    connection.execute(
        "CREATE TABLE pdf_text_content (text_id INTEGER, document_id TEXT, "
        "attachment_id TEXT, pdf_page_no INTEGER, text TEXT)"
    )
    connection.executemany(
        "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
        [(i, "doc", "attachment", i, "MATCH") for i in range(1, 6)],
    )
    bindings: _PDFPageBindings = {
        "available": True,
        "pages": {
            f"page-{i}": {
                "document_id": "doc",
                "attachment_id": "attachment",
                "pdf_page_no": i,
                "state": "stored-text",
                "text_ids": [i],
                "text_rows": 1,
                "text_chars": 5,
                "metadata_has_mapping": True,
                "mismatched_direct_pointer": False,
                "reason": None,
            }
            for i in range(1, 6)
        },
    }
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 11)
    rows = list(search_pdf_rows(connection, bindings, "match"))
    assert [row["sort_id"] for row in rows] == [1, 2, 3, 4, 5]
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 5)
    with pytest.raises(GoodnotesError, match="variable limit is too low"):
        list(search_pdf_rows(connection, bindings, "match"))


def test_batch_size_reserves_fixed_parameters() -> None:
    connection = sqlite3.connect(":memory:")
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 11)
    assert sql_batch_size(connection, per_row=5, fixed=1) == 2
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
    assert sql_batch_size(connection, per_row=5, fixed=1) == 1
