"""Generated SQLite snapshots and source libraries shared by tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pymupdf

from goodnotes_cli.safety import Sandbox

ROOT_ID = "virtual-root"


def _create_snapshot(
    root: Path,
    *,
    with_fts: bool = True,
    with_cycle: bool = False,
    with_scan_page: bool = False,
    fts_encoding: str | None = None,
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
        _create_fts(database_dir / "fts.sqlite", encoding=fts_encoding)
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


def _create_fts(path: Path, *, encoding: str | None = None) -> None:
    with sqlite3.connect(path) as db:
        if encoding is not None:
            db.execute(f"PRAGMA encoding = '{encoding}'")
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


def _add_repeated_fallback_pages(sandbox: Sandbox, count: int) -> None:
    assert count >= 0
    with sqlite3.connect(sandbox.projection_path) as db:
        db.executemany(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"batch-template-{index:04d}", "doc-hostile", "attachment-fallback", 1, 600, 800)
                for index in range(count)
            ],
        )
        db.executemany(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    f"batch-page-{index:04d}",
                    "doc-hostile",
                    f"batch-template-{index:04d}",
                    f"batch-{index:04d}",
                    "group-02",
                    0,
                )
                for index in range(count)
            ],
        )


def make_library(root: Path) -> Path:
    """Create a minimal generated source library for capture tests."""
    library = root / "Library"
    database_dir = library / "Databases"
    attachments = library / "Attachments"
    database_dir.mkdir(parents=True)
    attachments.mkdir()
    with sqlite3.connect(database_dir / "projection.sqlite") as connection:
        connection.executescript(
            """
            CREATE TABLE folders (
                id TEXT, name TEXT, updated_at REAL, deleted INTEGER, position TEXT
            );
            CREATE TABLE documents (
                id TEXT, name TEXT, updated_at REAL, deleted INTEGER, position TEXT
            );
            CREATE TABLE folder_to_folder_items (
                id TEXT, item_type INTEGER, item_id TEXT, parent_folder_id TEXT,
                root_folder_id TEXT, item_position TEXT, deleted INTEGER
            );
            CREATE TABLE pages (
                id TEXT, document_id TEXT, template_id TEXT, position TEXT,
                group_id TEXT, deleted INTEGER
            );
            CREATE TABLE item (name TEXT);
            INSERT INTO item VALUES ('generated fixture');
            """
        )
    (attachments / "page.pdf").write_bytes(b"generated fixture attachment")
    (attachments / "nested").mkdir()
    (attachments / "nested" / "image.png").write_bytes(b"generated fixture image")
    return library
