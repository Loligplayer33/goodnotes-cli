import errno
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from contextlib import closing
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


@pytest.fixture
def wal_library(tmp_path: Path) -> Iterator[Path]:
    source = make_library(tmp_path / "source")
    with closing(sqlite3.connect(source / "Databases/projection.sqlite")) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE item SET name=name")
        writer.commit()
        yield source


def add_native_store(library: Path) -> Path:
    native = library / "Databases" / "notes_main.db"
    native.mkdir()
    (native / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (native / "MANIFEST-000001").write_bytes(b"generated manifest bytes")
    (native / "000002.log").write_bytes(b"generated wal bytes")
    (native / "OPTIONS-000003").write_bytes(b"generated options, never executed")
    (native / "000004.sst").write_bytes(b"generated immutable table" * 1024)
    (native / "archive").mkdir()
    (native / "archive" / "000000.log").write_bytes(b"generated archived wal")
    return native


def marker_data(snapshot: Path) -> dict[str, Any]:
    return json.loads((snapshot / "goodnotes-snapshot.json").read_text(encoding="utf-8"))


def test_snapshot_captures_native_store_as_immutable_bytes(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    native = add_native_store(source)
    before = hash_tree(source)
    metadata_before = {
        str(path.relative_to(source)): snapshot_module._stat_key(path.lstat())
        for path in source.rglob("*")
    }

    snapshot = create_snapshot(source, tmp_path / "snapshot")
    copied_native = snapshot / "Library" / "Databases" / "notes_main.db"
    assert hash_tree(copied_native) == hash_tree(native)
    assert hash_tree(source) == before
    assert {
        str(path.relative_to(source)): snapshot_module._stat_key(path.lstat())
        for path in source.rglob("*")
    } == metadata_before
    for copied in copied_native.rglob("*"):
        assert copied.stat().st_mode & 0o222 == 0
        original = native / copied.relative_to(copied_native)
        assert copied.stat().st_ino != original.stat().st_ino
    marker = marker_data(snapshot)
    assert marker["capture_stats"]["native_store_present"] is True
    assert "Library/Databases/notes_main.db/000002.log" in {
        record["path"] for record in marker["files"]
    }


@pytest.mark.parametrize("unsafe", ["file-symlink", "directory-symlink", "fifo", "store-file"])
def test_snapshot_rejects_unsafe_native_inputs(tmp_path: Path, unsafe: str) -> None:
    source = make_library(tmp_path / "source")
    native = add_native_store(source)
    if unsafe == "file-symlink":
        (native / "linked.sst").symlink_to(native / "000004.sst")
    elif unsafe == "directory-symlink":
        (native / "linked").symlink_to(native / "archive", target_is_directory=True)
    elif unsafe == "fifo":
        os.mkfifo(native / "pipe")
    else:
        snapshot_module._make_writable_and_remove(native)
        native.write_bytes(b"not a native directory")
    destination = tmp_path / "snapshot"
    with pytest.raises(GoodnotesError):
        create_snapshot(source, destination)
    assert not destination.exists()


def test_incremental_snapshot_hashes_only_changed_source_files_and_reuses_verified_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    native = add_native_store(source)
    original = create_snapshot(source, tmp_path / "first")
    changed = source / "Attachments" / "page.pdf"
    changed.write_bytes(b"new generated annotation background")
    original_hash = snapshot_module._hash_file
    hashed: list[Path] = []

    def record_hash(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(snapshot_module, "_hash_file", record_hash)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=original)
    stats = marker_data(second)["capture_stats"]
    source_hashes = [path for path in hashed if source in path.parents]
    assert source_hashes == [changed]
    assert all(original not in path.parents for path in hashed)
    assert stats["source_captured_files"] == 1
    assert stats["source_captured_bytes"] == changed.stat().st_size
    assert stats["cache_reused_files"] == 8
    assert stats["cache_reused_bytes"] >= (native / "000004.sst").stat().st_size
    copied_sst = second / "Library" / "Databases" / "notes_main.db" / "000004.sst"
    old_sst = original / "Library" / "Databases" / "notes_main.db" / "000004.sst"
    assert copied_sst.read_bytes() == old_sst.read_bytes()
    assert copied_sst.stat().st_ino != old_sst.stat().st_ino
    assert copied_sst.stat().st_ino != (native / "000004.sst").stat().st_ino


def test_changed_sqlite_family_reuses_unchanged_source_member_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    database = source / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("UPDATE item SET name='first WAL state'")
    writer.commit()
    first = create_snapshot(source, tmp_path / "first")
    main_key = snapshot_module._stat_key(database.lstat())
    assert database.read_bytes() != (first / "Library/Databases/projection.sqlite").read_bytes()

    writer.execute("UPDATE item SET name='second WAL state'")
    writer.commit()
    assert snapshot_module._stat_key(database.lstat()) == main_key
    original_hash = snapshot_module._hash_file
    hashed: list[Path] = []

    def record_hash(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(snapshot_module, "_hash_file", record_hash)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    stats = marker_data(second)["capture_stats"]

    assert database not in hashed
    assert any(second / "Library" in path.parents for path in hashed)
    assert stats["source_bytes_hashed"] < stats["source_captured_bytes"]
    with open_database(second / "Library/Databases/projection.sqlite") as reader:
        assert reader.execute("SELECT name FROM item").fetchone()[0] == "second WAL state"
    writer.close()


def test_same_size_edit_with_restored_mtime_rehashes_changed_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    attachment = source / "Attachments/page.pdf"
    first = create_snapshot(source, tmp_path / "first")
    previous_key = snapshot_module._stat_key(attachment.lstat())
    original_hash = snapshot_module._hash_file
    hashed: list[Path] = []

    def record_hash(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
        hashed.append(path)
        return original_hash(path)

    attachment.write_bytes(b"x" * previous_key[3])
    current = attachment.stat()
    os.utime(attachment, ns=(current.st_atime_ns, previous_key[4]))
    assert attachment.stat().st_mtime_ns == previous_key[4]
    assert snapshot_module._stat_key(attachment.lstat()) != previous_key
    monkeypatch.setattr(snapshot_module, "_hash_file", record_hash)

    create_snapshot(source, tmp_path / "second", previous_snapshot=first)

    assert attachment in hashed


def test_wrong_prior_original_source_digest_fails_destination_verification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    data = marker_data(first)
    original_record = next(
        record
        for record in data["source_inventory"]
        if record["path"] == "Databases/projection.sqlite"
    )
    original_record["sha256"] = "f" * 64
    marker = first / "goodnotes-snapshot.json"
    marker.chmod(0o644)
    marker.write_text(json.dumps(data), encoding="utf-8")
    marker.chmod(0o444)
    (source / "Databases/projection.sqlite-journal").touch()

    def independent_copy(source_fd: int, destination: Path) -> bool:
        with os.fdopen(os.dup(source_fd), "rb") as source_stream:
            with destination.open("xb") as destination_stream:
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    destination_stream.write(chunk)
        return True

    monkeypatch.setattr(snapshot_module, "_clone_file_from_fd", independent_copy)
    destination = tmp_path / "second"
    with pytest.raises(GoodnotesError, match="Snapshot file verification failed"):
        create_snapshot(source, destination, previous_snapshot=first)

    assert not destination.exists()


@pytest.mark.parametrize(
    "invalidity",
    ["missing", "unsupported", "digest-mismatch", "changed-family", "cache-stat-damage"],
)
def test_projection_integrity_attestation_requires_all_fingerprints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, invalidity: str
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    if invalidity in {"missing", "unsupported", "digest-mismatch"}:
        data = marker_data(first)
        if invalidity == "missing":
            data.pop("projection_validation")
        elif invalidity == "unsupported":
            data["projection_validation"]["validation_id"] = "future-validation"
        else:
            data["projection_validation"]["sha256"] = "0" * 64
        marker = first / "goodnotes-snapshot.json"
        marker.chmod(0o644)
        marker.write_text(json.dumps(data), encoding="utf-8")
        marker.chmod(0o444)
    elif invalidity == "changed-family":
        (source / "Databases/projection.sqlite-journal").touch()
    else:
        cached = first / "Library/Databases/projection.sqlite"
        before = cached.stat()
        contents = cached.read_bytes()
        cached.chmod(0o644)
        cached.write_bytes(contents)
        os.utime(cached, ns=(before.st_atime_ns, before.st_mtime_ns))
        cached.chmod(0o444)
        assert snapshot_module._stat_key(cached.lstat()) != tuple(
            next(
                record["cache_stat_key"]
                for record in marker_data(first)["files"]
                if record["path"] == "Library/Databases/projection.sqlite"
            )
        )

    validations: list[Path] = []
    original_validate = snapshot_module._validate_projection_database

    def record_validation(path: Path) -> None:
        validations.append(path)
        original_validate(path)

    monkeypatch.setattr(snapshot_module, "_validate_projection_database", record_validation)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)

    assert validations == [second / "Library/Databases/projection.sqlite"]


def test_unchanged_projection_reuses_prior_integrity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    (source / "Attachments/page.pdf").write_bytes(b"changed attachment only")
    validations: list[Path] = []
    original_validate = snapshot_module._validate_projection_database

    def record_validation(path: Path) -> None:
        validations.append(path)
        original_validate(path)

    monkeypatch.setattr(snapshot_module, "_validate_projection_database", record_validation)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)

    assert validations == []
    assert marker_data(second)["projection_validation"]["validation_id"] == (
        snapshot_module._PROJECTION_VALIDATION_ID
    )
    assert marker_data(second)["capture_stats"]["source_captured_files"] == 1


def test_tampered_previous_projection_forces_copy_and_integrity_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    previous_db = first / "Library/Databases/projection.sqlite"
    previous_db.chmod(0o644)
    previous_db.write_bytes(b"tampered previous projection")
    previous_db.chmod(0o444)
    validations: list[Path] = []
    original_validate = snapshot_module._validate_projection_database

    def record_validation(path: Path) -> None:
        validations.append(path)
        original_validate(path)

    monkeypatch.setattr(snapshot_module, "_validate_projection_database", record_validation)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)

    assert validations == [second / "Library/Databases/projection.sqlite"]
    with open_database(second / "Library/Databases/projection.sqlite") as reader:
        assert reader.execute("SELECT name FROM item").fetchone()[0] == "first"


def test_incremental_byte_copy_fallback_checks_reused_digest_and_counts_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    original = create_snapshot(source, tmp_path / "first")
    changed = source / "Attachments" / "page.pdf"
    changed.write_bytes(b"new generated bytes")
    monkeypatch.setattr(snapshot_module, "_clone_file_from_fd", lambda *_args: False)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=original)
    stats = marker_data(second)["capture_stats"]
    assert stats["byte_copied_files"] == stats["total_files"] == 3
    assert stats["cloned_files"] == stats["cloned_logical_bytes"] == 0
    assert stats["source_bytes_hashed"] == changed.stat().st_size * 2
    assert stats["bytes_are_logical"] is True
    assert hash_tree(second / "Library") == hash_tree(source)


