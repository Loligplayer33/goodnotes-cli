"""Generated SQLite fixtures for safe library navigation and text reads."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pymupdf
import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from goodnotes_cli.safety import Sandbox

ROOT_ID = "virtual-root"


def _create_snapshot(
    root: Path,
    *,
    with_fts: bool = True,
    with_cycle: bool = False,
    with_scan_page: bool = False,
) -> Sandbox:
    database_dir = root / "Library" / "Databases"
    attachments_dir = root / "Library" / "Attachments"
    database_dir.mkdir(parents=True)
    attachments_dir.mkdir()
    _create_projection(
        database_dir / "projection.sqlite",
        with_cycle=with_cycle,
        with_scan_page=with_scan_page,
    )
    if with_fts:
        _create_fts(database_dir / "fts.sqlite")
    _write_pdf(attachments_dir / "safe.pdf", "ignored indexed PDF page")
    _write_pdf(attachments_dir / "fallback.pdf", "Copied attachment fallback text")
    (root / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )
    return Sandbox.load(root)


def _create_projection(
    path: Path, *, with_cycle: bool = False, with_scan_page: bool = False
) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE folders (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL,
                deleted INTEGER DEFAULT 0, position TEXT
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL,
                deleted INTEGER DEFAULT 0, position TEXT
            );
            CREATE TABLE folder_to_folder_items (
                id TEXT PRIMARY KEY, item_type INTEGER, item_id TEXT,
                parent_folder_id TEXT, root_folder_id TEXT, item_position TEXT,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE pages (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, template_id TEXT,
                position TEXT NOT NULL, group_id TEXT, deleted INTEGER DEFAULT 0
            );
            CREATE TABLE templates (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, attachment_id TEXT,
                page_number INTEGER, size_width REAL, size_height REAL
            );
            CREATE TABLE attachments (id TEXT PRIMARY KEY, path TEXT);
            CREATE TABLE notes_items (
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, deleted INTEGER DEFAULT 0
            );
            """
        )
        db.executemany(
            "INSERT INTO folders VALUES (?, ?, ?, ?, ?)",
            [
                ("folder-a", "Shared A", 20, 0, "a"),
                ("folder-b", "Shared B", 10, 0, "b"),
                ("nested", "Nested", 30, 0, "b"),
                ("deleted-folder", "Gone", 0, 1, "z"),
            ],
        )
        db.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            [
                ("doc-a", "same", 1, 0, "a"),
                ("doc-b", "same", 2, 0, "b"),
                ("doc-hostile", "quote ' ; DROP TABLE documents;--", 3, 0, "c"),
                ("doc-root", "Root note", 4, 0, "d"),
                ("doc-deleted", "Deleted notebook", 5, 1, "e"),
                ("doc-orphan", "Orphan notebook", 6, 0, "f"),
            ],
        )
        db.executemany(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("link-a", 0, "folder-a", ROOT_ID, ROOT_ID, "a", 0),
                ("link-b", 0, "folder-b", None, ROOT_ID, "b", 0),
                ("link-nested", 0, "nested", "folder-a", ROOT_ID, "b", 0),
                ("link-deleted-folder", 0, "deleted-folder", ROOT_ID, ROOT_ID, "z", 0),
                ("link-doc-a", 1, "doc-a", "folder-a", ROOT_ID, "a", 0),
                ("link-doc-b", 1, "doc-b", "folder-b", ROOT_ID, "a", 0),
                (
                    "link-hostile",
                    1,
                    "doc-hostile",
                    "nested",
                    ROOT_ID,
                    "c",
                    0,
                ),
                ("link-root", 1, "doc-root", "", ROOT_ID, "c", 0),
                ("link-deleted", 1, "doc-deleted", ROOT_ID, ROOT_ID, "e", 0),
                ("link-orphan", 1, "doc-orphan", "missing-parent", ROOT_ID, "f", 0),
                ("link-removed", 1, "doc-a", "folder-b", ROOT_ID, "z", 1),
            ],
        )
        if with_cycle:
            db.executemany(
                "INSERT INTO folders VALUES (?, ?, ?, ?, ?)",
                [
                    ("cycle-a", "Cycle A", 0, 0, "c"),
                    ("cycle-b", "Cycle B", 0, 0, "d"),
                ],
            )
            db.executemany(
                "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
                [
                    ("link-cycle-a", 0, "cycle-a", "cycle-b", ROOT_ID, "c", 0),
                    ("link-cycle-b", 0, "cycle-b", "cycle-a", ROOT_ID, "d", 0),
                ],
            )
        db.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("page-10", "doc-hostile", "template-10", "10", "group-10", 0),
                ("page-02", "doc-hostile", "template-02", "02", "group-02", 0),
                ("page-30", "doc-hostile", "template-30", "30", "group-30", 0),
                ("page-gone", "doc-hostile", None, "40", "group-gone", 1),
            ],
        )
        db.executemany(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("template-02", "doc-hostile", "attachment-safe", 9, 600, 800),
                ("template-10", "doc-hostile", "attachment-unsafe", 12, 600, 800),
                ("template-30", "doc-hostile", "attachment-fallback", 1, 600, 800),
            ],
        )
        db.executemany(
            "INSERT INTO attachments VALUES (?, ?)",
            [
                ("attachment-safe", "safe.pdf"),
                ("attachment-unsafe", "../outside.pdf"),
                (
                    "attachment-fallback",
                    "original/goodnotes/attachments/fallback.pdf",
                ),
            ],
        )
        db.executemany(
            "INSERT INTO notes_items VALUES (?, ?, ?)",
            [
                ("note-1", "group-02", 0),
                ("note-2", "group-02", 0),
                ("note-deleted", "group-02", 1),
            ],
        )
        if with_scan_page:
            db.execute(
                "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
                ("page-40", "doc-hostile", "template-40", "40", "group-40", 0),
            )
            db.execute(
                "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
                ("template-40", "doc-hostile", "attachment-fallback", 1, 600, 800),
            )


