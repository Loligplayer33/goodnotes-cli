from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import multiprocessing
import os
import sqlite3
import stat
from collections.abc import Callable, Generator
from contextlib import contextmanager
from multiprocessing.synchronize import Event as SyncEvent
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import goodnotes_cli.cache as cache_module
import goodnotes_cli.storage.cache.lifecycle as cache_lifecycle
import goodnotes_cli.storage.cache.maintenance as cache_maintenance
import goodnotes_cli.storage.cache.state as cache_state
import goodnotes_cli.storage.inventory as cache_inventory
import goodnotes_cli.storage.locks as cache_locks
from goodnotes_cli.config import Settings
from goodnotes_cli.errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    StaleSnapshotError,
)
from goodnotes_cli.safety import Sandbox
from goodnotes_cli.snapshot import _InventoryChanged
from tests.support.library import _create_snapshot, make_library


def active_pointer(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "active.json").read_text(encoding="utf-8"))


def generated_snapshots(data_dir: Path) -> list[Path]:
    return sorted(path for path in (data_dir / "snapshots").iterdir() if path.is_dir())


def _assert_cache_access_error(error: GoodnotesError, *, code: str, path: Path) -> dict[str, Any]:
    assert getattr(error, "code", None) == code
    details = getattr(error, "details", None)
    assert isinstance(details, dict)
    assert {"operation", "path", "remedy"} <= details.keys()
    assert details["path"] == str(path)
    assert isinstance(details["operation"], str)
    assert isinstance(details["remedy"], str)
    return details


def _expected_permission_path(action: Callable[[], object]) -> str:
    try:
        action()
    except PermissionError as exc:
        assert exc.filename is not None
        return os.fsdecode(exc.filename)
    pytest.fail("expected the mode-000 parent to deny filesystem access")


def test_refresh_skips_capture_when_source_inventory_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)

    def fail_if_captured(*args: object, **kwargs: object) -> Path:
        raise AssertionError("unchanged refresh should not capture a snapshot")

    monkeypatch.setattr(cache_lifecycle, "create_snapshot", fail_if_captured)
    second = cache_module.refresh_cache(source, data_dir)

    assert first["status"] == "ready"
    assert second["status"] == "unchanged"
    assert second["snapshot"] == first["snapshot"]
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["captured_at"] == first["captured_at"]
    stats = second["capture_stats"]
    assert isinstance(stats, dict)
    assert stats["source_captured_files"] == stats["source_captured_bytes"] == 0
    assert stats["cache_reused_files"] == stats["cache_reused_bytes"] == 0
    assert stats["source_bytes_hashed"] == stats["byte_copied_bytes"] == 0