def test_incremental_snapshot_rejects_changed_cached_bytes_and_rehashes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    cached = first / "Library" / "Attachments" / "page.pdf"
    cached.chmod(0o644)
    cached.write_bytes(b"x" * cached.stat().st_size)
    cached.chmod(0o444)
    original_hash = snapshot_module._hash_file
    hashed = []

    def record_hash(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
        hashed.append(path)
        return original_hash(path)

    monkeypatch.setattr(snapshot_module, "_hash_file", record_hash)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    expected = source / "Attachments" / "page.pdf"
    assert [path for path in hashed if source in path.parents] == [expected]
    assert (second / "Library" / "Attachments" / "page.pdf").read_bytes() == expected.read_bytes()


def test_incremental_source_inventory_handles_additions_deletions_and_native_wal_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    native = add_native_store(source)
    first = create_snapshot(source, tmp_path / "first")
    (native / "000004.sst").unlink()
    (native / "000005.sst").write_bytes(b"new generated table")
    (native / "000002.log").write_bytes(b"changed generated wal")
    (native / "CURRENT").write_bytes(b"MANIFEST-000006\n")
    (native / "MANIFEST-000006").write_bytes(b"replacement generated manifest")
    (source / "Attachments" / "nested" / "image.png").unlink()
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    assert hash_tree(second / "Library") == hash_tree(source)
    stats = marker_data(second)["capture_stats"]
    assert stats["source_captured_files"] == 4
    assert not (second / "Library" / "Databases" / "notes_main.db" / "000004.sst").exists()
    assert (first / "Library" / "Databases" / "notes_main.db" / "000004.sst").is_file()


def test_incremental_sqlite_reuses_consolidated_copy_only_for_unchanged_source_family(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source", wal=True)
    database = source / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO item VALUES ('initial wal fixture')")
    writer.commit()
    first = create_snapshot(source, tmp_path / "first")
    (source / "Attachments" / "page.pdf").write_bytes(b"only attachment changed")
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    assert marker_data(second)["capture_stats"]["source_captured_files"] == 1
    assert not Path(f"{second / 'Library' / 'Databases' / 'projection.sqlite'}-wal").exists()
    writer.execute("INSERT INTO item VALUES ('new wal fixture')")
    writer.commit()
    third = create_snapshot(source, tmp_path / "third", previous_snapshot=second)
    with open_database(third / "Library" / "Databases" / "projection.sqlite") as reader:
        assert (
            reader.execute("SELECT name FROM item ORDER BY rowid DESC LIMIT 1").fetchone()[0]
            == "new wal fixture"
        )
    assert marker_data(third)["capture_stats"]["source_captured_files"] >= 2
    writer.close()


def test_incremental_cached_file_replaced_after_planning_retries_using_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    cached = first / "Library" / "Attachments" / "page.pdf"
    original_copy = snapshot_module._copy_snapshot_tree
    calls = 0

    def replace_then_copy(
        library: Path, destination: Path, entries: list[object]
    ) -> dict[str, int]:
        nonlocal calls
        calls += 1
        if calls == 1:
            cached.chmod(0o644)
            cached.write_bytes(b"tampered previous cache")
            cached.chmod(0o444)
        return original_copy(library, destination, entries)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_tree", replace_then_copy)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    assert calls == 2
    assert marker_data(second)["capture_stats"]["attempts"] == 2
    assert (second / "Library" / "Attachments" / "page.pdf").read_bytes() == (
        source / "Attachments" / "page.pdf"
    ).read_bytes()


def test_native_source_changes_during_every_attempt_clean_incomplete_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    native = add_native_store(source)
    first = create_snapshot(source, tmp_path / "first")
    wal = native / "000002.log"
    original_copy = snapshot_module._copy_snapshot_tree
    calls = 0

    def copy_then_append(library: Path, destination: Path, entries: list[object]) -> dict[str, int]:
        nonlocal calls
        result = original_copy(library, destination, entries)  # type: ignore[arg-type]
        calls += 1
        with wal.open("ab") as stream:
            stream.write(b"new generated wal transaction")
        return result

    monkeypatch.setattr(snapshot_module, "_copy_snapshot_tree", copy_then_append)
    destination = tmp_path / "second"
    with pytest.raises(GoodnotesError, match="kept changing"):
        create_snapshot(source, destination, previous_snapshot=first, max_attempts=2)
    assert calls == 2
    assert not destination.exists()
    assert (first / "Library" / "Databases" / "notes_main.db" / wal.name).read_bytes() == (
        b"generated wal bytes"
    )


def test_marker_records_frozen_file_metadata_and_old_marker_forces_full_capture(
    tmp_path: Path,
) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    data = marker_data(first)
    for record in data["files"]:
        assert tuple(record["cache_stat_key"]) == snapshot_module._stat_key(
            (first / record["path"]).lstat()
        )
    marker = first / "goodnotes-snapshot.json"
    marker.chmod(0o644)
    data.pop("source_inventory")
    marker.write_text(json.dumps(data), encoding="utf-8")
    marker.chmod(0o444)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    stats = marker_data(second)["capture_stats"]
    assert stats["cache_reused_files"] == 0
    assert stats["source_captured_files"] == stats["total_files"] == 3


def test_inconsistent_marker_digest_is_not_reused_for_unchanged_attachment(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    first = create_snapshot(source, tmp_path / "first")
    data = marker_data(first)
    record = next(
        entry for entry in data["files"] if entry["path"] == "Library/Attachments/page.pdf"
    )
    record["sha256"] = "0" * 64
    marker = first / "goodnotes-snapshot.json"
    marker.chmod(0o644)
    marker.write_text(json.dumps(data), encoding="utf-8")
    marker.chmod(0o444)
    second = create_snapshot(source, tmp_path / "second", previous_snapshot=first)
    stats = marker_data(second)["capture_stats"]
    assert stats["source_captured_files"] == 1
    fixed = next(
        entry
        for entry in marker_data(second)["files"]
        if entry["path"] == "Library/Attachments/page.pdf"
    )
    assert (
        fixed["sha256"]
        == hashlib.sha256((source / "Attachments" / "page.pdf").read_bytes()).hexdigest()
    )


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
    metadata_before = {
        path.relative_to(source_library): snapshot_module._stat_key(path.lstat())
        for path in source_library.rglob("*")
    }
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
    assert {
        path.relative_to(source_library): snapshot_module._stat_key(path.lstat())
        for path in source_library.rglob("*")
    } == metadata_before
    assert connection_targets
    assert all(str(snapshot) in target for target in connection_targets)
    assert all(source_db.as_posix() not in target for target in connection_targets)
    assert any("mode=rw" in target for target in connection_targets)
    assert all(".consolidated" not in target for target in connection_targets)
    source_connection.close()


def test_copied_wal_checkpoint_rebuilds_shm_before_opening_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wal_library: Path
) -> None:
    source_library = wal_library
    original_consolidate = snapshot_module._consolidate_copied_databases
    original_connect = snapshot_module.sqlite3.connect
    checked_staging = False

    def replace_staging_shm(library: Path) -> None:
        (library / "Databases" / "projection.sqlite-shm").write_bytes(b"stale copied index")
        original_consolidate(library)

    def check_connection(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        nonlocal checked_staging
        if "mode=rw" in str(database):
            checked_staging = True
            assert not (tmp_path / "snapshot/Library/Databases/projection.sqlite-shm").exists()
            assert (source_library / "Databases/projection.sqlite-shm").is_file()
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module, "_consolidate_copied_databases", replace_staging_shm)
    monkeypatch.setattr(snapshot_module.sqlite3, "connect", check_connection)
    source_before = hash_tree(source_library)
    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    assert checked_staging
    assert hash_tree(source_library) == source_before
    with open_database(snapshot / "Library/Databases/projection.sqlite") as connection:
        assert connection.execute("SELECT name FROM item").fetchone()[0] == "first"


def test_copied_wal_checkpoint_accepts_read_only_source_files(
    tmp_path: Path, wal_library: Path
) -> None:
    for path in (wal_library / "Databases").iterdir():
        if path.is_file():
            path.chmod(0o400)
    source_before = hash_tree(wal_library)
    metadata_before = {
        path.relative_to(wal_library): snapshot_module._stat_key(path.lstat())
        for path in wal_library.rglob("*")
    }
    snapshot = create_snapshot(wal_library, tmp_path / "snapshot")
    assert hash_tree(wal_library) == source_before
    assert {
        path.relative_to(wal_library): snapshot_module._stat_key(path.lstat())
        for path in wal_library.rglob("*")
    } == metadata_before
    copied_database = snapshot / "Library/Databases/projection.sqlite"
    assert copied_database.stat().st_mode & 0o222 == 0
    with open_database(copied_database) as connection:
        assert connection.execute("SELECT name FROM item").fetchone()[0] == "first"


@pytest.mark.parametrize(
    ("failure", "message"),
    [
        ("busy", "checkpoint did not complete"),
        ("incomplete", "checkpoint did not complete"),
        ("not-wal", "checkpoint did not complete"),
        ("mode", "remove copied SQLite WAL mode"),
        ("sqlite-error", "Could not checkpoint copied SQLite database"),
        ("leftover", "nonempty copied sidecar"),
    ],
)
def test_copied_wal_checkpoint_failure_cleans_unpublished_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    wal_library: Path,
    failure: str,
    message: str,
) -> None:
    source_library = wal_library
    source_before = hash_tree(source_library)
    original_connect = snapshot_module.sqlite3.connect

    class FaultyCheckpoint(sqlite3.Connection):
        def execute(self, sql: str, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
            if sql == "PRAGMA main.wal_checkpoint(TRUNCATE)":
                if failure == "sqlite-error":
                    raise sqlite3.OperationalError("generated checkpoint error")
                fault = {"busy": "1,3,2", "incomplete": "0,3,2", "not-wal": "0,-1,-1"}
                if failure in fault:
                    return super().execute(f"SELECT {fault[failure]}")
            if sql == "PRAGMA main.journal_mode = DELETE" and failure == "mode":
                return super().execute("SELECT 'wal'")
            return super().execute(sql, *args, **kwargs)

        def close(self) -> None:
            super().close()
            if failure == "leftover":
                (tmp_path / "snapshot/Library/Databases/projection.sqlite-wal").write_bytes(
                    b"unexpected retained WAL"
                )

    def faulty_connection(database: str, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        if "mode=rw" in str(database):
            kwargs["factory"] = FaultyCheckpoint
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(snapshot_module.sqlite3, "connect", faulty_connection)
    destination = tmp_path / "snapshot"
    with pytest.raises(GoodnotesError, match=message):
        create_snapshot(source_library, destination)
    assert not destination.exists()
    assert hash_tree(source_library) == source_before


def test_copied_wal_with_rollback_journal_keeps_backup_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, wal_library: Path
) -> None:
    source_library = wal_library
    (source_library / "Databases/projection.sqlite-journal").touch()
    source_before = hash_tree(source_library)

    def no_checkpoint(database: Path) -> None:
        raise AssertionError("A copied rollback journal must use the backup path")

    monkeypatch.setattr(snapshot_module, "_checkpoint_copied_database", no_checkpoint)
    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    with open_database(snapshot / "Library/Databases/projection.sqlite") as connection:
        assert connection.execute("SELECT name FROM item").fetchone()[0] == "first"
        assert connection.execute("PRAGMA main.journal_mode").fetchone()[0] == "delete"
    assert hash_tree(source_library) == source_before


def test_genuine_hot_rollback_journal_recovers_only_the_staging_copy(
    tmp_path: Path,
) -> None:
    source_library = make_library(tmp_path / "source")
    database = source_library / "Databases/projection.sqlite"
    writer_code = (
        "import os, sqlite3, sys; "
        "connection = sqlite3.connect(sys.argv[1]); "
        "connection.execute('BEGIN EXCLUSIVE'); "
        "connection.execute(\"UPDATE item SET name='uncommitted'\"); "
        "os._exit(0)"
    )
    subprocess.run([sys.executable, "-c", writer_code, str(database)], check=True)
    journal = Path(f"{database}-journal")
    assert journal.is_file() and journal.stat().st_size > 0
    source_before = hash_tree(source_library)

    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    copied_database = snapshot / "Library/Databases/projection.sqlite"

    with open_database(copied_database) as reader:
        assert reader.execute("SELECT name FROM item").fetchone()[0] == "first"
        assert reader.execute("PRAGMA main.journal_mode").fetchone()[0] == "delete"
    assert not Path(f"{copied_database}-journal").exists()
    assert hash_tree(source_library) == source_before


def test_copied_rollback_recovery_does_not_fall_back_after_database_corruption(
    tmp_path: Path,
) -> None:
    source_library = make_library(tmp_path / "source")
    database = source_library / "Databases/projection.sqlite"
    database.write_bytes(b"generated corrupt database bytes")
    Path(f"{database}-journal").write_bytes(b"generated nonempty journal bytes")
    destination = tmp_path / "snapshot"

    with pytest.raises(GoodnotesError, match="Could not recover copied SQLite rollback journal"):
        create_snapshot(source_library, destination)

    assert not destination.exists()


@pytest.mark.parametrize("stray_wal", [b"", b"not a valid WAL"])
def test_delete_mode_database_with_stray_wal_keeps_backup_compatibility(
    tmp_path: Path, stray_wal: bytes
) -> None:
    source_library = make_library(tmp_path / "source")
    (source_library / "Databases/projection.sqlite-wal").write_bytes(stray_wal)
    source_before = hash_tree(source_library)
    metadata_before = {
        path.relative_to(source_library): snapshot_module._stat_key(path.lstat())
        for path in source_library.rglob("*")
    }
    snapshot = create_snapshot(source_library, tmp_path / "snapshot")
    copied_database = snapshot / "Library/Databases/projection.sqlite"
    assert not Path(f"{copied_database}-wal").exists()
    with open_database(copied_database) as connection:
        assert connection.execute("SELECT name FROM item").fetchone()[0] == "first"
    assert hash_tree(source_library) == source_before
    assert {
        path.relative_to(source_library): snapshot_module._stat_key(path.lstat())
        for path in source_library.rglob("*")
    } == metadata_before


@pytest.mark.parametrize("tail", ["torn", "reset"])
def test_copied_wal_preserves_commits_with_unused_tail(tmp_path: Path, tail: str) -> None:
    source_library = make_library(tmp_path / "source")
    source_database = source_library / "Databases/projection.sqlite"
    writer = sqlite3.connect(source_database)
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE payload (body BLOB)")
        writer.execute("INSERT INTO payload VALUES (zeroblob(100000))")
        writer.commit()
        wal = Path(f"{source_database}-wal")
        if tail == "reset":
            previous_size = wal.stat().st_size
            assert writer.execute("PRAGMA wal_checkpoint(RESTART)").fetchone()[0] == 0
        writer.execute("INSERT INTO item VALUES ('committed before unused tail')")
        writer.commit()
        if tail == "reset":
            assert wal.stat().st_size == previous_size
        else:
            with wal.open("ab") as stream:
                stream.write(b"incomplete uncommitted frame")
        source_before = hash_tree(source_library)
        snapshot = create_snapshot(source_library, tmp_path / "snapshot")
        with open_database(snapshot / "Library/Databases/projection.sqlite") as connection:
            names = [row[0] for row in connection.execute("SELECT name FROM item ORDER BY rowid")]
            assert names == [
                "first",
                "committed before unused tail",
            ]
            assert connection.execute("SELECT length(body) FROM payload").fetchone()[0] == 100000
        assert hash_tree(source_library) == source_before
    finally:
        writer.close()


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


def test_fts_wal_checkpoint_does_not_require_custom_virtual_table_tokenizer(
    tmp_path: Path,
) -> None:
    source_library = make_library(tmp_path / "source")
    fts_path = source_library / "Databases/fts.sqlite"
    with closing(sqlite3.connect(fts_path)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("CREATE TABLE text_cache (body TEXT)")
        writer.execute("INSERT INTO text_cache VALUES ('ordinary stored row')")
        writer.execute("PRAGMA writable_schema=ON")
        writer.execute(
            "INSERT INTO sqlite_master(type,name,tbl_name,rootpage,sql) "
            "VALUES ('table','custom_fts','custom_fts',0,?)",
            (
                "CREATE VIRTUAL TABLE custom_fts USING "
                "fts5(body, tokenize='unavailable_fixture_tokenizer')",
            ),
        )
        writer.commit()
        source_before = hash_tree(source_library)
        snapshot = create_snapshot(source_library, tmp_path / "snapshot")
        assert hash_tree(source_library) == source_before
        with open_database(snapshot / "Library/Databases/fts.sqlite") as reader:
            assert reader.execute("SELECT body FROM text_cache").fetchone()[0] == (
                "ordinary stored row"
            )
            with pytest.raises(sqlite3.Error, match="tokenizer|vtable|module"):
                reader.execute("SELECT * FROM custom_fts").fetchall()


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

    def copy_then_change(source: Path, destination: Path, entries: list[object]) -> dict[str, int]:
        nonlocal calls
        metrics = original_copy(source, destination, entries)  # type: ignore[arg-type]
        calls += 1
        if calls == 1:
            attachment.write_bytes(b"changed during first copy")
        return metrics

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

    def copy_then_write(source: Path, destination: Path, expected: object) -> bool:
        nonlocal copy_mutations
        cloned = original_copy_file(source, destination, expected)  # type: ignore[arg-type]
        if source.name == "projection.sqlite":
            copy_mutations += 1
            writer.execute("INSERT INTO item VALUES (?)", (f"during copy {copy_mutations}",))
            writer.commit()
        return cloned

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