def _create_fts(path: Path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript(
            """
            CREATE TABLE annotation_text_content (
                text_id INTEGER PRIMARY KEY, page_id TEXT, text TEXT,
                engine_type TEXT DEFAULT 'myscript', priority TEXT DEFAULT 'primary'
            );
            CREATE TABLE textbox_text_content (
                text_id INTEGER PRIMARY KEY, page_id TEXT, text TEXT
            );
            CREATE TABLE pdf_text_content (
                text_id INTEGER PRIMARY KEY, text TEXT, attachment_id TEXT,
                pdf_page_no INTEGER, document_id TEXT
            );
            CREATE TABLE page_meta (
                page_id TEXT PRIMARY KEY, document_id TEXT, page_position TEXT,
                pdf_text_id INTEGER, template_attachment_id_and_page_no TEXT,
                is_doc_deleted INTEGER, is_page_deleted INTEGER
            );
            CREATE TABLE template_meta (
                template_id TEXT PRIMARY KEY, document_id TEXT,
                attachment_id TEXT, pdf_page_no INTEGER
            );
            """
        )
        db.executemany(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("page-02", "doc-hostile", "02", 101, "attachment-safe_9", 0, 0),
                ("page-10", "doc-hostile", "10", None, "attachment-unsafe_12", 0, 0),
                ("page-30", "doc-hostile", "30", None, None, 0, 0),
                ("page-gone", "doc-hostile", "40", None, None, 0, 1),
                ("deleted-page-meta", "doc-deleted", "01", None, None, 1, 0),
            ],
        )
        db.executemany(
            "INSERT INTO template_meta VALUES (?, ?, ?, ?)",
            [
                ("template-meta-10", "doc-hostile", "attachment-unsafe", 12),
            ],
        )
        db.executemany(
            "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (101, "direct PDF phrase", "attachment-safe", 9, "doc-hostile"),
                (102, "mapped PDF phrase", "attachment-unsafe", 12, "doc-hostile"),
            ],
        )
        db.executemany(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (1, "page-02", "Straße | Strasse", "myscript", "primary"),
                (2, "page-02", "fallback words", "other-engine", "secondary"),
                (3, "page-10", "recognized handwritten words", "myscript", "primary"),
                (4, "page-gone", "deleted page text", "myscript", "primary"),
            ],
        )
        db.executemany(
            "INSERT INTO textbox_text_content VALUES (?, ?, ?)",
            [
                (10, "page-02", "typed textbox phrase"),
                (11, "page-gone", "deleted textbox text"),
            ],
        )


