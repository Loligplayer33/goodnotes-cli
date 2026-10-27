"""Generated cases for page-number cache scope."""

import sqlite3
from pathlib import Path

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.library import Library
from tests.support.library import ROOT_ID, _create_snapshot


def test_page_number_cache_tracks_changed_and_mutated_notebook_scopes(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)",
            ("other-page", "doc-a", None, "01", None, 0),
        )
    original = {"page-02": 1, "page-10": 2, "page-30": 3}
    scope: set[str] = set()
    cases = [
        (set(), {}),
        ({"doc-hostile"}, original),
        ({"doc-a"}, {"other-page": 1}),
        ({"doc-hostile", "doc-a"}, {**original, "other-page": 1}),
        (set(), {}),
        ({"doc-hostile"}, original),
    ]
    with Library(sandbox) as library:
        for notebooks, expected in cases:
            scope.clear()
            scope.update(notebooks)
            assert library._pages.page_numbers_by_id(scope) == expected
            assert library._pages.page_number_for_id(
                "doc-hostile", "page-10", set(scope)
            ) == expected.get("page-10")


def test_search_validates_page_scope_once_for_many_matches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path)
    assert sandbox.fts_path is not None
    with sqlite3.connect(sandbox.fts_path) as connection:
        connection.executemany(
            "INSERT INTO annotation_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (1000 + i, "page-10", f"recognized generated {i}", "myscript", "primary")
                for i in range(100)
            ],
        )
    with Library(sandbox) as library:
        calls = 0
        original = library._pages._ensure_page_numbers

        def count(scope: set[str]) -> None:
            nonlocal calls
            calls += 1
            original(scope)

        monkeypatch.setattr(library._pages, "_ensure_page_numbers", count)
        hits = library.search("recognized", source="handwriting", notebook="doc-hostile", limit=1)
        assert len(hits) == 1
        assert hits[0].page == 2
        assert calls == 1
        assert (
            library.search("recognized", source="handwriting", notebook="doc-hostile", limit=1)
            == hits
        )
        assert calls == 2


def test_foreign_invalid_page_id_does_not_break_selected_root(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path)
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)", ("foreign", "Foreign", 1, 0, "a")
        )
        connection.execute(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("foreign-link", 1, "foreign", "foreign-root", "foreign-root", "a", 0),
        )
        connection.execute(
            "INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)", ("", "foreign", None, "a", None, 0)
        )
    with Library(sandbox, root_folder_id=ROOT_ID) as library:
        assert library.search("recognized", source="handwriting", notebook="doc-hostile")
        library.search_coverage(source="handwriting", notebook="doc-hostile")
    for root in ("foreign-root", None):
        with Library(sandbox, root_folder_id=root) as library:
            with pytest.raises(GoodnotesError, match="invalid pages.id"):
                library.search("recognized", source="handwriting")
