import errno
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import pytest

import goodnotes_cli.snapshot as snapshot_module
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.safety import Sandbox, open_database
from goodnotes_cli.snapshot import create_snapshot


def make_library(root: Path, *, wal: bool = False) -> Path:
    library = root / "Library"
    database_dir = library / "Databases"
    attachments = library / "Attachments"
    database_dir.mkdir(parents=True)
    attachments.mkdir()
    database_path = database_dir / "projection.sqlite"
    connection = sqlite3.connect(database_path)
    if wal:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
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
        """
    )
    connection.execute("CREATE TABLE item (name TEXT)")
    connection.execute("INSERT INTO item VALUES ('first')")
    connection.commit()
    if not wal:
        connection.close()
    (attachments / "page.pdf").write_bytes(b"synthetic attachment")
    (attachments / "nested").mkdir()
    (attachments / "nested" / "image.png").write_bytes(b"synthetic image")
    return library


def hash_tree(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_snapshot_copies_core_files_and_writes_marker_last(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    expected_source_files = hash_tree(source_library)
    destination = tmp_path / "snapshots" / "initial"

    result = create_snapshot(source_library, destination)

    assert result == destination
    sandbox = Sandbox.load(destination)
    assert sandbox.fts_path is None
    assert (sandbox.attachments_dir / "page.pdf").read_bytes() == b"synthetic attachment"
    assert (sandbox.attachments_dir / "nested" / "image.png").read_bytes() == b"synthetic image"
    assert hash_tree(source_library) == expected_source_files

    manifest = json.loads((destination / "goodnotes-snapshot.json").read_text(encoding="utf-8"))
    assert manifest["format_version"] == 1
    file_records = {item["path"]: item for item in manifest["files"]}
    assert "Library/Databases/projection.sqlite" in file_records
    assert (
        file_records["Library/Databases/projection.sqlite"]["sha256"]
        == hashlib.sha256(sandbox.projection_path.read_bytes()).hexdigest()
    )
    assert destination.stat().st_mode & 0o222 == 0
    assert sandbox.projection_path.stat().st_mode & 0o222 == 0


def test_real_cow_clone_has_independent_writes(tmp_path: Path) -> None:
    if sys.platform != "darwin":
        pytest.skip("fclonefileat is a macOS API")
    source = tmp_path / "source.bin"
    destination = tmp_path / "clone.bin"
    source.write_bytes(b"original data")
    source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        try:
            cloned = snapshot_module._clone_file_from_fd(source_fd, destination)
        except OSError as exc:
            if exc.errno in snapshot_module._CLONE_FALLBACK_ERRNOS:
                pytest.skip("temporary directory filesystem does not support COW clones")
            raise
    finally:
        os.close(source_fd)

    if not cloned:
        pytest.skip("fclonefileat is unavailable")
    assert source.read_bytes() == b"original data"
    assert destination.read_bytes() == b"original data"
    assert source.stat().st_ino != destination.stat().st_ino

    destination.write_bytes(b"destination change")
    assert source.read_bytes() == b"original data"
    source.write_bytes(b"source change")
    assert destination.read_bytes() == b"destination change"


def test_snapshot_falls_back_to_copy_after_exdev_and_keeps_source_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_library = make_library(tmp_path / "source")
    source_file = source_library / "Attachments" / "page.pdf"
    source_before = hash_tree(source_library)
    original_clone = snapshot_module._clone_file_from_fd
    failed_destinations: list[Path] = []

    def fail_one_clone(source_fd: int, destination: Path) -> bool:
        if destination.name == source_file.name:
            failed_destinations.append(destination)
            raise OSError(errno.EXDEV, "cross-device clone")
        return original_clone(source_fd, destination)

    monkeypatch.setattr(snapshot_module, "_clone_file_from_fd", fail_one_clone)
    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    copied_file = snapshot / "Library" / "Attachments" / source_file.name

    assert failed_destinations == [copied_file]
    assert source_file.read_bytes() == b"synthetic attachment"
    assert copied_file.read_bytes() == source_file.read_bytes()
    assert hash_tree(source_library) == source_before

    copied_file.chmod(0o644)
    copied_file.write_bytes(b"private copy change")
    assert source_file.read_bytes() == b"synthetic attachment"


def test_snapshot_consolidates_copied_wal_and_reads_committed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_library = make_library(tmp_path / "source", wal=True)
    source_connection = sqlite3.connect(source_library / "Databases" / "projection.sqlite")
    source_connection.execute("INSERT INTO item VALUES ('committed in wal')")
    source_connection.commit()
    source_db = source_library / "Databases" / "projection.sqlite"
    wal_path = Path(f"{source_db}-wal")
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    source_before = hash_tree(source_library)
    connection_targets: list[str] = []
    original_connect = snapshot_module.sqlite3.connect

    def record_connection(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection_targets.append(str(database))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", record_connection)

    snapshot = create_snapshot(source_library, tmp_path / "snapshot")

    copied_database = snapshot / "Library" / "Databases" / "projection.sqlite"
    assert not Path(f"{copied_database}-wal").exists()
    assert not Path(f"{copied_database}-shm").exists()
    with open_database(snapshot / "Library" / "Databases" / "projection.sqlite") as connection:
        names = [row[0] for row in connection.execute("SELECT name FROM item ORDER BY rowid")]
    assert names == ["first", "committed in wal"]
    assert wal_path.is_file() and wal_path.stat().st_size > 0
    assert hash_tree(source_library) == source_before
    assert connection_targets
    assert all(str(snapshot) in target for target in connection_targets)
    assert all(source_db.as_posix() not in target for target in connection_targets)
    source_connection.close()


def test_snapshot_consolidates_optional_fts_database_sidecars(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    fts_path = source_library / "Databases" / "fts.sqlite"
    connection = sqlite3.connect(fts_path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA wal_autocheckpoint=0")
    connection.execute("CREATE TABLE text_cache (body TEXT)")
    connection.execute("INSERT INTO text_cache VALUES ('synthetic text')")
    connection.commit()
    assert Path(f"{fts_path}-wal").is_file()

    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    sandbox = Sandbox.load(snapshot)

    assert sandbox.fts_path is not None
    assert not Path(f"{sandbox.fts_path}-wal").exists()
    with open_database(sandbox.fts_path) as snapshot_connection:
        assert snapshot_connection.execute("SELECT body FROM text_cache").fetchone()[0] == (
            "synthetic text"
        )
    connection.close()


def test_snapshot_refuses_to_overwrite_existing_destination(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    destination = tmp_path / "existing"
    destination.mkdir()
    sentinel = destination / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(GoodnotesError, match="Refusing to overwrite"):
        create_snapshot(source_library, destination)

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_snapshot_rejects_non_sqlite_projection_and_cleans_destination(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    (source_library / "Databases" / "projection.sqlite").write_bytes(b"not a SQLite file")
    destination = tmp_path / "snapshot"

    with pytest.raises(GoodnotesError, match="integrity or schema validation"):
        create_snapshot(source_library, destination)

    assert not destination.exists()


def test_snapshot_rejects_projection_missing_core_schema(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    database_path = source_library / "Databases" / "projection.sqlite"
    with sqlite3.connect(database_path) as connection:
        for table in (
            "folders",
            "documents",
            "folder_to_folder_items",
            "pages",
            "item",
        ):
            connection.execute(f"DROP TABLE {table}")
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(GoodnotesError, match="missing core tables"):
        create_snapshot(source_library, tmp_path / "snapshot")


def test_snapshot_cleans_partial_output_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_library = make_library(tmp_path / "source")

    def interrupt_copy(source: Path, destination: Path, expected: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr(snapshot_module, "_copy_regular_file", interrupt_copy)
    destination = tmp_path / "snapshot"

    with pytest.raises(KeyboardInterrupt):
        create_snapshot(source_library, destination)

    assert not destination.exists()
    assert not (destination / "goodnotes-snapshot.json").exists()


def test_snapshot_rejects_source_destination_overlap(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    destination = source_library / "snapshot"

    with pytest.raises(GoodnotesError, match="must not overlap"):
        create_snapshot(source_library, destination)

    assert not destination.exists()


def test_snapshot_rejects_symlink_inputs_and_cleans_destination(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source")
    outside = tmp_path / "outside.pdf"
    outside.write_bytes(b"outside")
    (source_library / "Attachments" / "linked.pdf").symlink_to(outside)
    destination = tmp_path / "snapshot"

    with pytest.raises(GoodnotesError, match="Symlinks are not allowed"):
        create_snapshot(source_library, destination)

    assert not destination.exists()


def test_snapshot_retries_after_a_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_library = make_library(tmp_path / "source")
    attachment = source_library / "Attachments" / "page.pdf"
    original_copy = snapshot_module._copy_snapshot_tree
    calls = 0

    def copy_then_change(source: Path, destination: Path, entries: list[object]) -> None:
        nonlocal calls
        original_copy(source, destination, entries)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            attachment.write_bytes(b"changed during first copy")

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_tree", copy_then_change)
    snapshot = create_snapshot(source_library, tmp_path / "snapshot", max_attempts=3)

    assert calls == 2
    assert (snapshot / "Library" / "Attachments" / "page.pdf").read_bytes() == (
        b"changed during first copy"
    )
    Sandbox.load(snapshot)


def test_snapshot_fails_during_active_wal_writes_without_locking_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_library = make_library(tmp_path / "source", wal=True)
    source_database = source_library / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(source_database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO item VALUES ('writer open')")
    writer.commit()

    original_copy_file = snapshot_module._copy_regular_file
    copy_mutations = 0

    def copy_then_write(source: Path, destination: Path, expected: object) -> None:
        nonlocal copy_mutations
        original_copy_file(source, destination, expected)  # type: ignore[arg-type]
        if source.name == "projection.sqlite":
            copy_mutations += 1
            writer.execute("INSERT INTO item VALUES (?)", (f"during copy {copy_mutations}",))
            writer.commit()

    connection_targets: list[str] = []
    original_connect = snapshot_module.sqlite3.connect

    def record_connection(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection_targets.append(str(database))
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_copy_regular_file", copy_then_write)
    monkeypatch.setattr(snapshot_module.sqlite3, "connect", record_connection)
    destination = tmp_path / "snapshot"

    with pytest.raises(GoodnotesError, match="kept changing"):
        create_snapshot(source_library, destination, max_attempts=2)

    assert copy_mutations == 2
    assert all(source_database.as_posix() not in target for target in connection_targets)
    assert not destination.exists()
    assert not (destination / "goodnotes-snapshot.json").exists()

    writer.execute("INSERT INTO item VALUES ('after failed capture')")
    writer.commit()
    assert writer.execute("SELECT name FROM item ORDER BY rowid DESC LIMIT 1").fetchone()[0] == (
        "after failed capture"
    )
    writer.close()


def test_old_snapshot_reads_remain_stable_during_refresh(tmp_path: Path) -> None:
    source_library = make_library(tmp_path / "source", wal=True)
    source_database = source_library / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(source_database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")

    try:
        old_snapshot = create_snapshot(source_library, tmp_path / "snapshots" / "first")
        old_database = old_snapshot / "Library" / "Databases" / "projection.sqlite"
        with open_database(old_database) as old_reader:
            before_refresh = [
                row[0] for row in old_reader.execute("SELECT name FROM item ORDER BY rowid")
            ]

            writer.execute("INSERT INTO item VALUES ('new source row')")
            writer.commit()
            new_snapshot = create_snapshot(source_library, tmp_path / "snapshots" / "second")

            after_refresh = [
                row[0] for row in old_reader.execute("SELECT name FROM item ORDER BY rowid")
            ]
            new_database = new_snapshot / "Library" / "Databases" / "projection.sqlite"
            with open_database(new_database) as new_reader:
                refreshed = [
                    row[0] for row in new_reader.execute("SELECT name FROM item ORDER BY rowid")
                ]
        assert before_refresh == ["first"]
        assert after_refresh == before_refresh
        assert refreshed == ["first", "new source row"]
    finally:
        writer.close()
