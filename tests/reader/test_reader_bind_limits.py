"""Generated snapshot cases for SQLite's runtime variable limit."""

import sqlite3

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from goodnotes_cli.reader.attachments import AttachmentResolver
from goodnotes_cli.reader.catalog import Catalog
from goodnotes_cli.reader.pages import PageReader
from goodnotes_cli.reader.pdf import PdfTextReader
from goodnotes_cli.reader.read import PageTextReader
from goodnotes_cli.reader.session import ReaderSession
from tests.support.library import _create_snapshot


def test_page_hydration_batches_each_kind_of_reference_at_runtime_limit(tmp_path):
    sandbox = _create_snapshot(tmp_path / "snapshot")
    session = ReaderSession(sandbox)
    session.open()
    try:
        pages = PageReader(session, AttachmentResolver(sandbox.attachments_dir))
        expected = pages.pages("doc-hostile")
        pages.clear()
        assert session.projection is not None
        session.projection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
        actual = pages.pages("doc-hostile")
        assert actual == expected
        refs = pages.page_refs_for_notebooks(
            {"doc-hostile", "other"}, page_ids={"page-02", "page-10", "page-30"}
        )
        assert {ref.page_id for ref in refs} == {"page-02", "page-10", "page-30"}
    finally:
        session.close()


def test_bounded_text_read_checks_fixed_bind_count(tmp_path):
    sandbox = _create_snapshot(tmp_path / "snapshot")
    session = ReaderSession(sandbox)
    session.open()
    try:
        session.ensure_fts()
        assert session.fts is not None
        attachments = AttachmentResolver(sandbox.attachments_dir)
        pages = PageReader(session, attachments)
        reader = PageTextReader(
            session, Catalog(session), pages, PdfTextReader(session, pages, attachments)
        )
        expected = reader._read_text_table("textbox", "page-02", 100)
        session.fts.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 7)
        assert reader._read_text_table("textbox", "page-02", 100) == expected
        session.fts.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 6)
        with pytest.raises(GoodnotesError, match="SQLite variable limit is too low"):
            reader._read_text_table("textbox", "page-02", 100)
        assert reader._read_text_table("textbox", "page-02", 0)[2:4] == (True, True)
    finally:
        session.close()


def test_page_refs_preserve_original_batch_order_under_runtime_limit(tmp_path):
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [(f"id-{i:03}", "doc-hostile", None, f"{300 - i:03}", None, 0) for i in range(301)],
        )
    with Library(sandbox) as library:
        connection = library._session.projection
        assert connection is not None
        connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 10)
        refs = library._pages.page_refs_for_notebooks(
            {"doc-hostile"}, page_ids={f"id-{i:03}" for i in range(301)}
        )
        # Original query batches sorted page IDs in groups of 300, sorting each in SQL order.
        assert [ref.page_id for ref in refs] == [f"id-{i:03}" for i in range(299, -1, -1)] + [
            "id-300"
        ]


def test_page_ref_split_matches_sqlite_order_for_mixed_positions(tmp_path):
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"mixed-{i}", "doc-hostile", None, value, None, 0)
                for i, value in enumerate((b"blob", "z", "A", 2, -1, ""))
            ],
        )
        expected = [
            row[0]
            for row in connection.execute(
                "SELECT id FROM pages WHERE id LIKE 'mixed-%' ORDER BY document_id, position, id"
            )
        ]
    with Library(sandbox) as library:
        assert library._session.projection is not None
        library._session.projection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 3)
        result = library._pages.page_refs_for_notebooks(
            {"doc-hostile"}, page_ids={f"mixed-{i}" for i in range(6)}
        )
    assert [page.page_id for page in result] == expected


def test_split_page_refs_preserve_declared_sql_collation(tmp_path):
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute("ALTER TABLE pages RENAME TO old_pages")
        connection.execute(
            "CREATE TABLE pages (id TEXT PRIMARY KEY, document_id TEXT NOT NULL, "
            "template_id TEXT, position TEXT COLLATE NOCASE NOT NULL, group_id TEXT, "
            "deleted INTEGER DEFAULT 0)"
        )
        connection.execute("INSERT INTO pages SELECT * FROM old_pages")
        connection.execute("DROP TABLE old_pages")
        connection.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [("x1", "doc-hostile", None, "a", None, 0), ("x2", "doc-hostile", None, "Z", None, 0)],
        )
    with Library(sandbox) as library:
        assert library._session.projection is not None
        library._session.projection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
        refs = library._pages.page_refs_for_notebooks({"doc-hostile"}, page_ids={"x1", "x2"})
        assert [ref.page_id for ref in refs] == ["x1", "x2"]


def test_page_ref_reordering_ignores_unselected_malformed_rows_and_validates_in_order(tmp_path):
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [("", "doc-hostile", None, "a", None, 0), ("valid", "doc-hostile", None, "z", None, 0)],
        )
    with Library(sandbox) as library:
        assert library._session.projection is not None
        library._session.projection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 2)
        refs = library._pages.page_refs_for_notebooks(
            {"doc-hostile"}, page_ids={"valid", "page-02"}
        )
        assert {ref.page_id for ref in refs} == {"valid", "page-02"}
        with pytest.raises(GoodnotesError, match="pages.id"):
            library._pages.page_refs_for_notebooks({"doc-hostile"}, page_ids={"", "valid"})


def test_single_page_ref_uses_one_data_query(tmp_path):
    with Library(_create_snapshot(tmp_path)) as library:
        assert library._session.projection is not None
        statements = []
        library._session.projection.set_trace_callback(statements.append)
        library._pages.page_refs_for_notebooks({"doc-hostile"}, page_ids={"page-02"})
    reads = [statement for statement in statements if "SELECT p.id AS page_id" in statement]
    assert len(reads) == 1
