"""Generated cases for page-number cache scope."""

import sqlite3
from pathlib import Path

from goodnotes_cli.library import Library
from tests.support.library import _create_snapshot


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
