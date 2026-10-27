"""Generated regression cases for bounded PDF reader operations."""

import sqlite3
from types import SimpleNamespace
from typing import cast

import pytest

from goodnotes_cli.models import Item, Page, PageRef
from goodnotes_cli.reader.attachments import AttachmentResolver
from goodnotes_cli.reader.index import SchemaCache
from goodnotes_cli.reader.pages import PageReader
from goodnotes_cli.reader.pdf import PdfTextReader
from goodnotes_cli.reader.session import ReaderSession
from goodnotes_cli.reader.text import _PDFPageBindings


def test_indexed_page_respects_runtime_bind_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE pdf_text_content (text_id INTEGER, document_id TEXT, "
        "attachment_id TEXT, pdf_page_no INTEGER, text TEXT)"
    )
    connection.executemany(
        "INSERT INTO pdf_text_content VALUES (?, 'doc', 'attachment', 1, ?)",
        [(i, str(i)) for i in range(1, 6)],
    )
    session = SimpleNamespace(
        fts=connection, fts_schema=SchemaCache(connection), ensure_fts=lambda: None
    )
    # This SQL-focused test exercises only the session's FTS fields.
    reader = PdfTextReader(
        cast(ReaderSession, session), cast(PageReader, None), cast(AttachmentResolver, None)
    )
    bindings: _PDFPageBindings = {
        "available": True,
        "pages": {
            "page": {
                "document_id": "doc",
                "attachment_id": "attachment",
                "pdf_page_no": 1,
                "state": "stored-text",
                "text_ids": [1, 2, 3, 4, 5],
                "text_rows": 5,
                "text_chars": 5,
                "metadata_has_mapping": True,
                "mismatched_direct_pointer": False,
                "reason": None,
            },
        },
    }
    monkeypatch.setattr(reader, "bindings_for", lambda _refs: bindings)
    ref = PageRef("doc", "page", "attachment", 1)
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 13)
    parts, remaining, truncated, found, warning = reader.read_indexed_page(ref, 5)
    assert [part.text for part in parts] == ["1", "2", "3", "4", "5"]
    assert (remaining, truncated, found, warning) == (0, False, True, None)
    connection.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 4)
    assert reader.read_indexed_page(ref, 0)[2:4] == (True, True)


def test_scan_classifies_truncation_from_result_not_warning_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = Page(id="page", document_id="doc", number=1, position="1", pdf_page=1)
    notebook = Item(id="doc", name="Example", kind="notebook", path="/Example", page_count=1)
    pages = cast(
        PageReader,
        SimpleNamespace(
            attachment_state_by_page={("doc", "page"): ("available", None)},
            iter_page_window=lambda notebooks, offset, count: iter([(notebook, page)]),
        ),
    )
    # This scan test replaces extraction and page iteration; it does not use a session.
    reader = PdfTextReader(cast(ReaderSession, None), pages, cast(AttachmentResolver, None))

    def extract_fallback(
        _page: Page, *, max_chars: int | None = None
    ) -> tuple[str | None, str | None, bool]:
        return ("text", "changed message", True)

    monkeypatch.setattr(reader, "extract_fallback", extract_fallback)
    result = reader.scan_pdfs("absent", {"type": "notebook"}, [notebook])
    assert result["extraction_issues"][0]["issue"] == "truncated-text"
    assert any("truncated" in warning for warning in result["warnings"])
