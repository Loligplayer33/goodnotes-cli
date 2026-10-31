"""Exercise search collaborators with generated snapshot data."""

import gc
import json
import sqlite3
import sys
from pathlib import Path
from types import GeneratorType
from typing import Any

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from goodnotes_cli.reader import search as search_module
from goodnotes_cli.reader import text as text_queries
from goodnotes_cli.reader.attachments import AttachmentResolver
from goodnotes_cli.reader.catalog import Catalog
from goodnotes_cli.reader.pages import PageReader
from goodnotes_cli.reader.pdf import PdfTextReader
from goodnotes_cli.reader.scope import ScopeResolver
from goodnotes_cli.reader.search import SearchReader
from goodnotes_cli.reader.session import ReaderSession
from tests.support.library import _create_snapshot


def test_search_and_scope_collaborators(tmp_path):
    sandbox = _create_snapshot(tmp_path / "snapshot")
    session = ReaderSession(sandbox)
    session.open()
    try:
        catalog = Catalog(session)
        catalog.load()
        attachments = AttachmentResolver(sandbox.attachments_dir)
        pages = PageReader(session, attachments)
        pdf = PdfTextReader(session, pages, attachments)
        scope = ScopeResolver(catalog)
        reader = SearchReader(session, catalog, pages, scope, pdf)
        hit = reader.search("STRASSE", source="handwriting")[0]
        assert (hit.text, hit.page, hit.source) == ("Straße | Strasse", 1, "handwriting")
        assert reader.search("recognized", folder="Shared B") == []
        assert reader.search("direct PDF phrase", source="pdf")[0].page == 1
        with pytest.raises(GoodnotesError, match="--notebook"):
            scope.scope("doc-root", None)
        with pytest.raises(GoodnotesError, match="either"):
            scope.scope("Shared A", "doc-root")
    finally:
        session.close()


@pytest.fixture
def search_cursors(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Observe real query cursors without changing the generated SQL or rows."""
    cursors: list[Any] = []
    connect = sqlite3.connect

    class Cursor(sqlite3.Cursor):
        close_checks: list[bool]

        def close(self) -> None:
            try:
                usable = self.connection.execute("SELECT 1").fetchone()[0] == 1
            except sqlite3.ProgrammingError:
                usable = False
            self.close_checks.append(usable)
            super().close()

    class Connection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            if "SELECT p.document_id" in sql or "WITH bound_rows(" in sql:
                cursor = self.cursor(factory=Cursor)
                cursor.close_checks = []
                cursors.append(cursor)
                return cursor.execute(sql, parameters)
            return super().execute(sql, parameters)

    def tracked_connect(*args, **kwargs):
        kwargs.setdefault("factory", Connection)
        return connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", tracked_connect)
    return cursors


@pytest.mark.parametrize(
    "source, query", [("handwriting", "recognized"), ("textbox", "phrase"), ("pdf", "phrase")]
)
@pytest.mark.parametrize("failure", [KeyboardInterrupt, RuntimeError])
def test_search_closes_retained_producers_before_session_teardown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    search_cursors: list[Any],
    source: str,
    query: str,
    failure: type[BaseException],
) -> None:
    sandbox = _create_snapshot(tmp_path)
    retained: list[GeneratorType] = []
    batches: list[list[str]] = []
    unraisable: list[str] = []
    monkeypatch.setattr(
        sys, "unraisablehook", lambda event: unraisable.append(str(event.exc_value))
    )
    caught: BaseException | None = None

    def interrupt(*_args):
        raise failure("consumer stopped")

    with Library(sandbox) as library:
        monkeypatch.setattr(library._search, "_search_batch_size", lambda: 1)
        original = text_queries.search_pdf_rows if source == "pdf" else library._search._search_rows

        def track(*args):
            producer = original(*args)
            assert isinstance(producer, GeneratorType)
            retained.append(producer)
            batches.append(list(args[-1]))
            return producer

        if source == "pdf":
            monkeypatch.setattr(text_queries, "search_pdf_rows", track)
        else:
            monkeypatch.setattr(library._search, "_search_rows", track)
        monkeypatch.setattr(search_module.heapq, "heappush", interrupt)
        try:
            library.search(query, source=source)
        except failure as exc:
            caught = exc
        assert caught is not None
        # Retain the real producer and consumer traceback until the Library closes.
        completed_before_exit = [producer.gi_frame is None for producer in retained]
        checks_before_exit = [list(cursor.close_checks) for cursor in search_cursors]

    caught = None
    retained.clear()
    gc.collect()
    assert completed_before_exit and all(completed_before_exit)
    assert checks_before_exit and all(checks == [True] for checks in checks_before_exit)
    assert [batch[0] for batch in batches] == ["doc-a", "doc-b", "doc-hostile"]
    assert not library._session.is_open
    assert unraisable == []


@pytest.mark.parametrize("source", ["handwriting", "textbox"])
def test_recursive_ordinary_search_producer_closes_leaf_cursor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, search_cursors: list[Any], source: str
) -> None:
    with Library(_create_snapshot(tmp_path)) as library:
        library._session.ensure_fts()
        monkeypatch.setattr(library._search, "_search_batch_size", lambda: 1)
        query = "recognized" if source == "handwriting" else "phrase"
        producer = library._search._search_rows(source, query, ["doc-hostile", "doc-root"])
        next(producer)
        producer.close()
        assert [cursor.close_checks for cursor in search_cursors] == [[True]]


@pytest.mark.parametrize("source", ["handwriting", "textbox"])
@pytest.mark.parametrize("text_id", [None, 1.5, "7", "invalid-id"])
def test_search_rejects_reached_noninteger_sort_id(
    tmp_path: Path, source: str, text_id: Any
) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    table = "annotation_text_content" if source == "handwriting" else "textbox_text_content"
    with sqlite3.connect(sandbox.fts_path) as connection:
        extra_columns = ", engine_type TEXT, priority TEXT" if source == "handwriting" else ""
        extra_values = ", 'myscript', 'primary'" if source == "handwriting" else ""
        connection.execute(f"DROP TABLE {table}")
        connection.execute(
            f"CREATE TABLE {table} (text_id, page_id TEXT, text TEXT{extra_columns})"
        )
        connection.execute(
            f"INSERT INTO {table} VALUES (?, 'page-02', 'needle'{extra_values})", (text_id,)
        )
    with Library(sandbox) as library:
        with pytest.raises(GoodnotesError, match=rf"{table}\.text_id.*expected an integer"):
            library.search("needle", source=source, notebook="doc-hostile")


def test_malformed_search_id_has_structured_stderr_without_late_traceback(tmp_path: Path) -> None:
    from tests.support.cli import invoke

    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        connection.execute("DROP TABLE textbox_text_content")
        connection.execute("CREATE TABLE textbox_text_content (text_id, page_id TEXT, text TEXT)")
        connection.execute(
            "INSERT INTO textbox_text_content VALUES ('invalid-id', 'page-02', 'needle')"
        )
    result = invoke(
        ["search", "needle", "--source", "textbox", "--notebook", "doc-hostile"],
        tmp_path,
        sandbox=sandbox.root,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"
    assert "textbox_text_content.text_id" in result.stderr
    assert "Traceback" not in result.stderr
    assert "Exception ignored" not in result.stderr
