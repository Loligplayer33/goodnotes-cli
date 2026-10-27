"""Generated immutable-snapshot fixture helpers shared by tests."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from goodnotes_cli.safety import Sandbox


def populate_snapshot(root: Path, *, include_fts: bool = False) -> None:
    database_dir = root / "Library" / "Databases"
    attachments_dir = root / "Library" / "Attachments"
    database_dir.mkdir(parents=True)
    attachments_dir.mkdir()
    with sqlite3.connect(database_dir / "projection.sqlite") as connection:
        connection.execute("CREATE TABLE item (name TEXT)")
        connection.execute("INSERT INTO item VALUES ('sandbox')")
    if include_fts:
        with sqlite3.connect(database_dir / "fts.sqlite") as connection:
            connection.execute("CREATE TABLE text_cache (body TEXT)")
    (root / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )


def make_snapshot(root: Path, *, include_fts: bool = False) -> Sandbox:
    populate_snapshot(root, include_fts=include_fts)
    return Sandbox.load(root)
