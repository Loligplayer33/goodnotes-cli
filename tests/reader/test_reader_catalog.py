"""The catalog owns navigation and graph diagnostics for one reader session."""

import sqlite3

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.reader.catalog import Catalog, _DifferentKindError
from goodnotes_cli.reader.session import ReaderSession
from tests.support.library import ROOT_ID, _create_snapshot


def test_catalog_navigation_and_root_scope(tmp_path):
    session = ReaderSession(_create_snapshot(tmp_path / "snapshot"))
    session.open()
    try:
        catalog = Catalog(session, ROOT_ID)
        catalog.load()
        assert catalog.resolve("doc-root").path == "Root note"
        assert catalog.resolve("Shared A/same").id == "doc-a"
        assert "doc-a" in catalog.notebook_ids_under(catalog.folder_key("Shared A", True))
        assert catalog.active_notebook_count == len(catalog.active_items_by_kind("notebook"))
        with pytest.raises(_DifferentKindError):
            catalog.resolve("doc-a", "folder")
        with pytest.raises(GoodnotesError, match="ambiguous"):
            catalog.resolve("same")
    finally:
        session.close()
    with pytest.raises(GoodnotesError, match="with.*block"):
        catalog.resolve("doc-root")


def test_catalog_diagnostics_for_duplicate_parent(tmp_path):
    sandbox = _create_snapshot(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("duplicate", 1, "doc-a", "folder-b", ROOT_ID, "z", 0),
        )
    session = ReaderSession(sandbox)
    session.open()
    try:
        catalog = Catalog(session)
        catalog.load()
        assert catalog.duplicate_parent_count == 1
        assert catalog.paths[("notebook", "doc-a")] is None
        with pytest.raises(GoodnotesError, match="multiple parent"):
            catalog.tree()
    finally:
        session.close()
