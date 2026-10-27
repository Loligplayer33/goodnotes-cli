"""Generated SQLite fixtures for safe library navigation and text reads."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace

import pymupdf
import pytest

from goodnotes_cli.errors import GoodnotesError, TextIndexUnavailableError
from goodnotes_cli.library import Library
from goodnotes_cli.models import PageRef
from goodnotes_cli.reader import attachments as attachments_module
from goodnotes_cli.reader import pdf as pdf_module
from goodnotes_cli.reader import session as session_module
from goodnotes_cli.reader.index import SchemaCache
from goodnotes_cli.reader.text import _PDFPageBindings
from goodnotes_cli.safety import Sandbox
from tests.support.library import (
    ROOT_ID,
    _add_repeated_fallback_pages,
    _create_snapshot,
    _write_pdf,
)


def _make_library(
    tmp_path: Path, *, with_fts: bool = True, with_scan_page: bool = False
) -> Library:
    return Library(
        _create_snapshot(tmp_path / "snapshot", with_fts=with_fts, with_scan_page=with_scan_page)
    )


def test_root_listing_paths_order_and_deleted_rows_are_explicit(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    with library:
        root_items = library.list_items("/")
        assert [(item.name, item.kind) for item in root_items] == [
            ("Shared A", "folder"),
            ("Shared B", "folder"),
            ("Root note", "notebook"),
        ]
        assert root_items[0].path == "Shared A"
        assert library.list_items("Shared A", kind="notebook")[0].path == "Shared A/same"
        assert library.resolve("Shared A/Nested/quote ' ; DROP TABLE documents;--").id == (
            "doc-hostile"
        )
        assert library.resolve("doc-root").path == "Root note"
        with pytest.raises(GoodnotesError, match="No active"):
            library.resolve("doc-deleted")
        assert "Deleted notebook" not in [item.name for item in library.tree()]
        assert library.tree(max_depth=1) == root_items
        stats = library.stats()
        assert stats["deleted_link_rows"] == 1
        assert stats["links_to_deleted_items"] == 2


def test_basename_ambiguity_and_orphaned_items_are_not_guessed(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    with library:
        with pytest.raises(GoodnotesError, match="ambiguous"):
            library.resolve("same", kind="notebook")
        assert library.resolve("Shared B/same").id == "doc-b"
        with pytest.raises(GoodnotesError, match="no resolvable path"):
            library.resolve("doc-orphan")
        stats = library.stats()
        assert stats["orphaned_items"] == 1
        assert any("no resolvable root path" in warning for warning in stats["warnings"])


def test_full_path_collisions_and_kind_mismatches_are_reported(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE folders SET name = 'Shared A' WHERE id = 'folder-b'")
    library = Library(sandbox)
    with library:
        with pytest.raises(GoodnotesError, match="ambiguous"):
            library.resolve("Shared A/same", kind="notebook")
        with pytest.raises(GoodnotesError, match="different kind"):
            library.resolve("doc-a", kind="folder")


def test_missing_notes_items_keeps_annotation_count_unknown(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("DROP TABLE notes_items")

    with Library(sandbox) as library:
        pages = library.pages("doc-hostile")
        assert all(page.annotation_count is None for page in pages)


def test_required_catalog_names_and_numbers_reject_malformed_values(tmp_path: Path) -> None:
    bad_name = _create_snapshot(tmp_path / "bad-name")
    with sqlite3.connect(bad_name.projection_path) as db:
        db.execute(
            "UPDATE documents SET name = ? WHERE id = ?", (sqlite3.Binary(b"bad"), "doc-root")
        )
    with pytest.raises(GoodnotesError, match=r"documents\.name.*non-empty text"):
        Library(bad_name).__enter__()

    bad_number = _create_snapshot(tmp_path / "bad-number")
    with sqlite3.connect(bad_number.projection_path) as db:
        db.execute("UPDATE folders SET updated_at = ? WHERE id = ?", (float("inf"), "folder-a"))
    with pytest.raises(GoodnotesError, match=r"folders\.updated_at.*finite number"):
        Library(bad_number).__enter__()


def test_malformed_optional_template_geometry_is_reported_as_unknown(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "UPDATE templates SET size_width = ?, size_height = ? WHERE id = ?",
            ("not-a-number", float("inf"), "template-02"),
        )
        db.execute("UPDATE templates SET page_number = ? WHERE id = ?", (1.5, "template-02"))

    with Library(sandbox) as library:
        page = library.pages("doc-hostile")[0]
        assert page.width is None
        assert page.height is None
        assert page.pdf_page is None


def test_pages_are_one_based_ordered_and_text_candidates_keep_provenance(
    tmp_path: Path,
) -> None:
    library = _make_library(tmp_path)
    with library:
        pages = library.pages("doc-hostile")
        assert [(page.id, page.number) for page in pages] == [
            ("page-02", 1),
            ("page-10", 2),
            ("page-30", 3),
        ]
        assert pages[0].pdf_page == 9
        assert pages[0].annotation_count == 2
        assert pages[0].attachment_path == str(library.sandbox.attachments_dir / "safe.pdf")
        assert pages[1].pdf_page == 12
        assert pages[1].attachment_path is None
        assert pages[2].attachment_path == str(library.sandbox.attachments_dir / "fallback.pdf")

        page_text = library.read_page("doc-hostile", 1)
        assert page_text.page == 1
        assert [part.source for part in page_text.parts] == [
            "handwriting",
            "handwriting",
            "textbox",
            "pdf",
        ]
        assert page_text.parts[0].text == "Straße | Strasse"
        assert page_text.parts[0].engine == "myscript"
        assert page_text.parts[0].priority == "primary"
        assert page_text.parts[-1].text == "direct PDF phrase"
        assert any("machine-recognized" in warning for warning in page_text.warnings)
        text_only = library.read_page("doc-hostile", 1, source="text")
        assert [part.source for part in text_only.parts] == ["textbox", "pdf"]
        assert text_only.parts[-1].text == "direct PDF phrase"
        assert not any("machine-recognized" in warning for warning in text_only.warnings)
        assert library.read_page("doc-hostile", 2).parts[-1].text == "mapped PDF phrase"
        fallback = library.read_page("doc-hostile", 3)
        assert len(fallback.parts) == 1
        assert fallback.parts[0].source == "pdf"
        assert fallback.parts[0].text == "Copied attachment fallback text"
        assert any("confined copied attachment" in warning for warning in fallback.warnings)
        with pytest.raises(GoodnotesError, match="one-based"):
            library.read_page("doc-hostile", 0)


def test_page_hydration_queries_stay_bounded_for_500_pages_and_20_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "large")
    _add_repeated_fallback_pages(sandbox, 497)
    resolved_candidates: list[Path] = []
    actual_resolve = attachments_module.resolve_confined_file
    repeated_reference = (
        sandbox.attachments_dir / "original" / "goodnotes" / "attachments" / "fallback.pdf"
    )
    fallback_file = sandbox.attachments_dir / "fallback.pdf"

    def record_resolve(root: str | Path, candidate: str | Path) -> Path | None:
        resolved_candidates.append(Path(candidate))
        return actual_resolve(root, candidate)

    monkeypatch.setattr(attachments_module, "resolve_confined_file", record_resolve)

    with Library(sandbox) as library:
        assert library._session.projection is not None
        statements: list[str] = []
        library._session.projection.set_trace_callback(statements.append)
        pages = library.pages("doc-hostile")
        assert len(pages) == 500
        assert resolved_candidates.count(repeated_reference) == 1
        assert pages[0].annotation_count == 2
        assert pages[1].annotation_count == 0
        assert pages[2].annotation_count == 0
        assert all(page.annotation_count == 2 for page in pages[3:])
        for page in range(1, 21):
            library.read_page("doc-hostile", page, source="handwriting", max_chars=64)

        hydration_queries = [
            statement
            for statement in statements
            if any(
                marker in statement.casefold()
                for marker in (
                    "from pages",
                    "from templates",
                    "from attachments",
                    "from notes_items",
                )
            )
        ]
        assert len(hydration_queries) <= 6
        assert sum("from pages" in statement.casefold() for statement in hydration_queries) == 1
        assert sum("from templates" in statement.casefold() for statement in hydration_queries) <= 2
        assert (
            sum("from attachments" in statement.casefold() for statement in hydration_queries) == 1
        )
        assert (
            sum("from notes_items" in statement.casefold() for statement in hydration_queries) == 1
        )

        before_reads = resolved_candidates.count(fallback_file)
        library.read_page("doc-hostile", 3, source="pdf")
        assert resolved_candidates.count(fallback_file) == before_reads + 2
        library.read_page("doc-hostile", 3, source="pdf")
        assert resolved_candidates.count(fallback_file) == before_reads + 4


def test_metadata_navigation_does_not_open_fts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    opened: list[Path] = []
    actual_open_database = session_module.open_database

    def record_open(path: Path) -> AbstractContextManager[sqlite3.Connection]:
        opened.append(Path(path))
        return actual_open_database(path)

    def fail_pdf_import() -> None:
        raise AssertionError("metadata navigation should not import PyMuPDF")

    monkeypatch.setattr(session_module, "open_database", record_open)
    monkeypatch.setattr(session_module, "_pymupdf", fail_pdf_import)
    with Library(sandbox) as library:
        library.list_items("/")
        library.resolve("doc-hostile")
        library.pages("doc-hostile")
        assert library._session.fts is None
    assert opened == [Path(sandbox.projection_path)]


def test_search_and_coverage_share_pdf_bindings_and_page_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _make_library(tmp_path)
    binding_calls = 0
    original_bind = pdf_module.text_index.pdf_page_bindings

    def count_bindings(
        fts: sqlite3.Connection | None,
        page_refs: list[PageRef],
        *,
        schema: SchemaCache | None = None,
    ) -> _PDFPageBindings:
        nonlocal binding_calls
        binding_calls += 1
        return original_bind(fts, page_refs, schema=schema)

    monkeypatch.setattr(pdf_module.text_index, "pdf_page_bindings", count_bindings)
    with library:
        assert library._session.projection is not None
        statements: list[str] = []
        library._session.projection.set_trace_callback(statements.append)
        library.search("direct", source="pdf", notebook="doc-hostile")
        library.search_coverage(source="pdf", notebook="doc-hostile")

    assert binding_calls == 1
    page_ref_queries = [
        statement for statement in statements if "select p.id as page_id" in statement.casefold()
    ]
    assert len(page_ref_queries) == 1


def test_schema_metadata_is_cached_for_one_library_session(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    with library:
        library._session.ensure_fts()
        assert library._session.fts is not None
        statements: list[str] = []
        library._session.fts.set_trace_callback(statements.append)
        library.search("STRASSE", source="handwriting", notebook="doc-hostile")
        library.search_coverage(source="handwriting", notebook="doc-hostile")
        schema_reads = [
            statement
            for statement in statements
            if "sqlite_master" in statement.casefold()
            or "pragma table_info" in statement.casefold()
        ]
        assert sum("sqlite_master" in statement.casefold() for statement in schema_reads) == 1
        assert sum("pragma table_info" in statement.casefold() for statement in schema_reads) <= 3


def test_scan_reuses_pdf_documents_until_library_session_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "scan", with_fts=False)
    _add_repeated_fallback_pages(sandbox, 20)
    actual_open = pymupdf.open
    opened: list[pymupdf.Document] = []

    def record_pdf_open(path: Path) -> pymupdf.Document:
        document = actual_open(path)
        opened.append(document)
        return document

    monkeypatch.setattr(session_module, "_pymupdf", lambda: SimpleNamespace(open=record_pdf_open))
    with Library(sandbox) as library:
        result = library.scan_pdfs("not present", notebook="doc-hostile", page_limit=23)
        assert result["pages_scanned"] == 23
        assert len(opened) == 2
        assert not any(document.is_closed for document in opened)
    assert all(document.is_closed for document in opened)


def test_pdf_document_lru_closes_evictions_and_all_documents_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    paths = [sandbox.attachments_dir / "safe.pdf", sandbox.attachments_dir / "fallback.pdf"]
    for index in range(session_module._MAX_OPEN_PDFS - len(paths) + 1):
        path = sandbox.attachments_dir / f"cached-{index}.pdf"
        _write_pdf(path, f"Cached document {index}")
        paths.append(path)

    actual_open = pymupdf.open
    opened: list[pymupdf.Document] = []

    def record_pdf_open(path: str | Path) -> pymupdf.Document:
        document = actual_open(path)
        opened.append(document)
        return document

    monkeypatch.setattr(session_module, "_pymupdf", lambda: SimpleNamespace(open=record_pdf_open))
    with pytest.raises(RuntimeError, match="forced library error"):
        with Library(sandbox) as library:
            with library._pdf.pdf_session():
                for path in paths:
                    with library._session.pdf_document(path):
                        pass
                assert len(opened) == session_module._MAX_OPEN_PDFS + 1
                assert library._session.pdf_documents is not None
                assert (
                    len(library._session.pdf_documents.documents) == session_module._MAX_OPEN_PDFS
                )
                assert opened[0].is_closed
            raise RuntimeError("forced library error")

    assert all(document.is_closed for document in opened)


def test_missing_attachment_reference_survives_hydration_and_reports_reason(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "UPDATE attachments SET path = 'missing-background.pdf' WHERE id = ?",
            ("attachment-fallback",),
        )

    with Library(sandbox) as library:
        page = library.pages("doc-hostile")[2]
        assert page.attachment_path == "missing-background.pdf"
        result = library.read_page("doc-hostile", 3, source="pdf")
        assert result.parts == []
        assert any("mapped copied attachment is missing" in warning for warning in result.warnings)
        scan = library.scan_pdfs("anything", notebook="doc-hostile", page_offset=2, page_limit=1)
        assert scan["extraction_issues"][0]["issue"] == "missing-attachment"


def test_read_page_can_limit_sources_without_loading_unselected_sources(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "INSERT INTO textbox_text_content VALUES (?, ?, ?)",
            (12, "page-02", "unselected " * 100_000),
        )

    with Library(sandbox) as library:
        sql: list[str] = []
        library._session.ensure_fts()
        assert library._session.fts is not None
        library._session.fts.set_trace_callback(sql.append)
        page_text = library.read_page("doc-hostile", 1, source="handwriting")
        assert [part.source for part in page_text.parts] == ["handwriting", "handwriting"]
        assert any("machine-recognized" in warning for warning in page_text.warnings)
        assert not any("FROM textbox_text_content" in statement for statement in sql)
        assert not any("FROM pdf_text_content" in statement for statement in sql)
        handwriting_reads = [
            statement
            for statement in sql
            if "with candidates as" in statement.casefold()
            and "from annotation_text_content" in statement.casefold()
        ]
        assert len(handwriting_reads) == 1

    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE textbox_text_content")
        db.execute("DROP TABLE pdf_text_content")
    with Library(sandbox) as library:
        page_text = library.read_page("doc-hostile", 1, source="handwriting")
        assert not any("Textbox text is unavailable" in warning for warning in page_text.warnings)
        assert not any("PDF text is unavailable" in warning for warning in page_text.warnings)


@pytest.mark.parametrize(
    "malformation",
    ["handwriting-blob", "handwriting-text-id", "pdf-blob", "pdf-text-id", "pdf-pointer"],
)
def test_read_page_rejects_malformed_index_text_rows(tmp_path: Path, malformation: str) -> None:
    sandbox = _create_snapshot(tmp_path / malformation)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        if malformation == "handwriting-blob":
            db.execute(
                "UPDATE annotation_text_content SET text = ? WHERE text_id = ?",
                (sqlite3.Binary(b"\xffinvalid UTF-8"), 1),
            )
        elif malformation == "handwriting-text-id":
            db.execute("DROP TABLE annotation_text_content")
            db.execute(
                "CREATE TABLE annotation_text_content "
                "(text_id TEXT, page_id TEXT, text TEXT, engine_type TEXT, priority TEXT)"
            )
            db.execute(
                "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
                ("invalid-id", "page-02", "stored text", "myscript", "primary"),
            )
        elif malformation == "pdf-blob":
            db.execute(
                "UPDATE pdf_text_content SET text = ? WHERE text_id = ?",
                (sqlite3.Binary(b"\xffinvalid UTF-8"), 101),
            )
        elif malformation == "pdf-text-id":
            db.execute("DROP TABLE pdf_text_content")
            db.execute(
                "CREATE TABLE pdf_text_content "
                "(text_id TEXT, text TEXT, attachment_id TEXT, pdf_page_no INTEGER, "
                "document_id TEXT)"
            )
            db.execute(
                "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
                ("invalid-id", "stored PDF text", "attachment-safe", 9, "doc-hostile"),
            )
        else:
            db.execute(
                "UPDATE page_meta SET pdf_text_id = ? WHERE page_id = ?",
                ("invalid-id", "page-02"),
            )

    with Library(sandbox) as library:
        expected = {
            "handwriting-blob": "annotation_text_content.text",
            "handwriting-text-id": "annotation_text_content.text_id",
            "pdf-blob": "pdf_text_content.text",
            "pdf-text-id": "pdf_text_content.text_id",
            "pdf-pointer": "page_meta.pdf_text_id",
        }[malformation]
        with pytest.raises(GoodnotesError, match=expected.replace(".", r"\.")):
            source = "pdf" if malformation.startswith("pdf-") else "handwriting"
            library.read_page("doc-hostile", 1, source=source)


@pytest.mark.parametrize(
    ("source", "table", "text_id"),
    [
        ("handwriting", "annotation_text_content", 1),
        ("textbox", "textbox_text_content", 10),
        ("pdf", "pdf_text_content", 101),
    ],
)
@pytest.mark.parametrize("encoding", ["UTF-8", "UTF-16le"])
def test_embedded_nul_text_is_preserved_unbounded_and_rejected_when_bounded(
    tmp_path: Path, source: str, table: str, text_id: int, encoding: str
) -> None:
    sandbox = _create_snapshot(tmp_path / f"{source}-{encoding}", fts_encoding=encoding)
    assert sandbox.fts_path is not None
    with Library(sandbox) as library:
        normal_text = library.read_page("doc-hostile", 1, source=source, max_chars=4)
        assert sum(len(part.text) for part in normal_text.parts) <= 4
        assert normal_text.truncated is True

    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            f"UPDATE {table} SET text = ? WHERE text_id = ?",
            ("before\x00after", text_id),
        )

    with Library(sandbox) as library:
        unbounded = library.read_page("doc-hostile", 1, source=source, max_chars=None)
        assert unbounded.parts[0].text == "before\x00after"
        assert unbounded.parts[0].truncated is False
        assert unbounded.truncated is False

        with pytest.raises(GoodnotesError, match="embedded NUL"):
            library.read_page("doc-hostile", 1, source=source, max_chars=4)

        zero_budget = library.read_page("doc-hostile", 1, source=source, max_chars=0)
        assert zero_budget.parts == []
        assert zero_budget.truncated is True


def test_read_page_character_budget_marks_clipped_and_omitted_text(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("UPDATE textbox_text_content SET text = '12345' WHERE text_id = 10")
        db.execute(
            "INSERT INTO textbox_text_content VALUES (?, ?, ?)",
            (12, "page-02", "later text"),
        )

    with Library(sandbox) as library:
        clipped = library.read_page("doc-hostile", 1, source="handwriting", max_chars=8)
        assert clipped.truncated is True
        assert len(clipped.parts) == 1
        assert clipped.parts[0].text == "Straße |"
        assert clipped.parts[0].truncated is True

        exact = library.read_page("doc-hostile", 1, source="textbox", max_chars=5)
        assert exact.truncated is True
        assert exact.parts[0].text == "12345"
        assert exact.parts[0].truncated is False
        assert len(exact.parts) == 1

        zero = library.read_page("doc-hostile", 1, source="handwriting", max_chars=0)
        assert zero.parts == []
        assert zero.truncated is True


def test_zero_budget_does_not_materialize_content_text(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    with library:
        sql: list[str] = []
        library._session.ensure_fts()
        assert library._session.fts is not None
        library._session.fts.set_trace_callback(sql.append)
        page_text = library.read_page("doc-hostile", 1, source="handwriting", max_chars=0)
        assert page_text.truncated is True
        assert page_text.parts == []
        pdf_page_text = library.read_page("doc-hostile", 1, source="pdf", max_chars=0)
        assert pdf_page_text.truncated is True
        assert pdf_page_text.parts == []
        assert not any("substr(" in statement.casefold() for statement in sql)


def test_pdf_text_reads_use_one_bounded_query_and_preserve_row_order(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("UPDATE page_meta SET pdf_text_id = NULL WHERE page_id = 'page-02'")
        db.execute("UPDATE pdf_text_content SET text = 'abcd' WHERE text_id = 101")
        db.executemany(
            "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (201, "efgh", "attachment-safe", 9, "doc-hostile"),
                (202, "tail", "attachment-safe", 9, "doc-hostile"),
            ],
        )

    with Library(sandbox) as library:
        library._session.ensure_fts()
        assert library._session.fts is not None
        statements: list[str] = []
        library._session.fts.set_trace_callback(statements.append)
        page_text = library.read_page("doc-hostile", 1, source="pdf", max_chars=5)

    pdf_reads = [
        statement
        for statement in statements
        if "with candidates as" in statement.casefold()
        and "from pdf_text_content" in statement.casefold()
    ]
    assert len(pdf_reads) == 1
    assert "substr(content.text" in pdf_reads[0].casefold()
    assert [part.text for part in page_text.parts] == ["abcd", "e"]
    assert [part.truncated for part in page_text.parts] == [False, True]
    assert page_text.truncated is True


def test_pdf_read_preserves_parts_when_budget_expires_at_id_batch_boundary(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("UPDATE page_meta SET pdf_text_id = NULL WHERE page_id = 'page-02'")
        db.execute("UPDATE pdf_text_content SET text = 'a' WHERE text_id = 101")
        db.executemany(
            "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
            [(text_id, "a", "attachment-safe", 9, "doc-hostile") for text_id in range(201, 601)],
        )

    with Library(sandbox) as library:
        page_text = library.read_page("doc-hostile", 1, source="pdf", max_chars=400)

    assert len(page_text.parts) == 400
    assert all(part.text == "a" for part in page_text.parts)
    assert all(part.truncated is False for part in page_text.parts)
    assert page_text.truncated is True


def test_pdf_fallback_respects_read_budget_and_reports_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _make_library(tmp_path, with_fts=False)
    with library:
        page_text = library.read_page("doc-hostile", 3, source="pdf", max_chars=5)
        assert page_text.truncated is True
        assert page_text.parts[0].text == "Copie"
        assert page_text.parts[0].truncated is True
        assert any(
            "truncated to the requested character budget" in warning
            for warning in page_text.warnings
        )
        assert len(page_text.parts[0].text) <= 5

        def fail_if_opened(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("zero-budget PDF read should not extract page text")

        monkeypatch.setattr("goodnotes_cli.reader.pdf._pymupdf", fail_if_opened)
        zero = library.read_page("doc-hostile", 3, source="pdf", max_chars=0)
        assert zero.parts == []
        assert zero.truncated is True


def test_search_uses_literal_casefolded_terms_scope_and_source_labels(tmp_path: Path) -> None:
    library = _make_library(tmp_path)
    with library:
        handwriting = library.search("STRASSE", source="handwriting")
        assert len(handwriting) == 1
        assert handwriting[0].text == "Straße | Strasse"
        assert handwriting[0].page == 1
        assert handwriting[0].source == "handwriting"
        assert handwriting[0].path == "Shared A/Nested/quote ' ; DROP TABLE documents;--"

        literal_quotes = library.search("' ; DROP TABLE documents;--", source="handwriting")
        assert literal_quotes == []
        typed = library.search("textbox phrase", source="textbox")
        assert [hit.source for hit in typed] == ["textbox"]
        direct_pdf = library.search("direct PDF phrase", source="pdf")
        mapped_pdf = library.search("mapped PDF phrase", source="pdf")
        text_hits = library.search("typed textbox phrase", source="text")
        text_pdf_hits = library.search("direct PDF phrase", source="text")
        assert direct_pdf[0].page == 1
        assert mapped_pdf[0].page == 2
        assert [hit.source for hit in text_hits] == ["textbox"]
        assert [hit.source for hit in text_pdf_hits] == ["pdf"]
        assert library.search("recognized", folder="Shared B") == []


def test_search_streams_rows_deduplicates_and_preserves_sort_order(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.executemany(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (5, "page-02", "Straße | Strasse", "myscript", "primary"),
                (6, "page-02", "handwriting phrase", "myscript", "primary"),
            ],
        )

    with Library(sandbox) as library:
        rows = library._search._search_rows("handwriting", "phrase")
        assert iter(rows) is rows
        ordered = library.search("phrase", limit=10)
        assert [(hit.page, hit.source, hit.text) for hit in ordered] == [
            (1, "handwriting", "handwriting phrase"),
            (1, "pdf", "direct PDF phrase"),
            (1, "textbox", "typed textbox phrase"),
            (2, "pdf", "mapped PDF phrase"),
        ]
        assert len(library.search("STRASSE", source="handwriting")) == 1


def test_search_streaming_handles_many_rows_with_bounded_result_count(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.executemany(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (100 + index, "page-02", f"bulk result {index:04}", "myscript", "primary")
                for index in range(1000)
            ],
        )

    with Library(sandbox) as library:
        hits = library.search("bulk result", source="handwriting", limit=2)
        assert [hit.text for hit in hits] == ["bulk result 0000", "bulk result 0001"]
        assert len(library.search("bulk result", source="handwriting", limit=1200)) == 1000


def test_search_top_k_keeps_global_path_order_when_rows_arrive_in_reverse_order(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            ("page-root", "doc-root", None, "01", "group-root", 0),
        )
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("page-root", "doc-root", "01", None, None, 0, 0),
        )
        db.executemany(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (5, "page-02", "pathorder nested", "myscript", "primary"),
                (6, "page-root", "pathorder root", "myscript", "primary"),
                (7, "page-02", "pathorder nested", "myscript", "primary"),
            ],
        )

    with Library(sandbox) as library:
        assert library.search("pathorder", source="handwriting", limit=1)[0].path == "Root note"
        assert [hit.path for hit in library.search("pathorder", source="handwriting")] == [
            "Root note",
            "Shared A/Nested/quote ' ; DROP TABLE documents;--",
        ]


def test_duplicate_parent_and_cycle_errors_are_reported(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("second-parent", 1, "doc-a", "folder-b", ROOT_ID, "z", 0),
        )
    library = Library(sandbox)
    with library:
        assert library.stats()["duplicate_parent_items"] == 1
        with pytest.raises(GoodnotesError, match="multiple parent links"):
            library.list_items()

    cycle_library = Library(_create_snapshot(tmp_path / "cycle-snapshot", with_cycle=True))
    with cycle_library:
        assert cycle_library.stats()["cycle_folders"] == 2
        with pytest.raises(GoodnotesError, match="folder cycle"):
            cycle_library.resolve("doc-a")


def test_missing_search_index_does_not_block_navigation(tmp_path: Path) -> None:
    library = _make_library(tmp_path, with_fts=False)
    with library:
        assert library.resolve("doc-root").name == "Root note"
        page_text = library.read_page("doc-hostile", 3)
        assert page_text.parts[0].source == "pdf"
        assert page_text.parts[0].text == "Copied attachment fallback text"
        assert any("no search index" in warning for warning in page_text.warnings)
        assert library.text_warnings() == [
            "Library-wide: the sandbox has no search index; indexed handwriting, textbox, and "
            "PDF text sources are unavailable."
        ]
        assert library.stats()["search_index_available"] is False
        with pytest.raises(GoodnotesError, match="no search index"):
            library.search("anything")


def test_missing_text_sources_are_reported_and_explicit_search_fails(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE textbox_text_content")
        db.execute("DROP TABLE pdf_text_content")

    with Library(sandbox) as library:
        warnings = library.text_warnings()
        assert len(warnings) == 2
        assert any("Textbox text is unavailable" in warning for warning in warnings)
        assert any("PDF text is unavailable" in warning for warning in warnings)
        assert [hit.source for hit in library.search("STRASSE")] == ["handwriting"]
        with pytest.raises(GoodnotesError, match="Textbox text is unavailable"):
            library.search("typed", source="textbox")
        page_text = library.read_page("doc-hostile", 1)
        assert any("Textbox text is unavailable" in warning for warning in page_text.warnings)
        assert any("PDF text is unavailable" in warning for warning in page_text.warnings)


def test_missing_page_metadata_warns_about_stored_index_coverage(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE page_meta")

    with Library(sandbox) as library:
        warnings = library.text_warnings()
        assert len(warnings) == 1
        assert "Stored-index search cannot map" in warnings[0]
        with pytest.raises(GoodnotesError, match="page metadata"):
            library.search("STRASSE")
        with pytest.raises(GoodnotesError, match="page metadata is missing"):
            library.search("STRASSE", source="handwriting")


def test_stale_attachment_prefix_is_not_remapped_when_basename_is_ambiguous(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    duplicate_dir = sandbox.attachments_dir / "another-copy"
    duplicate_dir.mkdir()
    duplicate_path = duplicate_dir / "fallback.pdf"
    duplicate_path.write_bytes((sandbox.attachments_dir / "fallback.pdf").read_bytes())

    with Library(sandbox) as library:
        assert library.pages("doc-hostile")[2].attachment_path is None
        page_text = library.read_page("doc-hostile", 3)
        assert page_text.parts == []
        assert any("basename is ambiguous" in warning for warning in page_text.warnings)


def test_pdf_scan_is_bounded_paginated_and_independent_of_the_search_index(
    tmp_path: Path,
) -> None:
    library = _make_library(tmp_path, with_fts=False, with_scan_page=True)
    with library:
        first = library.scan_pdfs("COPIED ATTACHMENT FALLBACK TEXT", limit=1, page_limit=4)
        assert first["pages_scanned"] == 3
        assert first["next_page_offset"] == 3
        assert first["total_pages"] == 4
        assert first["scan_complete"] is False
        assert first["coverage_complete"] is False
        assert first["stop_reason"] == "hit-limit"
        assert first["scope"]["type"] == "folder"
        assert any(
            issue["issue"] == "unsafe-attachment-path" for issue in first["extraction_issues"]
        )
        assert len(first["hits"]) == 1
        assert first["hits"][0].source == "pdf"
        assert first["hits"][0].page == 3
        assert first["hits"][0].path == "Shared A/Nested/quote ' ; DROP TABLE documents;--"
        assert any(
            "1 scanned pages have no confined copied background" in warning
            for warning in first["warnings"]
        )
        assert any("stored search index" in x for x in first["warnings"])

        second = library.scan_pdfs(
            "Copied attachment fallback", limit=1, page_limit=1, page_offset=3
        )
        assert second["pages_scanned"] == 1
        assert second["next_page_offset"] is None
        assert second["scan_complete"] is True
        assert second["coverage_complete"] is False
        assert second["stop_reason"] == "complete"
        assert second["hits"][0].page == 4

        with pytest.raises(GoodnotesError, match="between 1 and 250"):
            library.scan_pdfs("x", page_limit=251)
        with pytest.raises(GoodnotesError, match="must not be empty"):
            library.scan_pdfs("")
        with pytest.raises(GoodnotesError, match="page_offset"):
            library.scan_pdfs("x", page_offset=-1)
        no_hits = library.scan_pdfs("no match", page_limit=1)
        assert no_hits["hits"] == []
        assert no_hits["pages_scanned"] == 1
        assert no_hits["next_page_offset"] == 1
        assert no_hits["stop_reason"] == "page-limit"
        assert no_hits["scan_complete"] is False
        assert library.scan_pdfs("' OR 1=1 --", page_limit=4)["hits"] == []


def _add_root_text_page(sandbox: Sandbox, text: str = "scope-only phrase") -> None:
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            ("page-root", "doc-root", None, "01", "group-root", 0),
        )
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("page-root", "doc-root", "01", None, None, 0, 0),
        )
        db.execute(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            (20, "page-root", text, "myscript", "primary"),
        )


def test_search_and_coverage_respect_notebook_and_folder_scopes(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    _add_root_text_page(sandbox)

    with Library(sandbox) as library:
        root_hits = library.search("scope-only", notebook="doc-root")
        assert [(hit.document_id, hit.page) for hit in root_hits] == [("doc-root", 1)]
        assert library.search("scope-only", folder="Shared A") == []
        assert [(hit.document_id, hit.page) for hit in library.search("scope-only")] == [
            ("doc-root", 1)
        ]

        root_coverage = library.search_coverage(source="handwriting", notebook="doc-root")
        assert root_coverage["scope"] == {
            "type": "notebook",
            "notebook_id": "doc-root",
            "path": "Root note",
        }
        assert root_coverage["active_pages"] == 1
        assert root_coverage["page_meta"]["mapped_active_pages"] == 1
        assert root_coverage["sources"]["handwriting"]["pages_with_stored_nonempty_text"] == 1
        assert root_coverage["completeness"] == "unknown"
        assert isinstance(root_coverage["warnings"], list)
        assert 0 < len(root_coverage["next_steps"]) <= 5

        folder_coverage = library.search_coverage("Shared A", source="handwriting")
        assert folder_coverage["scope"]["path"] == "Shared A"
        assert folder_coverage["active_pages"] == 3
        assert folder_coverage["sources"]["handwriting"]["pages_with_stored_nonempty_text"] == 2

        with pytest.raises(GoodnotesError, match="either --notebook or a non-root --folder"):
            library.search("scope-only", folder="Shared A", notebook="doc-root")
        with pytest.raises(GoodnotesError, match="--folder"):
            library.search("scope-only", notebook="Shared A")
        with pytest.raises(GoodnotesError, match="--notebook"):
            library.search("scope-only", folder="doc-root")
        with pytest.raises(GoodnotesError, match="--folder"):
            library.scan_pdfs("scope-only", notebook="Shared A")


def test_search_coverage_does_not_count_pdf_metadata_without_content(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("UPDATE page_meta SET pdf_text_id = 999 WHERE page_id = 'page-02'")
        db.execute("DELETE FROM pdf_text_content WHERE text_id = 101")

    with Library(sandbox) as library:
        text_details = library.search_coverage(source="text", notebook="doc-hostile")
        assert set(text_details["sources"]) == {"pdf", "textbox"}
        assert not any("Handwriting" in warning for warning in text_details["warnings"])
        details = library.search_coverage(source="pdf", notebook="doc-hostile")
        pdf = details["sources"]["pdf"]
        assert pdf["availability"] == "available"
        assert pdf["pages_with_stored_nonempty_text"] == 1
        assert pdf["metadata_without_nonempty_text_pages"] == 1
        assert pdf["mismatched_direct_pointers"] == 1
        assert details["page_meta"]["mapped_active_pages"] == 3
        assert details["completeness"] == "unknown"
        assert any("no non-empty indexed PDF text" in warning for warning in details["warnings"])
        assert library.search("direct PDF phrase", source="pdf", notebook="doc-hostile") == []


def test_pdf_direct_pointer_must_match_projection_identity(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("UPDATE page_meta SET pdf_text_id = 102 WHERE page_id = 'page-02'")

    with Library(sandbox) as library:
        first_page = library.read_page("doc-hostile", 1, source="pdf")
        assert [part.text for part in first_page.parts] == ["direct PDF phrase"]
        assert any(
            "did not match the current page identity" in warning for warning in first_page.warnings
        )
        direct_hits = library.search("direct PDF phrase", source="pdf", notebook="doc-hostile")
        wrong_page_hits = library.search("mapped PDF phrase", source="pdf", notebook="doc-hostile")
        assert [(hit.page, hit.text) for hit in direct_hits] == [(1, "direct PDF phrase")]
        assert [(hit.page, hit.text) for hit in wrong_page_hits] == [(2, "mapped PDF phrase")]
        coverage = library.search_coverage(source="pdf", notebook="doc-hostile")
        assert coverage["sources"]["pdf"]["pages_with_stored_nonempty_text"] == 2
        assert coverage["sources"]["pdf"]["mismatched_direct_pointers"] == 1


def test_direct_only_pdf_metadata_uses_projection_identity(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE page_meta")
        db.execute(
            "CREATE TABLE page_meta (page_id TEXT PRIMARY KEY, document_id TEXT, "
            "pdf_text_id INTEGER)"
        )
        db.executemany(
            "INSERT INTO page_meta VALUES (?, ?, ?)",
            [
                ("page-02", "doc-hostile", 101),
                ("page-10", "doc-hostile", None),
                ("page-30", "doc-hostile", None),
            ],
        )

    with Library(sandbox) as library:
        page_text = library.read_page("doc-hostile", 1, source="pdf")
        assert [part.text for part in page_text.parts] == ["direct PDF phrase"]
        hits = library.search("mapped PDF phrase", source="pdf", notebook="doc-hostile")
        assert [(hit.page, hit.text) for hit in hits] == [(2, "mapped PDF phrase")]
        coverage = library.search_coverage(source="pdf", notebook="doc-hostile")
        assert coverage["sources"]["pdf"]["availability"] == "available"
        assert coverage["sources"]["pdf"]["pages_with_stored_nonempty_text"] == 2


def test_pdf_search_reports_unverifiable_projection_mapping(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE templates SET attachment_id = NULL, page_number = NULL")

    with Library(sandbox) as library:
        with pytest.raises(TextIndexUnavailableError, match="no verifiable identity mapping"):
            library.search("mapped PDF phrase", source="pdf", notebook="doc-hostile")
        with pytest.raises(GoodnotesError, match="Search source"):
            library.search("mapped PDF phrase", source="invalid", notebook="doc-hostile")


def test_pdf_binding_uses_bounded_indexed_identity_joins(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    page_rows = []
    template_rows = []
    meta_rows = []
    pdf_rows = []
    for index in range(120):
        page_id = f"bulk-page-{index:03}"
        template_id = f"bulk-template-{index:03}"
        attachment_id = f"bulk-attachment-{index:03}"
        position = f"bulk-{index:03}"
        page_no = index + 1
        page_rows.append((page_id, "doc-hostile", template_id, position, f"group-{index}", 0))
        template_rows.append((template_id, "doc-hostile", attachment_id, page_no, 600, 800))
        meta_rows.append(
            (
                page_id,
                "doc-hostile",
                position,
                None,
                f"{attachment_id}_{page_no}",
                0,
                0,
            )
        )
        pdf_rows.append(
            (1000 + index, "bulk indexed phrase", attachment_id, page_no, "doc-hostile")
        )
    with sqlite3.connect(sandbox.projection_path) as db:
        db.executemany("INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)", page_rows)
        db.executemany("INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)", template_rows)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "CREATE INDEX pdf_identity_idx "
            "ON pdf_text_content(document_id, attachment_id, pdf_page_no)"
        )
        db.executemany("INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)", meta_rows)
        db.executemany("INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)", pdf_rows)

    with Library(sandbox) as library:
        library._session.ensure_fts()
        assert library._session.fts is not None
        statements: list[str] = []
        library._session.fts.set_trace_callback(statements.append)
        coverage = library.search_coverage(source="pdf", notebook="doc-hostile")
        assert coverage["sources"]["pdf"]["pages_with_stored_nonempty_text"] == 122
        identity_queries = [statement for statement in statements if "WITH expected(" in statement]
        assert len(identity_queries) == 2
        assert all("template_meta" not in statement for statement in identity_queries)
        plan = library._session.fts.execute(f"EXPLAIN QUERY PLAN {identity_queries[0]}").fetchall()
        assert any("pdf_identity_idx" in row[3] for row in plan)


def test_pdf_search_coverage_suggests_scoped_background_scan(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    _add_root_text_page(sandbox)

    with Library(sandbox) as library:
        details = library.search_coverage(source="pdf", notebook="doc-root")
        assert set(details["sources"]) == {"pdf"}
        assert details["scope"]["type"] == "notebook"
        assert details["scope"]["notebook_id"] == "doc-root"
        hint = details["next_steps"][0]
        assert "goodnotes scan-pdfs 'QUERY' --notebook 'Root note' --page-limit 200" in hint
        assert "replace `QUERY`" in hint
        assert "--page-offset" in hint


def test_source_specific_coverage_skips_irrelevant_metadata_queries_and_warnings(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("ALTER TABLE pages ADD COLUMN needs_index INTEGER DEFAULT 0")
        db.execute("UPDATE pages SET needs_index = 1 WHERE id = 'page-30'")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE annotation_text_content")
        db.execute("DROP TABLE textbox_text_content")

    with Library(sandbox) as library:
        assert library._session.projection is not None
        statements: list[str] = []
        library._session.projection.set_trace_callback(statements.append)
        pdf = library.search_coverage(source="pdf", notebook="doc-hostile")
        assert set(pdf["sources"]) == {"pdf"}
        assert not any("needs_index" in statement for statement in statements)
        assert not any("Handwriting indexing is pending" in warning for warning in pdf["warnings"])
        assert not any("Handwriting text is unavailable" in warning for warning in pdf["warnings"])
        assert not any("Textbox text is unavailable" in warning for warning in pdf["warnings"])

        text = library.search_coverage(source="text", notebook="doc-hostile")
        assert set(text["sources"]) == {"pdf", "textbox"}
        assert text["sources"]["textbox"]["availability"] == "unavailable"
        assert not any("Handwriting" in warning for warning in text["warnings"])

        all_sources = library.search_coverage(source="all", notebook="doc-hostile")
        assert set(all_sources["sources"]) == {"handwriting", "textbox", "pdf"}
        assert any(
            "Handwriting indexing is pending" in warning for warning in all_sources["warnings"]
        )


def test_recognition_status_reports_pending_blank_and_optional_schema(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("ALTER TABLE pages ADD COLUMN needs_index INTEGER DEFAULT 0")
        db.execute("UPDATE pages SET needs_index = 1 WHERE id = 'page-30'")

    with Library(sandbox) as library:
        statuses = library.recognition_status("doc-hostile")
        assert set(statuses) == {"page-02", "page-10", "page-30"}
        assert statuses["page-02"].source == "stored-handwriting-recognition"
        assert statuses["page-02"].availability == "stored-text"
        assert statuses["page-02"].text_rows == 2
        assert statuses["page-02"].index_state == "not-pending"
        assert statuses["page-30"].availability == "no-stored-text"
        assert statuses["page-30"].text_rows == 0
        assert statuses["page-30"].text_chars == 0
        assert statuses["page-30"].index_state == "pending"
        blank_read = library.read_page("doc-hostile", 3, source="handwriting")
        assert blank_read.parts == []
        assert any("indexing is pending" in warning for warning in blank_read.warnings)

    old_schema = _make_library(tmp_path / "old-schema")
    with old_schema:
        statuses = old_schema.recognition_status("doc-hostile")
        assert statuses["page-02"].availability == "stored-text"
        assert statuses["page-02"].index_state == "unknown"
        assert statuses["page-30"].availability == "no-stored-text"
        assert statuses["page-30"].index_state == "unknown"


def test_recognition_status_marks_missing_store_or_table_unavailable(tmp_path: Path) -> None:
    no_store = _make_library(tmp_path / "no-store", with_fts=False)
    with no_store:
        status = no_store.recognition_status("doc-hostile")["page-02"]
        assert status.availability == "unavailable"
        assert status.text_rows is None
        assert status.text_chars is None
        assert status.index_state == "unknown"

    no_table_sandbox = _create_snapshot(tmp_path / "no-table")
    assert no_table_sandbox.fts_path is not None
    with sqlite3.connect(no_table_sandbox.fts_path) as db:
        db.execute("DROP TABLE annotation_text_content")
    no_table = Library(no_table_sandbox)
    with no_table:
        status = no_table.recognition_status("doc-hostile")["page-02"]
        assert status.availability == "unavailable"
        assert status.text_rows is None


def test_stats_scope_index_counts_and_label_library_references_as_context(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("ALTER TABLE templates ADD COLUMN template_library_document_id TEXT")
        db.execute(
            "UPDATE templates SET template_library_document_id = ? WHERE id = ?",
            ("doc-hostile", "template-02"),
        )
        db.execute("CREATE TABLE patterns (pattern_library_document_id TEXT)")
        db.executemany(
            "INSERT INTO patterns VALUES (?)",
            [("doc-root",), ("unmatched-reference",)],
        )
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("unmatched-page", "doc-root", "99", None, None, 0, 0),
        )

    with Library(sandbox) as library:
        global_stats = library.stats()
        template_refs = global_stats["catalog_reference_counts"]["template_library_document_id"]
        pattern_refs = global_stats["catalog_reference_counts"]["pattern_library_document_id"]
        assert template_refs["distinct_document_ids"] == 1
        assert template_refs["active_document_matches"] == 1
        assert template_refs["visible_active_document_matches"] == 1
        assert pattern_refs["distinct_document_ids"] == 2
        assert pattern_refs["active_document_matches"] == 1
        assert pattern_refs["not_active_document_references"] == 1
        assert global_stats["index_metadata_without_active_pages"] == 3
        assert "do not establish system ownership" in global_stats["diagnostic_notes"][2]

        scoped = library.stats("doc-hostile")
        assert scoped["scope"] == {
            "type": "notebook",
            "notebook_id": "doc-hostile",
            "path": "Shared A/Nested/quote ' ; DROP TABLE documents;--",
        }
        assert scoped["page_index"]["active_pages"] == 3
        assert scoped["page_index"]["pages_without_index_metadata"] == 0
        assert scoped["page_index"]["index_metadata_without_active_pages"] == 1
        assert scoped["library"]["index_metadata_without_active_pages"] == 3
        assert len(library.list_items("/")) == 3


def test_diagnostics_distinguish_page_rows_from_pages_of_active_notebooks(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with Library(sandbox) as library:
        before = library.stats()
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE documents SET deleted = 1 WHERE id = 'doc-hostile'")
    with Library(sandbox) as library:
        after = library.stats()
    assert before["index_metadata_without_active_pages"] is not None
    assert after["index_metadata_without_active_pages"] is not None
    assert after["active_pages"] == before["active_pages"]
    assert after["pages_outside_active_notebooks"] == before["pages_outside_active_notebooks"] + 3
    assert (
        after["index_metadata_without_active_pages"]
        == before["index_metadata_without_active_pages"] + 3
    )


def test_partial_page_metadata_is_safe_for_pdf_read_and_doctor(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "partial")
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE page_meta")
        db.execute("CREATE TABLE page_meta (page_id TEXT PRIMARY KEY, document_id TEXT)")
        db.executemany(
            "INSERT INTO page_meta VALUES (?, ?)",
            [
                ("page-02", "doc-hostile"),
                ("page-10", "doc-hostile"),
                ("page-30", "doc-hostile"),
            ],
        )

    with Library(sandbox) as library:
        page_text = library.read_page("doc-hostile", 3, source="pdf")
        assert page_text.parts[0].text == "Copied attachment fallback text"
        assert not any("page_meta" in warning for warning in page_text.warnings)
        stats = library.stats()
        assert stats["pages_without_index_metadata"] == 0
        assert stats["search_index_available"] is True

    no_metadata = _create_snapshot(tmp_path / "missing")
    assert no_metadata.fts_path is not None
    with sqlite3.connect(no_metadata.fts_path) as db:
        db.execute("DROP TABLE page_meta")
    with Library(no_metadata) as library:
        stats = library.stats()
        assert stats["pages_without_index_metadata"] is None
        assert stats["index_metadata_without_active_pages"] is None
        assert stats["search_index_available"] is False
        assert any("metadata coverage is unavailable" in warning for warning in stats["warnings"])


def test_search_coverage_is_batched_metadata_only_and_handles_missing_tables(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with Library(sandbox) as library:
        library._session.ensure_fts()
        assert library._session.fts is not None
        statements: list[str] = []
        library._session.fts.set_trace_callback(statements.append)
        coverage = library.search_coverage(source="handwriting", notebook="doc-hostile")
        assert coverage["sources"]["handwriting"]["pages_with_stored_nonempty_text"] == 2
        assert not any(
            "SELECT text FROM annotation_text_content" in statement for statement in statements
        )

    missing_table_sandbox = _create_snapshot(tmp_path / "missing-table")
    assert missing_table_sandbox.fts_path is not None
    with sqlite3.connect(missing_table_sandbox.fts_path) as db:
        db.execute("DROP TABLE textbox_text_content")
    with Library(missing_table_sandbox) as library:
        coverage = library.search_coverage(source="textbox", notebook="doc-hostile")
        assert coverage["sources"]["textbox"]["availability"] == "unavailable"
        assert coverage["sources"]["textbox"]["pages_with_stored_nonempty_text"] is None
        assert any("Textbox text is unavailable" in warning for warning in coverage["warnings"])

    no_store = _make_library(tmp_path / "no-store", with_fts=False)
    with no_store:
        coverage = no_store.search_coverage(notebook="doc-hostile")
        assert coverage["page_meta"]["available"] is False
        assert coverage["sources"]["handwriting"]["availability"] == "unavailable"
        assert coverage["sources"]["handwriting"]["text_rows"] is None
        assert len(coverage["next_steps"]) <= 5


def test_pdf_scan_reports_complete_coverage_only_for_clean_full_scope(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            ("page-root", "doc-root", "template-root", "01", "group-root", 0),
        )
        db.execute(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
            ("template-root", "doc-root", "attachment-fallback", 1, 600, 800),
        )

    with Library(sandbox) as library:
        result = library.scan_pdfs("copied attachment fallback", notebook="doc-root")
        assert result["scope"] == {
            "type": "notebook",
            "notebook_id": "doc-root",
            "path": "Root note",
        }
        assert result["total_pages"] == 1
        assert result["pages_scanned"] == 1
        assert result["scan_complete"] is True
        assert result["coverage_complete"] is True
        assert result["stop_reason"] == "complete"
        assert result["extraction_issues"] == []
        assert len(result["hits"]) == 1


def test_deep_catalog_walk_and_paths_do_not_recurse(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as db:
        for index in range(1200):
            folder_id = f"deep-{index:04}"
            parent = ROOT_ID if index == 0 else f"deep-{index - 1:04}"
            db.execute(
                "INSERT INTO folders VALUES (?, ?, ?, ?, ?)", (folder_id, "f", 0, 0, str(index))
            )
            db.execute(
                "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"link-{folder_id}", 0, folder_id, parent, ROOT_ID, str(index), 0),
            )
    with Library(sandbox) as library:
        assert len([item for item in library.tree() if item.id.startswith("deep-")]) == 1200
        assert len(library.resolve("deep-1199").path.split("/")) == 1200


def test_failed_index_registration_closes_provisional_connection_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path)
    with Library(sandbox) as library:
        original = library._session._register_casefold
        failed_connection = None

        def fail() -> None:
            nonlocal failed_connection
            failed_connection = library._session.fts
            raise RuntimeError("registration failed")

        monkeypatch.setattr(library._session, "_register_casefold", fail)
        with pytest.raises(RuntimeError, match="registration failed"):
            library._session.ensure_fts()
        assert failed_connection is not None
        with pytest.raises(sqlite3.ProgrammingError):
            failed_connection.execute("SELECT 1")
        assert library._session.fts is None
        monkeypatch.setattr(library._session, "_register_casefold", original)
        assert library._session.ensure_fts() is not None


def test_pdf_cache_attempts_every_close() -> None:
    calls: list[str] = []

    class Document:
        def __init__(self, name: str) -> None:
            self.name = name

        def close(self) -> None:
            calls.append(self.name)
            if self.name != "one":
                raise RuntimeError(self.name)

    cache = session_module._PDFDocumentCache()
    cache.documents.update(
        {Path(name): (Document(name), (0, 0, 0, 0, 0, 0)) for name in ("one", "two", "three")}
    )
    with pytest.raises(RuntimeError, match="three"):
        cache.close()
    assert calls == ["three", "two", "one"]
    assert not cache.documents


def test_attachment_prefix_resolves_before_basename_fallback(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path)
    for folder in ("x", "y"):
        directory = sandbox.attachments_dir / folder
        directory.mkdir()
        (directory / "shared.pdf").write_bytes(b"generated")
    with Library(sandbox) as library:
        path, status = library._attachments.resolve("Attachments/x/shared.pdf")
        assert status == "available"
        assert path == sandbox.attachments_dir / "x" / "shared.pdf"
        nested = sandbox.attachments_dir / "Attachments" / "x"
        nested.mkdir(parents=True)
        (nested / "shared.pdf").write_bytes(b"nested")
        path, status = library._attachments._resolve_uncached("Attachments/x/shared.pdf")
        assert status == "available"
        assert path == nested / "shared.pdf"


def test_zero_budget_missing_attachment_ignores_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "UPDATE attachments SET path = 'missing.pdf' WHERE id = ?", ("attachment-fallback",)
        )
    monkeypatch.chdir(tmp_path)
    (tmp_path / "missing.pdf").write_bytes(b"unrelated")
    with Library(sandbox) as library:
        result = library.read_page("doc-hostile", 3, source="pdf", max_chars=0)
        assert not result.truncated
        assert any("mapped copied attachment is missing" in warning for warning in result.warnings)


def test_pdf_replaced_between_public_reads_stays_confined_after_lru_eviction(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    _add_repeated_fallback_pages(sandbox, 4)
    outside = tmp_path / "outside.pdf"
    _write_pdf(outside, "outside generated secret")
    with sqlite3.connect(sandbox.projection_path) as connection:
        for i in range(4):
            attachment = f"generated-attachment-{i}"
            name = f"generated-{i}.pdf"
            _write_pdf(sandbox.attachments_dir / name, f"generated text {i}")
            connection.execute("INSERT INTO attachments VALUES (?, ?)", (attachment, name))
            connection.execute(
                "UPDATE templates SET attachment_id = ? WHERE id = ?",
                (attachment, f"batch-template-{i:04d}"),
            )
    with Library(sandbox) as library:
        first = library.read_page("doc-hostile", 3, source="pdf")
        assert any("fallback text" in part.text for part in first.parts)
        target = sandbox.attachments_dir / "fallback.pdf"
        target.unlink()
        target.symlink_to(outside)
        for page_number in range(4, 8):
            library.read_page("doc-hostile", page_number, source="pdf")
        result = library.read_page("doc-hostile", 3, source="pdf")
        assert not any("outside generated secret" in part.text for part in result.parts)
        assert any("outside the sandbox" in warning for warning in result.warnings)


def test_cached_basename_match_is_revalidated_before_read(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    outside = tmp_path / "outside.pdf"
    _write_pdf(outside, "outside generated content")
    with Library(sandbox) as library:
        library._attachments.begin_read_operation()
        path, state = library._attachments.resolve("old/location/fallback.pdf", for_read=True)
        assert state == "available"
        assert path == sandbox.attachments_dir / "fallback.pdf"
        path.unlink()
        path.symlink_to(outside)
        assert library._attachments.resolve("old/location/fallback.pdf", for_read=True) == (
            None,
            "unsafe",
        )


def test_pdf_path_replaced_during_open_is_not_extracted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    outside = tmp_path / "outside.pdf"
    _write_pdf(outside, "outside generated content")
    target = sandbox.attachments_dir / "fallback.pdf"
    actual_open = pymupdf.open

    def replace_and_open(path: Path) -> pymupdf.Document:
        target.unlink()
        target.symlink_to(outside)
        return actual_open(path)

    monkeypatch.setattr(session_module, "_pymupdf", lambda: SimpleNamespace(open=replace_and_open))
    with Library(sandbox) as library:
        result = library.read_page("doc-hostile", 3, source="pdf")
        assert not any("outside generated content" in part.text for part in result.parts)
        assert any("could not be read" in warning for warning in result.warnings)
