"""Generated fixtures for root-scoped library catalogs."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pymupdf
import pytest
from helpers import invoke_in_process
from test_cli import successful
from test_library import ROOT_ID, _create_snapshot

import goodnotes_cli.cache as cache_module
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from goodnotes_cli.safety import Sandbox

FOREIGN_ROOT_ID = "foreign-virtual-root"
EMPTY_HISTORICAL_ROOT_ID = "empty-historical-root"


def _add_foreign_root(
    sandbox: Sandbox,
    *,
    with_cycle: bool = True,
    with_multiple_parents: bool = True,
) -> None:
    """Add a second synthetic root with colliding names, text, and a PDF page."""
    with sqlite3.connect(sandbox.projection_path) as db:
        db.executemany(
            "INSERT INTO folders VALUES (?, ?, ?, ?, ?)",
            [
                ("foreign-folder-a", "Shared A", 7, 0, "a"),
                ("foreign-folder-b", "Shared B", 8, 0, "b"),
            ]
            + (
                [
                    ("foreign-cycle-a", "Cycle A", 9, 0, "c"),
                    ("foreign-cycle-b", "Cycle B", 10, 0, "d"),
                ]
                if with_cycle
                else []
            ),
        )
        documents = [
            ("foreign-doc-a", "same", 7, 0, "a"),
            ("foreign-doc-b", "same", 8, 0, "b"),
            ("foreign-doc-root", "Root note", 9, 0, "c"),
        ]
        if with_multiple_parents:
            documents.append(("foreign-doc-multiple-parents", "Repeated", 10, 0, "d"))
        db.executemany("INSERT INTO documents VALUES (?, ?, ?, ?, ?)", documents)

        links = [
            (
                "foreign-link-folder-a",
                0,
                "foreign-folder-a",
                FOREIGN_ROOT_ID,
                FOREIGN_ROOT_ID,
                "a",
                0,
            ),
            (
                "foreign-link-folder-b",
                0,
                "foreign-folder-b",
                FOREIGN_ROOT_ID,
                FOREIGN_ROOT_ID,
                "b",
                0,
            ),
            ("foreign-link-doc-a", 1, "foreign-doc-a", "foreign-folder-a", FOREIGN_ROOT_ID, "a", 0),
            ("foreign-link-doc-b", 1, "foreign-doc-b", "foreign-folder-b", FOREIGN_ROOT_ID, "a", 0),
            (
                "foreign-link-doc-root",
                1,
                "foreign-doc-root",
                FOREIGN_ROOT_ID,
                FOREIGN_ROOT_ID,
                "c",
                0,
            ),
        ]
        if with_cycle:
            links.extend(
                [
                    (
                        "foreign-link-cycle-a",
                        0,
                        "foreign-cycle-a",
                        "foreign-cycle-b",
                        FOREIGN_ROOT_ID,
                        "c",
                        0,
                    ),
                    (
                        "foreign-link-cycle-b",
                        0,
                        "foreign-cycle-b",
                        "foreign-cycle-a",
                        FOREIGN_ROOT_ID,
                        "d",
                        0,
                    ),
                ]
            )
        if with_multiple_parents:
            links.extend(
                [
                    (
                        "foreign-link-repeated-a",
                        1,
                        "foreign-doc-multiple-parents",
                        "foreign-folder-a",
                        FOREIGN_ROOT_ID,
                        "d",
                        0,
                    ),
                    (
                        "foreign-link-repeated-b",
                        1,
                        "foreign-doc-multiple-parents",
                        "foreign-folder-b",
                        FOREIGN_ROOT_ID,
                        "d",
                        0,
                    ),
                ]
            )
        db.executemany("INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)", links)
        db.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            ("foreign-page-a", "foreign-doc-a", "foreign-template-a", "01", "foreign-group-a", 0),
        )
        db.execute(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
            ("foreign-template-a", "foreign-doc-a", "attachment-foreign", 1, 600, 800),
        )
        db.execute(
            "INSERT INTO attachments VALUES (?, ?)",
            ("attachment-foreign", "foreign.pdf"),
        )

    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as db:
        db.execute(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "foreign-page-a",
                "foreign-doc-a",
                "01",
                901,
                "attachment-foreign_1",
                0,
                0,
            ),
        )
        db.execute(
            "INSERT INTO template_meta VALUES (?, ?, ?, ?)",
            ("foreign-template-meta-a", "foreign-doc-a", "attachment-foreign", 1),
        )
        db.execute(
            "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
            (901, "foreign indexed PDF marker", "attachment-foreign", 1, "foreign-doc-a"),
        )
        db.execute(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            (901, "foreign-page-a", "foreign handwriting marker", "myscript", "primary"),
        )
        db.execute(
            "INSERT INTO textbox_text_content VALUES (?, ?, ?)",
            (901, "foreign-page-a", "foreign typed marker"),
        )

    pdf = pymupdf.open()
    pdf.new_page().insert_text((72, 72), "foreign PDF scan marker")
    pdf.save(sandbox.attachments_dir / "foreign.pdf")
    pdf.close()


def _add_empty_historical_root(sandbox: Sandbox) -> None:
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            ("deleted-empty-root-doc", "Historical notebook", 1, 1, "a"),
        )
        db.execute(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "deleted-empty-root-link",
                1,
                "deleted-empty-root-doc",
                EMPTY_HISTORICAL_ROOT_ID,
                EMPTY_HISTORICAL_ROOT_ID,
                "a",
                1,
            ),
        )


def test_selected_root_excludes_foreign_catalog_search_and_page_metadata(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with Library(sandbox) as base_library:
        base_stats = base_library.stats()
    _add_foreign_root(sandbox)

    with Library(sandbox, root_folder_id=ROOT_ID) as library:
        assert [item.name for item in library.list_items("/")] == [
            "Shared A",
            "Shared B",
            "Root note",
        ]
        assert library.resolve("Shared A/same", kind="notebook").id == "doc-a"
        assert library.resolve("Shared B/same", kind="notebook").id == "doc-b"
        assert all(not item.id.startswith("foreign-") for item in library.tree())
        with pytest.raises(GoodnotesError, match="No active"):
            library.resolve("foreign-doc-a")

        assert library.search("foreign handwriting marker", source="handwriting") == []
        assert library.search("foreign typed marker", source="textbox") == []
        assert library.search("foreign indexed PDF marker", source="pdf") == []
        scan = library.scan_pdfs("foreign PDF scan marker", page_limit=250)
        assert scan["hits"] == []

        stats = library.stats()
        for key in (
            "active_folders",
            "active_notebooks",
            "active_pages",
            "visible_folders",
            "visible_notebooks",
            "orphaned_items",
            "broken_links",
            "duplicate_parent_items",
            "cycle_folders",
            "pages_without_index_metadata",
            "index_metadata_without_active_pages",
        ):
            assert stats[key] == base_stats[key], key
        assert stats["duplicate_parent_items"] == 0
        assert stats["cycle_folders"] == 0
        assert (
            stats["index_metadata_without_active_pages"]
            == (base_stats["index_metadata_without_active_pages"])
        )


def test_unscoped_library_keeps_all_root_ambiguity_and_graph_diagnostics(
    tmp_path: Path,
) -> None:
    clean = _create_snapshot(tmp_path / "clean-roots")
    _add_foreign_root(clean, with_cycle=False, with_multiple_parents=False)
    with Library(clean) as library:
        with pytest.raises(GoodnotesError, match="ambiguous"):
            library.resolve("Shared A/same", kind="notebook")
        assert [hit.document_id for hit in library.search("foreign handwriting marker")] == [
            "foreign-doc-a"
        ]
        scan = library.scan_pdfs("foreign PDF scan marker", page_limit=250)
        assert [hit.document_id for hit in scan["hits"]] == ["foreign-doc-a"]

    with_graph_errors = _create_snapshot(tmp_path / "roots-with-errors")
    _add_foreign_root(with_graph_errors)
    with Library(with_graph_errors) as library:
        stats = library.stats()
        assert stats["duplicate_parent_items"] == 1
        assert stats["cycle_folders"] == 2
        with pytest.raises(GoodnotesError, match="multiple parent links"):
            library.resolve("Shared A/same", kind="notebook")


def test_unknown_explicit_root_is_rejected_during_open_before_index_access(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")

    def unexpected_index_access(*_args: object, **_kwargs: object) -> None:
        pytest.fail("An invalid root must fail before opening the search index.")

    monkeypatch.setattr(Library, "_ensure_fts", unexpected_index_access)
    with pytest.raises(GoodnotesError, match="root"):
        with Library(sandbox, root_folder_id="unknown-root"):
            pytest.fail("Unknown explicit roots must fail while opening the catalog.")


def test_historical_root_without_active_entries_is_a_valid_empty_scope(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    _add_empty_historical_root(sandbox)

    with Library(sandbox, root_folder_id=EMPTY_HISTORICAL_ROOT_ID) as library:
        assert library.list_items("/") == []
        stats = library.stats()
        assert stats["active_folders"] == 0
        assert stats["active_notebooks"] == 0
        assert stats["visible_folders"] == 0
        assert stats["visible_notebooks"] == 0


def test_managed_root_selection_is_publicly_configurable_and_sandbox_local(
    tmp_path: Path,
) -> None:
    sandbox = _create_snapshot(tmp_path / "generated-source")
    _add_foreign_root(sandbox, with_cycle=False, with_multiple_parents=False)
    state = tmp_path / "state"
    cache_module.refresh_cache(sandbox.root / "Library", state)
    (state / "library-root.json").write_text(
        json.dumps({"root_folder_id": ROOT_ID}), encoding="utf-8"
    )

    configured = successful(invoke_in_process(["ls"], tmp_path, data_dir=state, verbose=False))
    assert {item["id"] for item in configured["items"]} == {
        "folder-a",
        "folder-b",
        "doc-root",
    }
    assert len(configured["items"]) == 3
    assert all("root_folder_id" not in item for item in configured["items"])

    all_roots = successful(
        invoke_in_process(["--library-root", "all", "ls"], tmp_path, data_dir=state, verbose=False)
    )
    assert {item["id"] for item in all_roots["items"]} == {
        "folder-a",
        "folder-b",
        "doc-root",
        "foreign-folder-a",
        "foreign-folder-b",
        "foreign-doc-root",
    }
    assert len(all_roots["items"]) == 6

    info = successful(
        invoke_in_process(
            ["--fields", "root_folder_id", "info", "folder-a"],
            tmp_path,
            data_dir=state,
            verbose=False,
        )
    )
    assert info["root_folder_id"] == ROOT_ID

    explicit_sandbox = successful(
        invoke_in_process(["ls"], tmp_path, sandbox=sandbox.root, data_dir=state, verbose=False)
    )
    assert len(explicit_sandbox["items"]) == 6
    assert {item["id"] for item in explicit_sandbox["items"]} == {
        "folder-a",
        "folder-b",
        "doc-root",
        "foreign-folder-a",
        "foreign-folder-b",
        "foreign-doc-root",
    }
