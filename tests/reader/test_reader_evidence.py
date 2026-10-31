"""Generated malformed evidence must remain distinct from absent stored text."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from goodnotes_cli.models import PageRef
from goodnotes_cli.reader.index import TEXT_TABLES
from tests.support.library import _create_snapshot


def _nullable_content(connection: sqlite3.Connection, source: str) -> str:
    table = TEXT_TABLES[source]
    extra = ", engine_type TEXT, priority TEXT" if source == "handwriting" else ""
    connection.execute(f"DROP TABLE {table}")
    connection.execute(f"CREATE TABLE {table} (text_id, page_id TEXT, text TEXT{extra})")
    return table


@pytest.mark.parametrize("source", ["handwriting", "textbox"])
@pytest.mark.parametrize("budget", [None, 4, 0])
def test_read_rejects_reached_null_text_id(tmp_path: Path, source: str, budget: int | None) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        table = _nullable_content(connection, source)
        extra = ", 'myscript', 'primary'" if source == "handwriting" else ""
        connection.execute(f"INSERT INTO {table} VALUES (NULL, 'page-02', 'needle'{extra})")
    with Library(sandbox) as library:
        with pytest.raises(GoodnotesError, match=rf"{table}\.text_id.*expected an integer"):
            library.read_page("doc-hostile", 1, source=source, max_chars=budget)


@pytest.mark.parametrize("budget", [0, 4, 5, 6, None])
def test_read_validates_null_id_only_when_budget_reaches_it(
    tmp_path: Path, budget: int | None
) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        table = _nullable_content(connection, "handwriting")
        connection.executemany(
            f"INSERT INTO {table} VALUES (?, 'page-02', ?, 'myscript', ?)",
            [(1, "first", "primary"), (None, "needle", "secondary")],
        )
    with Library(sandbox) as library:
        if budget is None or budget > 5:
            with pytest.raises(GoodnotesError, match=r"annotation_text_content\.text_id"):
                library.read_page("doc-hostile", 1, source="handwriting", max_chars=budget)
        else:
            result = library.read_page("doc-hostile", 1, source="handwriting", max_chars=budget)
            assert [part.text for part in result.parts] == (["first"[:budget]] if budget else [])
            assert result.truncated is True


@pytest.mark.parametrize("budget, expected", [(None, (3, 0, 9)), (4, (1, 0, 3))])
def test_actual_read_cte_preserves_null_candidates_without_cross_product(
    tmp_path: Path, budget: int | None, expected: tuple[int, int, int]
) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        table = _nullable_content(connection, "textbox")
        connection.executemany(
            f"INSERT INTO {table} VALUES (NULL, 'page-02', ?)",
            [("first",), ("second",), ("third",)],
        )
    statements: list[str] = []
    with Library(sandbox) as library:
        connection = library._session.ensure_fts()
        assert connection is not None
        connection.set_trace_callback(statements.append)
        with pytest.raises(GoodnotesError, match=r"textbox_text_content\.text_id"):
            library.read_page("doc-hostile", 1, source="textbox", max_chars=budget)
        connection.set_trace_callback(None)
        (query,) = [sql for sql in statements if "WITH candidates AS" in sql]
        assert "LEFT JOIN textbox_text_content AS content" in query
        original = query.replace("LEFT JOIN textbox_text_content", "JOIN textbox_text_content")
        rejected = original.replace(
            "content.text_id = candidates.text_id", "content.text_id IS candidates.text_id"
        )
        counts = tuple(
            len(connection.execute(sql).fetchall()) for sql in (query, original, rejected)
        )
        assert counts == expected


@pytest.mark.parametrize("stored_page", [9.75, float("inf")])
@pytest.mark.parametrize("valid_alternative", [False, True])
def test_pdf_identity_mismatch_never_verifies_coerced_page(
    tmp_path: Path, stored_page: float, valid_alternative: bool
) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        connection.execute(
            "UPDATE pdf_text_content SET pdf_page_no = ? WHERE text_id = 101", (stored_page,)
        )
        if valid_alternative:
            connection.execute(
                "INSERT INTO pdf_text_content VALUES "
                "(201, 'independent PDF text', 'attachment-safe', 9, 'doc-hostile')"
            )
    with Library(sandbox) as library:
        coverage = library.search_coverage(source="pdf", notebook="doc-hostile")
        details = coverage["sources"]["pdf"]
        assert details["pages_with_stored_nonempty_text"] == 1 + valid_alternative
        assert details["mismatched_direct_pointers"] == 1
        assert details["unverifiable_pages"] == 0
        assert any("direct pointers did not match" in warning for warning in coverage["warnings"])
        assert library.search("direct PDF phrase", source="pdf", notebook="doc-hostile") == []
        alternative = library.search("independent PDF text", source="pdf", notebook="doc-hostile")
        assert [(hit.page, hit.text) for hit in alternative] == (
            [(1, "independent PDF text")] if valid_alternative else []
        )
        if valid_alternative:
            read = library.read_page("doc-hostile", 1, source="pdf")
            assert [part.text for part in read.parts] == ["independent PDF text"]


@pytest.mark.parametrize(
    "stored_page, projection_page, expected",
    [
        (9, 9, True),
        (9.0, 9, False),
        (9.75, 9, False),
        (float("inf"), 9, False),
        (float("nan"), 9, False),
        ("9", 9, False),
        (b"9", 9, False),
        (None, 9, False),
        (True, 1, False),
        (0, 0, False),
        (-1, -1, False),
    ],
)
def test_stored_pdf_page_requires_exact_positive_integer(
    stored_page: Any, projection_page: int, expected: bool
) -> None:
    from goodnotes_cli.reader.validation import matches_pdf_identity

    row = {"document_id": "doc", "attachment_id": "attachment", "pdf_page_no": stored_page}
    assert (
        matches_pdf_identity(row, PageRef("doc", "page", "attachment", projection_page)) is expected
    )
    assert (
        matches_pdf_identity(row, PageRef("other", "page", "attachment", projection_page)) is False
    )
    assert matches_pdf_identity(row, PageRef("doc", "page", "other", projection_page)) is False