def _write_pdf(path: Path, text: str) -> None:
    document = pymupdf.open()
    document.new_page().insert_text((72, 72), text)
    document.save(path)
    document.close()


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
        assert library.read_page("doc-hostile", 2).parts[-1].text == "mapped PDF phrase"
        fallback = library.read_page("doc-hostile", 3)
        assert len(fallback.parts) == 1
        assert fallback.parts[0].source == "pdf"
        assert fallback.parts[0].text == "Copied attachment fallback text"
        assert any("confined copied attachment" in warning for warning in fallback.warnings)
        with pytest.raises(GoodnotesError, match="one-based"):
            library.read_page("doc-hostile", 0)


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
        assert library._fts is not None
        library._fts.set_trace_callback(sql.append)
        page_text = library.read_page("doc-hostile", 1, source="handwriting")
        assert [part.source for part in page_text.parts] == ["handwriting", "handwriting"]
        assert any("machine-recognized" in warning for warning in page_text.warnings)
        assert not any("FROM textbox_text_content" in statement for statement in sql)
        assert not any("FROM pdf_text_content" in statement for statement in sql)

    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute("DROP TABLE textbox_text_content")
        db.execute("DROP TABLE pdf_text_content")
    with Library(sandbox) as library:
        page_text = library.read_page("doc-hostile", 1, source="handwriting")
        assert not any("Textbox text is unavailable" in warning for warning in page_text.warnings)
        assert not any("PDF text is unavailable" in warning for warning in page_text.warnings)


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
        assert library._fts is not None
        library._fts.set_trace_callback(sql.append)
        page_text = library.read_page("doc-hostile", 1, source="handwriting", max_chars=0)
        assert page_text.truncated is True
        assert page_text.parts == []
        assert not any("SELECT text FROM annotation_text_content" in statement for statement in sql)
        assert not any("substr(text" in statement for statement in sql)


def test_pdf_fallback_respects_read_budget_and_reports_truncation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library = _make_library(tmp_path, with_fts=False)
    with library:
        page_text = library.read_page("doc-hostile", 3, source="pdf", max_chars=5)
        assert page_text.truncated is True
        assert page_text.parts[0].text == "Copie"
        assert page_text.parts[0].truncated is True
        assert len(page_text.parts[0].text) <= 5

        def fail_if_opened(*_args: object, **_kwargs: object) -> None:
            raise AssertionError("zero-budget PDF read should not extract page text")

        monkeypatch.setattr("goodnotes_cli.library.pymupdf.open", fail_if_opened)
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
        assert direct_pdf[0].page == 1
        assert mapped_pdf[0].page == 2
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
        rows = library._search_rows("handwriting", "phrase")
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
        assert library.search("STRASSE") == []
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
        assert any("No confined copied attachment" in warning for warning in page_text.warnings)


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
        assert any(issue["issue"] == "missing-background" for issue in first["extraction_issues"])
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

    with Library(sandbox) as library:
        details = library.search_coverage(source="pdf", notebook="doc-hostile")
        pdf = details["sources"]["pdf"]
        assert pdf["availability"] == "available"
        assert pdf["pages_with_stored_nonempty_text"] == 1
        assert pdf["metadata_without_nonempty_text_pages"] == 1
        assert details["page_meta"]["mapped_active_pages"] == 3
        assert details["completeness"] == "unknown"
        assert any("no non-empty indexed PDF text" in warning for warning in details["warnings"])
        assert library.search("direct PDF phrase", source="pdf", notebook="doc-hostile") == []


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
        assert library._projection is not None
        statements: list[str] = []
        library._projection.set_trace_callback(statements.append)
        pdf = library.search_coverage(source="pdf", notebook="doc-hostile")
        assert set(pdf["sources"]) == {"pdf"}
        assert not any("needs_index" in statement for statement in statements)
        assert not any("Handwriting indexing is pending" in warning for warning in pdf["warnings"])
        assert not any("Handwriting text is unavailable" in warning for warning in pdf["warnings"])
        assert not any("Textbox text is unavailable" in warning for warning in pdf["warnings"])

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


def test_search_coverage_is_batched_metadata_only_and_handles_missing_tables(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with Library(sandbox) as library:
        assert library._fts is not None
        statements: list[str] = []
        library._fts.set_trace_callback(statements.append)
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