@pytest.mark.parametrize("force", [False, True])
def test_nested_stale_marker_cannot_abort_refresh_after_publication(
    tmp_path: Path, force: bool
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    stale = data_dir / "snapshots" / "20000101T000000Z-00000000"
    stale.mkdir()
    marker = stale / "goodnotes-snapshot.json"
    nested = b"[" * 10_000 + b"0" + b"]" * 10_000
    marker.write_bytes(nested)

    second = cache_module.refresh_cache(source, data_dir, force=force)

    assert second["status"] == ("ready" if force else "unchanged")
    assert active_pointer(data_dir)["snapshot_id"] == second["snapshot_id"]
    assert Path(str(second["snapshot"])).is_dir()
    if not force:
        assert second["snapshot_id"] == first["snapshot_id"]
    # Invalid metadata cannot establish ownership for automatic deletion.
    assert marker.read_bytes() == nested


@pytest.mark.parametrize("record", ["snapshot", "pointer"])
def test_nested_cache_metadata_has_a_classified_failure(tmp_path: Path, record: str) -> None:
    snapshot = _create_snapshot(tmp_path / "snapshot").root
    data_dir = tmp_path / "cache"
    data_dir.mkdir()
    (data_dir / "snapshots").mkdir()
    path = (
        snapshot / "goodnotes-snapshot.json" if record == "snapshot" else data_dir / "active.json"
    )
    path.write_bytes(b"[" * 10_000 + b"0" + b"]" * 10_000)

    if record == "snapshot":
        with pytest.raises(GoodnotesError, match="marker is unreadable"):
            Sandbox.load(snapshot)
    else:
        with pytest.raises(CacheAccessError) as error:
            Settings(data_dir=data_dir).load_sandbox()
        assert error.value.code == "cache_invalid"


def test_missing_source_library_root_is_a_structured_input_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing-library"

    with pytest.raises(FilesystemAccessError) as captured:
        cache_inventory._input_signature(missing)

    error = captured.value
    assert error.code == "source_unavailable"
    assert error.details["operation"] == "inspect_source_library"
    assert error.details["path"] == str(missing)


def test_disappearing_child_directory_remains_a_transient_inventory_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    database_dir = source / "Databases"
    original_source_lstat = cache_inventory._source_lstat

    def disappear_child(path: Path) -> os.stat_result:
        if path == database_dir:
            raise _InventoryChanged(f"Generated child disappeared: {path}")
        return original_source_lstat(path)

    monkeypatch.setattr(cache_inventory, "_source_lstat", disappear_child)
    with pytest.raises(_InventoryChanged, match="Generated child disappeared"):
        cache_inventory._input_signature(source)


def test_changed_source_skips_unnecessary_active_cache_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    (source / "Attachments/page.pdf").write_bytes(b"changed source attachment")
    active_library = Path(str(first["snapshot"])) / "Library"
    original_signature = cache_inventory._input_signature
    inspected: list[Path] = []

    def record_signature(path: Path, *, include_root: bool = False) -> str:
        inspected.append(path)
        return original_signature(path, include_root=include_root)

    monkeypatch.setattr(cache_state, "_input_signature", record_signature)
    cache_module.refresh_cache(source, data_dir)

    assert active_library not in inspected


def test_native_change_refreshes_only_changed_files_and_prunes_previous_generation(
    tmp_path: Path,
) -> None:
    source = make_library(tmp_path / "source")
    native = source / "Databases" / "notes_main.db"
    native.mkdir()
    (native / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (native / "MANIFEST-000001").write_bytes(b"generated manifest")
    wal = native / "000002.log"
    wal.write_bytes(b"initial generated wal")
    (native / "000003.sst").write_bytes(b"generated large table" * 100_000)
    cache = tmp_path / "cache"
    first = cache_module.refresh_cache(source, cache)
    old = Path(str(first["snapshot"]))
    wal.write_bytes(b"new generated wal")
    result = cache_module.refresh_cache(source, cache)
    stats = result["capture_stats"]
    assert result["status"] == "ready"
    assert isinstance(stats, dict)
    assert stats["source_captured_files"] == 1
    assert stats["source_captured_bytes"] == wal.stat().st_size
    assert stats["cache_reused_files"] == 6
    assert stats["cache_reused_bytes"] > 2_000_000
    assert stats["native_store_present"] is True
    assert len(generated_snapshots(cache)) == 1
    assert not old.exists()
    copied = Path(str(result["snapshot"])) / "Library" / "Databases" / "notes_main.db"
    assert (copied / wal.name).read_bytes() == wal.read_bytes()
    unchanged = cache_module.refresh_cache(source, cache)
    assert unchanged["status"] == "unchanged"
    unchanged_stats = unchanged["capture_stats"]
    assert isinstance(unchanged_stats, dict)
    assert unchanged_stats["source_bytes_hashed"] == 0


def test_force_rehashes_native_store_and_does_not_reuse_cached_files(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    native = source / "Databases" / "notes_main.db"
    native.mkdir()
    (native / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (native / "MANIFEST-000001").write_bytes(b"generated manifest")
    (native / "000002.log").write_bytes(b"generated wal")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    forced = cache_module.refresh_cache(source, data_dir, force=True)
    stats = forced["capture_stats"]
    assert isinstance(stats, dict)
    assert stats["cache_reused_files"] == stats["cache_reused_bytes"] == 0
    assert stats["source_captured_files"] == stats["total_files"] == 6
    assert stats["source_bytes_hashed"] >= stats["source_captured_bytes"]
    assert len(generated_snapshots(data_dir)) == 1


def test_native_inventory_permission_denial_is_actionable_and_keeps_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    native = source / "Databases" / "notes_main.db"
    native.mkdir()
    (native / "000001.log").write_bytes(b"generated wal")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    pointer_before = (data_dir / "active.json").read_bytes()
    original_scandir = cache_inventory.os.scandir

    def deny_native(path: Any) -> Any:
        if Path(path) == native:
            raise PermissionError(errno.EACCES, "generated denial", str(native))
        return original_scandir(path)

    monkeypatch.setattr(cache_inventory.os, "scandir", deny_native)
    with pytest.raises(GoodnotesError) as error:
        cache_module.refresh_cache(source, data_dir)
    assert getattr(error.value, "code", None) == "source_permission_denied"
    details = getattr(error.value, "details", {})
    assert details["path"] == str(native)
    assert "remedy" in details
    assert (data_dir / "active.json").read_bytes() == pointer_before


@pytest.mark.parametrize(
    ("action", "expected_operation"),
    [
        ("read", "resolve_cache_directory"),
        ("sync", "prepare_cache_directory"),
    ],
)
def test_unsearchable_cache_parent_returns_structured_permission_error(
    tmp_path: Path, action: str, expected_operation: str
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod-based parent access checks require a non-root process")

    source = make_library(tmp_path / "source")
    inaccessible_parent = tmp_path / "inaccessible-parent"
    inaccessible_parent.mkdir()
    data_dir = inaccessible_parent / "cache"
    original_mode = stat.S_IMODE(inaccessible_parent.stat().st_mode)
    source_before = {
        path.relative_to(source): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in source.rglob("*")
        if path.is_file()
    }

    inaccessible_parent.chmod(0)
    try:
        expected_path = _expected_permission_path(
            lambda: data_dir.stat() if action == "read" else os.mkdir(data_dir, 0o700)
        )
        with pytest.raises(GoodnotesError) as error:
            if action == "read":
                with cache_module.read_view(Settings(data_dir=data_dir)):
                    pass
            else:
                cache_module.refresh_cache(source, data_dir)

        details = _assert_cache_access_error(
            error.value, code="cache_permission_denied", path=Path(expected_path)
        )
        assert details["operation"] == expected_operation
        if action == "read":
            assert "read/search access" in details["remedy"].casefold()
        else:
            assert "sync" in details["remedy"].casefold()
            assert "write/search access" in details["remedy"].casefold()
    finally:
        inaccessible_parent.chmod(original_mode)

    source_after = {
        path.relative_to(source): (
            path.stat().st_size,
            path.stat().st_mtime_ns,
            path.stat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest(),
        )
        for path in source.rglob("*")
        if path.is_file()
    }
    assert source_after == source_before
    assert not data_dir.exists()


def test_same_size_cached_attachment_replacement_triggers_refresh(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    cached_attachment = Path(str(first["snapshot"])) / "Library" / "Attachments" / "page.pdf"
    original = cached_attachment.read_bytes()
    replacement = b"x" * len(original)
    cached_attachment.chmod(0o644)
    cached_attachment.write_bytes(replacement)
    cached_attachment.chmod(0o444)

    refreshed = cache_module.refresh_cache(source, data_dir)

    assert len(replacement) == len(original)
    assert refreshed["status"] == "ready"
    assert refreshed["snapshot_id"] != first["snapshot_id"]
    refreshed_attachment = Path(str(refreshed["snapshot"])) / "Library" / "Attachments" / "page.pdf"
    assert refreshed_attachment.read_bytes() == original


def test_wal_change_after_initial_stat_is_detected_before_unchanged_reuse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    database = source / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO item VALUES ('initial wal row')")
    writer.commit()
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    active_before = (data_dir / "active.json").read_bytes()
    wal_path = Path(f"{database}-wal")
    original_lstat = cache_inventory._lstat
    committed = False

    def stat_then_commit(path: Path) -> os.stat_result:
        nonlocal committed
        result = original_lstat(path)
        if path == wal_path and not committed:
            committed = True
            writer.execute("INSERT INTO item VALUES ('committed after first stat')")
            writer.commit()
        return result

    monkeypatch.setattr(cache_inventory, "_lstat", stat_then_commit)
    with pytest.raises(GoodnotesError, match="changed while it was fingerprinted"):
        cache_module.refresh_cache(source, data_dir)
    writer.close()

    assert committed
    assert (data_dir / "active.json").read_bytes() == active_before
    assert Path(str(first["snapshot"])).is_dir()


def test_source_change_during_refresh_is_reported_with_snapshot_time(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    attachment = source / "Attachments" / "page.pdf"
    before_capture = attachment.read_bytes()
    original_create = cache_lifecycle.create_snapshot

    def capture_then_advance(
        source_library: str | Path,
        destination: str | Path,
        *,
        max_attempts: int = 3,
        previous_snapshot: Path | None = None,
    ) -> Path:
        snapshot = original_create(
            source_library,
            destination,
            max_attempts=max_attempts,
            previous_snapshot=previous_snapshot,
        )
        attachment.write_bytes(b"source advanced after capture")
        return snapshot

    monkeypatch.setattr(cache_lifecycle, "create_snapshot", capture_then_advance)
    result = cache_module.refresh_cache(source, tmp_path / "cache")
    root = Path(str(result["snapshot"]))
    pointer = active_pointer(tmp_path / "cache")
    marker = json.loads((root / "goodnotes-snapshot.json").read_text(encoding="utf-8"))

    assert result["status"] == "ready"
    assert result["source_changed_during_refresh"] is True
    assert result["captured_at"] == marker["created_at_utc"]
    warnings = result["warnings"]
    assert isinstance(warnings, list)
    assert any("as of captured_at" in str(warning) for warning in warnings)
    assert pointer["source_changed_during_refresh"] is True
    assert "source_signature" not in pointer
    assert (root / "Library" / "Attachments" / "page.pdf").read_bytes() == before_capture


def test_mid_final_inventory_mutation_publishes_the_validated_stale_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    attachment = source / "Attachments/page.pdf"
    captured_bytes = attachment.read_bytes()
    data_dir = tmp_path / "cache"
    original_input_signature = cache_inventory._input_signature
    original_lstat = cache_inventory._lstat
    source_scans = 0
    final_scan = False
    mutated = False

    def mark_final_scan(path: Path, *, include_root: bool = False) -> str:
        nonlocal source_scans, final_scan
        if path == source and not include_root:
            source_scans += 1
            final_scan = source_scans == 2
        return original_input_signature(path, include_root=include_root)

    def mutate_after_inventory_stat(path: Path) -> os.stat_result:
        nonlocal final_scan, mutated
        info = original_lstat(path)
        if final_scan and path == attachment and not mutated:
            attachment.write_bytes(b"edited during final source scan")
            mutated = True
            final_scan = False
        return info

    monkeypatch.setattr(cache_inventory, "_input_signature", mark_final_scan)
    monkeypatch.setattr(cache_inventory, "_lstat", mutate_after_inventory_stat)
    result = cache_module.refresh_cache(source, data_dir)
    root = Path(str(result["snapshot"]))
    pointer = active_pointer(data_dir)

    assert mutated
    assert result["source_changed_during_refresh"] is True
    assert pointer["source_changed_during_refresh"] is True
    assert "source_signature" not in pointer
    assert (root / "Library/Attachments/page.pdf").read_bytes() == captured_bytes
    assert Sandbox.load(root).root == root


def test_final_inventory_permission_error_preserves_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_pointer = (data_dir / "active.json").read_bytes()
    old_snapshot = Path(str(first["snapshot"]))
    (source / "Attachments/page.pdf").write_bytes(b"changed before denied scan")
    original_signature = cache_lifecycle._source_input_signature
    signatures = 0

    def deny_final_scan(path: Path) -> str:
        nonlocal signatures
        signatures += 1
        if signatures == 2:
            raise FilesystemAccessError(
                "source_permission_denied",
                "Generated final inventory denial",
                operation="inventory_source_library",
                path=path / "Attachments",
                remedy="Generated read remedy.",
            )
        return original_signature(path)

    monkeypatch.setattr(cache_lifecycle, "_source_input_signature", deny_final_scan)
    with pytest.raises(FilesystemAccessError) as error:
        cache_module.refresh_cache(source, data_dir)

    assert error.value.code == "source_permission_denied"
    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert old_snapshot.is_dir()
    assert len(generated_snapshots(data_dir)) == 1


def test_final_inventory_unsafe_state_is_not_treated_as_a_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_pointer = (data_dir / "active.json").read_bytes()
    old_snapshot = Path(str(first["snapshot"]))
    attachment = source / "Attachments/page.pdf"
    attachment.write_bytes(b"change that starts refresh")
    original_signature = cache_lifecycle._source_input_signature
    signatures = 0

    def install_unsafe_source(path: Path) -> str:
        nonlocal signatures
        signatures += 1
        if signatures == 2:
            attachment.unlink()
            attachment.symlink_to(tmp_path / "external-target")
        return original_signature(path)

    monkeypatch.setattr(cache_lifecycle, "_source_input_signature", install_unsafe_source)
    with pytest.raises(GoodnotesError, match="Symlinks are not allowed"):
        cache_module.refresh_cache(source, data_dir)

    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert old_snapshot.is_dir()
    assert len(generated_snapshots(data_dir)) == 1


def test_attachment_and_wal_metadata_changes_trigger_refresh(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    database = source / "Databases" / "projection.sqlite"
    writer = sqlite3.connect(database)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO item VALUES ('wal one')")
    writer.commit()

    first = cache_module.refresh_cache(source, data_dir)
    attachment = source / "Attachments" / "page.pdf"
    attachment.write_bytes(b"updated generated attachment")
    second = cache_module.refresh_cache(source, data_dir)
    writer.execute("INSERT INTO item VALUES ('wal two')")
    writer.commit()
    wal_path = Path(f"{database}-wal")
    assert wal_path.is_file()
    third = cache_module.refresh_cache(source, data_dir)
    writer.close()

    assert first["snapshot_id"] != second["snapshot_id"]
    assert second["snapshot_id"] != third["snapshot_id"]
    assert all(result["status"] == "ready" for result in (first, second, third))


def test_refresh_retains_only_active_generated_snapshot_and_removes_registration(
    tmp_path: Path,
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_root = Path(str(first["snapshot"]))
    old_key = hashlib.sha256(str(old_root.resolve()).encode()).hexdigest()[:24]
    old_sidecar = old_root.parent / ".goodnotes-cli" / old_key
    old_sidecar.mkdir(parents=True)
    (old_sidecar / "registration.json").write_text("{}", encoding="utf-8")

    (source / "Attachments" / "page.pdf").write_bytes(b"new generated attachment")
    second = cache_module.refresh_cache(source, data_dir)

    snapshots = generated_snapshots(data_dir)
    assert snapshots == [Path(str(second["snapshot"]))]
    assert not old_root.exists()
    assert not old_sidecar.exists()


def test_interrupted_prune_leaves_staging_tombstone_for_next_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_root = Path(str(first["snapshot"]))
    (source / "Attachments" / "page.pdf").write_bytes(b"new attachment before prune")
    original_remove = cache_maintenance._safe_remove_tree

    def interrupt_tombstone_cleanup(path: Path) -> None:
        if path.parent.name == "snapshots" and path.name.startswith(".staging-"):
            path.chmod(0o700)
            (path / "goodnotes-snapshot.json").unlink()
            raise GoodnotesError("simulated cleanup interruption")
        original_remove(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(cache_maintenance, "_safe_remove_tree", interrupt_tombstone_cleanup)
        refreshed = cache_module.refresh_cache(source, data_dir)

    tombstones = [
        path for path in (data_dir / "snapshots").iterdir() if path.name.startswith(".staging-")
    ]
    assert refreshed["status"] == "ready"
    assert refreshed["warnings"]
    assert active_pointer(data_dir)["snapshot"] == refreshed["snapshot"]
    assert not old_root.exists()
    assert len(tombstones) == 1
    assert not (tombstones[0] / "goodnotes-snapshot.json").exists()

    unchanged = cache_module.refresh_cache(source, data_dir)

    assert unchanged["status"] == "unchanged"
    assert not any(path.name.startswith(".staging-") for path in (data_dir / "snapshots").iterdir())


def test_force_refreshes_an_unchanged_source(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)

    forced = cache_module.refresh_cache(source, data_dir, force=True)

    assert forced["status"] == "ready"
    assert forced["snapshot_id"] != first["snapshot_id"]
    assert generated_snapshots(data_dir) == [Path(str(forced["snapshot"]))]


def test_failed_force_refresh_preserves_the_active_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_pointer = (data_dir / "active.json").read_bytes()

    def fail_capture(source_library: Path, destination: Path, *, max_attempts: int = 3) -> Path:
        del source_library, destination, max_attempts
        raise GoodnotesError("generated capture failure")

    monkeypatch.setattr(cache_lifecycle, "create_snapshot", fail_capture)
    with pytest.raises(GoodnotesError, match="capture failure"):
        cache_module.refresh_cache(source, data_dir, force=True)

    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert Path(str(first["snapshot"])).is_dir()


def _refresh_in_process(
    source: str,
    data_dir: str,
    start: SyncEvent,
    output: str,
) -> None:
    try:
        if not start.wait(15):
            raise RuntimeError("test start barrier timed out")
        result = cache_module.refresh_cache(Path(source), Path(data_dir))
        Path(output).write_text(json.dumps(result), encoding="utf-8")
    except BaseException as exc:
        Path(output).write_text(json.dumps({"error": repr(exc)}), encoding="utf-8")


def _refresh_waiting_to_publish(
    source: str,
    data_dir: str,
    captured: SyncEvent,
    publish_attempt: SyncEvent,
    complete: SyncEvent,
    output: str,
) -> None:
    original_create = cache_lifecycle.create_snapshot

    def capture_then_signal(
        source_library: str | Path,
        destination: str | Path,
        *,
        max_attempts: int = 3,
        previous_snapshot: Path | None = None,
    ) -> Path:
        result = original_create(
            source_library,
            destination,
            max_attempts=max_attempts,
            previous_snapshot=previous_snapshot,
        )
        captured.set()
        return result

    original_lock = cache_lifecycle._file_lock

    @contextmanager
    def observe_exclusive_view_lock(
        path: Path, *, exclusive: bool, create: bool = True
    ) -> Generator[None, None, None]:
        if path.name == cache_locks._VIEW_LOCK and exclusive:
            publish_attempt.set()
        with original_lock(path, exclusive=exclusive, create=create):
            yield

    vars(cache_lifecycle)["create_snapshot"] = capture_then_signal
    cache_lifecycle._file_lock = observe_exclusive_view_lock
    try:
        result = cache_module.refresh_cache(Path(source), Path(data_dir))
        Path(output).write_text(json.dumps(result), encoding="utf-8")
    except BaseException as exc:
        Path(output).write_text(json.dumps({"error": repr(exc)}), encoding="utf-8")
    finally:
        complete.set()


def _read_view_paused_with_missing_pointer(
    data_dir: str,
    old_snapshot: str,
    load_started: SyncEvent,
    allow_load: SyncEvent,
    yielded: SyncEvent,
    release_reader: SyncEvent,
    output: str,
) -> None:
    def paused_load(settings: Settings) -> Sandbox:
        del settings
        load_started.set()
        if not allow_load.wait(15):
            raise RuntimeError("read-view load barrier timed out")
        return Sandbox.load(Path(old_snapshot))

    try:
        with patch.object(Settings, "load_sandbox", paused_load):
            with cache_module.read_view(Settings(data_dir=Path(data_dir))) as sandbox:
                yielded.set()
                if not release_reader.wait(15):
                    raise RuntimeError("read-view release barrier timed out")
                Path(output).write_text(str(sandbox.root), encoding="utf-8")
    except BaseException as exc:
        Path(output).write_text(json.dumps({"error": repr(exc)}), encoding="utf-8")


def _read_explicit_view_paused(
    sandbox_path: str,
    yielded: SyncEvent,
    release_reader: SyncEvent,
    output: str,
) -> None:
    try:
        with cache_module.read_view(Settings(sandbox=Path(sandbox_path))) as sandbox:
            yielded.set()
            if not release_reader.wait(15):
                raise RuntimeError("explicit read-view release barrier timed out")
            Path(output).write_text(str(sandbox.root), encoding="utf-8")
    except BaseException as exc:
        Path(output).write_text(json.dumps({"error": repr(exc)}), encoding="utf-8")


def _read_view_in_process(data_dir: str, output: str) -> None:
    try:
        with cache_module.read_view(Settings(data_dir=Path(data_dir))) as sandbox:
            Path(output).write_text(str(sandbox.root), encoding="utf-8")
    except GoodnotesError as exc:
        Path(output).write_text(
            json.dumps(
                {
                    "error": type(exc).__name__,
                    "code": getattr(exc, "code", None),
                    "details": getattr(exc, "details", None),
                }
            ),
            encoding="utf-8",
        )
    except BaseException as exc:
        Path(output).write_text(json.dumps({"error": type(exc).__name__}), encoding="utf-8")


def test_read_view_opens_existing_lock_readonly_and_releases_it_on_interruption(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    view_lock = data_dir / cache_locks._VIEW_LOCK
    real_open = os.open
    opened: list[tuple[int, int]] = []

    def record_open(path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        descriptor = real_open(path, flags, mode)
        if Path(os.fsdecode(path)) == view_lock:
            opened.append((descriptor, flags))
        return descriptor

    monkeypatch.setattr(cache_inventory.os, "open", record_open)
    with pytest.raises(KeyboardInterrupt):
        with cache_module.read_view(Settings(data_dir=data_dir)):
            raise KeyboardInterrupt

    assert len(opened) == 1
    descriptor, flags = opened[0]
    assert flags & os.O_ACCMODE == os.O_RDONLY
    assert flags & os.O_CREAT == 0
    if hasattr(os, "O_NONBLOCK"):
        assert flags & os.O_NONBLOCK
    with pytest.raises(OSError):
        os.fstat(descriptor)

    probe = real_open(view_lock, os.O_RDONLY)
    try:
        fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(probe, fcntl.LOCK_UN)
    finally:
        os.close(probe)


def test_read_view_does_not_create_a_missing_lock_and_sync_repairs_it(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_pointer = (data_dir / "active.json").read_bytes()
    view_lock = data_dir / cache_locks._VIEW_LOCK
    view_lock.unlink()

    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    details = _assert_cache_access_error(error.value, code="cache_not_initialized", path=view_lock)
    assert "sync" in details["remedy"].casefold()
    assert not view_lock.exists()
    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert Path(str(first["snapshot"])).is_dir()

    refreshed = cache_module.refresh_cache(source, data_dir)
    assert refreshed["status"] == "unchanged"
    assert view_lock.is_file()
    with cache_module.read_view(Settings(data_dir=data_dir)) as sandbox:
        assert sandbox.root == Path(str(first["snapshot"]))


def test_read_view_reports_active_pointer_permission_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    pointer = data_dir / "active.json"
    original_read = cache_state.read_json

    def deny_pointer_read(path: Path, *, limit: int) -> object:
        if path == pointer:
            raise PermissionError(errno.EACCES, "generated active-pointer denial", str(pointer))
        return original_read(path, limit=limit)

    monkeypatch.setattr(cache_state, "read_json", deny_pointer_read)
    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    _assert_cache_access_error(error.value, code="cache_permission_denied", path=pointer)


def test_read_view_reports_malformed_active_pointer(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    pointer = data_dir / "active.json"
    pointer.write_text("{generated malformed pointer", encoding="utf-8")

    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    _assert_cache_access_error(error.value, code="cache_invalid", path=pointer)


def test_settings_load_uses_canonical_pointer_validation_and_relative_cache_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    cache = tmp_path / "cache"
    first = cache_module.refresh_cache(source, cache)
    monkeypatch.chdir(tmp_path)

    loaded = Settings(data_dir=Path("./cache")).load_sandbox()
    assert loaded.root == Path(str(first["snapshot"]))

    outside = cache_lifecycle.create_snapshot(source, tmp_path / "outside")
    pointer_path = cache / "active.json"
    pointer = active_pointer(cache)
    pointer["snapshot"] = str(outside)
    pointer["absolutePath"] = str(outside)
    pointer_path.write_text(json.dumps(pointer), encoding="utf-8")
    with pytest.raises(CacheAccessError) as error:
        Settings(data_dir=Path("./cache")).load_sandbox()
    assert error.value.code == "cache_invalid"


@pytest.mark.parametrize("error_type", [PermissionError, GoodnotesError])
def test_read_view_preserves_errors_raised_by_its_caller(
    tmp_path: Path, error_type: type[Exception]
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    failure = error_type("generated caller-side failure")

    with pytest.raises(error_type) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            raise failure

    assert error.value is failure


def test_sync_cache_setup_permission_error_has_writer_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    original_mkdir = Path.mkdir

    def deny_cache_mkdir(
        path: Path,
        mode: int = 0o777,
        parents: bool = False,
        exist_ok: bool = False,
    ) -> None:
        if path == data_dir:
            raise PermissionError("generated cache directory denial")
        original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

    monkeypatch.setattr(Path, "mkdir", deny_cache_mkdir)
    with pytest.raises(GoodnotesError) as error:
        cache_module.refresh_cache(source, data_dir)

    details = _assert_cache_access_error(error.value, code="cache_permission_denied", path=data_dir)
    assert "sync" in details["remedy"].casefold()
    assert "cache owner" in details["remedy"].casefold()
    assert not data_dir.exists()


@pytest.mark.parametrize("lock_kind", ["symlink", "directory"])
def test_read_view_rejects_non_regular_view_lock_without_replacing_it(
    tmp_path: Path, lock_kind: str
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    view_lock = data_dir / cache_locks._VIEW_LOCK
    view_lock.unlink()
    target = tmp_path / "lock-target"
    target.write_bytes(b"generated lock target")
    if lock_kind == "symlink":
        view_lock.symlink_to(target)
    else:
        view_lock.mkdir()

    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    _assert_cache_access_error(error.value, code="cache_invalid", path=view_lock)
    assert view_lock.is_symlink() if lock_kind == "symlink" else view_lock.is_dir()
    assert target.read_bytes() == b"generated lock target"


def test_read_view_reports_a_denied_lock_open_without_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    view_lock = data_dir / cache_locks._VIEW_LOCK
    pointer_before = (data_dir / "active.json").read_bytes()
    original_open = os.open

    def deny_view_lock(path: str | bytes | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        if Path(os.fsdecode(path)) == view_lock:
            raise PermissionError("generated permission denial")
        return original_open(path, flags, mode)

    monkeypatch.setattr(cache_inventory.os, "open", deny_view_lock)
    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    _assert_cache_access_error(error.value, code="cache_permission_denied", path=view_lock)
    assert view_lock.is_file()
    assert (data_dir / "active.json").read_bytes() == pointer_before


def test_read_view_rejects_fifo_lock_without_blocking(tmp_path: Path) -> None:
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO locks are only available on POSIX")

    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    view_lock = data_dir / cache_locks._VIEW_LOCK
    view_lock.unlink()
    os.mkfifo(view_lock)

    context = multiprocessing.get_context("spawn")
    output = tmp_path / "fifo-reader-output.json"
    process = context.Process(
        target=_read_view_in_process,
        args=(str(data_dir), str(output)),
    )
    process.start()
    try:
        process.join(10)
        assert not process.is_alive(), "opening a FIFO as the view lock must not block"
        assert process.exitcode == 0
        result = json.loads(output.read_text(encoding="utf-8"))
        assert result["error"] == "CacheAccessError"
        assert result["code"] == "cache_invalid"
        assert result["details"]["path"] == str(view_lock)
    finally:
        if process.is_alive():
            process.terminate()
            process.join()


def test_read_view_rejects_a_replaced_lock_inode_after_acquiring_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    view_lock = data_dir / cache_locks._VIEW_LOCK
    original_inode = view_lock.stat().st_ino
    displaced = data_dir / ".view.lock-displaced"
    original_flock = fcntl.flock
    replaced = False

    def replace_after_lock(descriptor: int, operation: int) -> None:
        nonlocal replaced
        original_flock(descriptor, operation)
        if operation == fcntl.LOCK_SH and not replaced:
            replaced = True
            view_lock.rename(displaced)
            view_lock.write_bytes(b"replacement lock")

    monkeypatch.setattr(cache_locks.fcntl, "flock", replace_after_lock)
    with pytest.raises(GoodnotesError) as error:
        with cache_module.read_view(Settings(data_dir=data_dir)):
            pass

    _assert_cache_access_error(error.value, code="cache_invalid", path=view_lock)
    assert replaced
    assert displaced.stat().st_ino == original_inode
    assert view_lock.stat().st_ino != original_inode


def test_existing_cache_without_pointer_locks_before_resolving_reader(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_root = Path(str(first["snapshot"]))
    (data_dir / "active.json").unlink()
    (source / "Attachments" / "page.pdf").write_bytes(b"new attachment without pointer")

    load_started = context.Event()
    allow_load = context.Event()
    yielded = context.Event()
    release_reader = context.Event()
    reader_output = tmp_path / "reader-output.txt"
    reader = context.Process(
        target=_read_view_paused_with_missing_pointer,
        args=(
            str(data_dir),
            str(old_root),
            load_started,
            allow_load,
            yielded,
            release_reader,
            str(reader_output),
        ),
    )
    reader.start()
    assert load_started.wait(15), "reader did not reach load_sandbox"

    lock_fd = os.open(data_dir / cache_locks._VIEW_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
    blocked = False
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            blocked = True
        else:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
    assert blocked, "read_view must hold .view.lock before calling load_sandbox"

    captured = context.Event()
    publish_attempt = context.Event()
    complete = context.Event()
    sync_output = tmp_path / "sync-output.json"
    sync_process = context.Process(
        target=_refresh_waiting_to_publish,
        args=(str(source), str(data_dir), captured, publish_attempt, complete, str(sync_output)),
    )
    sync_process.start()
    assert captured.wait(15), "refresh did not finish capture while reader held the view"
    assert publish_attempt.wait(15), "refresh did not reach publication"
    assert not complete.is_set()
    assert not (data_dir / "active.json").exists()
    assert old_root.is_dir()

    allow_load.set()
    assert yielded.wait(15), "reader did not yield its locked view"
    assert old_root.is_dir()
    release_reader.set()
    reader.join(15)
    sync_process.join(15)
    for process in (reader, sync_process):
        if process.is_alive():
            process.terminate()
            process.join()

    assert reader.exitcode == 0
    assert sync_process.exitcode == 0
    assert reader_output.read_text(encoding="utf-8") == str(old_root)
    sync_result = json.loads(sync_output.read_text(encoding="utf-8"))
    assert "error" not in sync_result
    assert active_pointer(data_dir)["snapshot"] == sync_result["snapshot"]
    assert not old_root.exists()


def test_old_reader_holds_snapshot_while_capture_progresses_and_publication_waits(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_root = Path(str(first["snapshot"]))
    (source / "Attachments" / "page.pdf").write_bytes(b"new attachment")
    captured = context.Event()
    publish_attempt = context.Event()
    complete = context.Event()
    output = tmp_path / "child-result.json"
    with cache_module.read_view(Settings(data_dir=data_dir)) as sandbox:
        assert sandbox.root == old_root
        process = context.Process(
            target=_refresh_waiting_to_publish,
            args=(
                str(source),
                str(data_dir),
                captured,
                publish_attempt,
                complete,
                str(output),
            ),
        )
        process.start()
        assert captured.wait(15), "snapshot staging did not finish"
        assert publish_attempt.wait(15), "refresh did not reach pointer publication"
        assert active_pointer(data_dir)["snapshot"] == str(old_root)
        assert old_root.is_dir()
        assert not complete.is_set()

    process.join(15)
    if process.is_alive():
        process.terminate()
        process.join()
    assert process.exitcode == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert "error" not in result
    assert active_pointer(data_dir)["snapshot"] == result["snapshot"]
    assert not old_root.exists()


def test_concurrent_refreshes_serialize_and_second_sees_fresh_cache(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    cache_module.refresh_cache(source, data_dir)
    (source / "Attachments" / "page.pdf").write_bytes(b"changed before concurrent sync")
    start = context.Event()
    output_paths = [tmp_path / "first.json", tmp_path / "second.json"]
    processes = [
        context.Process(
            target=_refresh_in_process,
            args=(str(source), str(data_dir), start, str(output)),
        )
        for output in output_paths
    ]
    for process in processes:
        process.start()
    start.set()
    for process in processes:
        process.join(15)
        if process.is_alive():
            process.terminate()
            process.join()

    assert all(process.exitcode == 0 for process in processes)
    results = [json.loads(path.read_text(encoding="utf-8")) for path in output_paths]
    assert sorted(result["status"] for result in results) == ["ready", "unchanged"]
    assert len(generated_snapshots(data_dir)) == 1
    assert active_pointer(data_dir)["snapshot"] in {result["snapshot"] for result in results}


def test_abandoned_staging_is_cleaned_on_next_refresh(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    stale_stage = data_dir / "snapshots" / (".staging-" + "a" * 32)
    stale_stage.mkdir()
    (stale_stage / "partial.bin").write_bytes(b"generated partial staging fixture")
    (source / "Attachments" / "page.pdf").write_bytes(b"change")

    second = cache_module.refresh_cache(source, data_dir)

    assert second["status"] == "ready"
    assert not stale_stage.exists()
    assert Path(str(first["snapshot"])) not in generated_snapshots(data_dir)


def test_interrupted_capture_cleans_partial_staging_and_preserves_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_pointer = (data_dir / "active.json").read_bytes()
    (source / "Attachments" / "page.pdf").write_bytes(b"changed source")

    def interrupt(source_path: Path, destination: Path, **kwargs: object) -> Path:
        del source_path, kwargs
        destination.mkdir()
        (destination / "partial.bin").write_bytes(b"interrupted staging fixture")
        raise KeyboardInterrupt

    monkeypatch.setattr(cache_lifecycle, "create_snapshot", interrupt)
    with pytest.raises(KeyboardInterrupt):
        cache_module.refresh_cache(source, data_dir)

    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert not any(path.name.startswith(".staging-") for path in (data_dir / "snapshots").iterdir())
    assert Path(str(first["snapshot"])).is_dir()


def test_explicit_managed_sandbox_takes_shared_view_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    result = cache_module.refresh_cache(source, data_dir)
    sandbox_path = Path(str(result["snapshot"]))
    acquired: list[tuple[Path, bool]] = []
    original_lock = cache_lifecycle._file_lock

    @contextmanager
    def record_lock(
        path: Path, *, exclusive: bool, create: bool = True
    ) -> Generator[None, None, None]:
        acquired.append((path, exclusive))
        with original_lock(path, exclusive=exclusive, create=create):
            yield

    monkeypatch.setattr(cache_lifecycle, "_file_lock", record_lock)
    settings = Settings(sandbox=sandbox_path)
    with cache_module.read_view(settings) as sandbox:
        assert sandbox.root == sandbox_path

    assert acquired == [(data_dir / cache_locks._VIEW_LOCK, False)]
    assert settings.snapshot_id == sandbox_path.name


def test_explicit_managed_sandbox_dotdot_alias_holds_snapshot_during_prune(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)
    old_root = Path(str(first["snapshot"]))
    # Resolves to the same managed generation, but its lexical parent is "..".
    sandbox_alias = old_root.parent / old_root.name / ".." / old_root.name
    assert sandbox_alias.resolve() == old_root
    (source / "Attachments" / "page.pdf").write_bytes(b"changed for explicit alias prune")

    yielded = context.Event()
    release_reader = context.Event()
    reader_output = tmp_path / "explicit-reader-output.txt"
    reader = context.Process(
        target=_read_explicit_view_paused,
        args=(str(sandbox_alias), yielded, release_reader, str(reader_output)),
    )
    reader.start()
    sync_process: Any = None
    try:
        assert yielded.wait(15), "explicit reader did not enter read_view"

        captured = context.Event()
        publish_attempt = context.Event()
        complete = context.Event()
        sync_output = tmp_path / "explicit-alias-sync-output.json"
        sync_process = context.Process(
            target=_refresh_waiting_to_publish,
            args=(
                str(source),
                str(data_dir),
                captured,
                publish_attempt,
                complete,
                str(sync_output),
            ),
        )
        sync_process.start()
        assert captured.wait(15), "refresh did not finish capture while explicit reader held view"
        assert publish_attempt.wait(15), "refresh did not reach publication"
        assert not complete.is_set(), "refresh must wait for the explicit reader's view lock"
        assert active_pointer(data_dir)["snapshot"] == str(old_root)
        assert old_root.is_dir()
    finally:
        release_reader.set()
        reader.join(15)
        if sync_process is not None:
            sync_process.join(15)
        for process in (reader, sync_process):
            if process is not None and process.is_alive():
                process.terminate()
                process.join()

    assert reader.exitcode == 0
    assert sync_process is not None and sync_process.exitcode == 0
    assert reader_output.read_text(encoding="utf-8") == str(old_root)
    sync_result = json.loads((tmp_path / "explicit-alias-sync-output.json").read_text())
    assert "error" not in sync_result
    assert active_pointer(data_dir)["snapshot"] == sync_result["snapshot"]
    assert not old_root.exists()


def test_explicit_custom_named_sandbox_still_loads_without_managed_cache(
    tmp_path: Path,
) -> None:
    sandbox_path = tmp_path / "custom" / "snapshot"
    sandbox_path.mkdir(parents=True)
    database_dir = sandbox_path / "Library" / "Databases"
    database_dir.mkdir(parents=True)
    (sandbox_path / "Library" / "Attachments").mkdir()
    with sqlite3.connect(database_dir / "projection.sqlite"):
        pass
    (sandbox_path / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )

    with cache_module.read_view(Settings(sandbox=sandbox_path)) as sandbox:
        assert sandbox.root == sandbox_path
        assert sandbox.root.name == "snapshot"


def test_explicit_managed_sandbox_symlink_is_still_rejected(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    result = cache_module.refresh_cache(source, tmp_path / "cache")
    sandbox_path = Path(str(result["snapshot"]))
    alias = tmp_path / "snapshot-link"
    alias.symlink_to(sandbox_path, target_is_directory=True)

    with pytest.raises(GoodnotesError, match="snapshot root must not be a symlink"):
        with cache_module.read_view(Settings(sandbox=alias)):
            pass


def test_expected_snapshot_mismatch_raises_stale_snapshot_error(tmp_path: Path) -> None:
    sandbox_path = tmp_path / "named-snapshot"
    sandbox_path.mkdir()
    (sandbox_path / "Library" / "Databases").mkdir(parents=True)
    (sandbox_path / "Library" / "Attachments").mkdir()
    with sqlite3.connect(sandbox_path / "Library" / "Databases" / "projection.sqlite"):
        pass
    (sandbox_path / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )
    settings = Settings(sandbox=sandbox_path, expect_snapshot="different-generation")

    with pytest.raises(StaleSnapshotError, match="requested") as error:
        with cache_module.read_view(settings):
            pass

    assert error.value.code == "stale_snapshot"
