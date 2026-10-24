from __future__ import annotations

import fcntl
import hashlib
import json
import multiprocessing
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

import goodnotes_cli.cache as cache_module
from goodnotes_cli.config import Settings
from goodnotes_cli.errors import GoodnotesError, StaleSnapshotError
from goodnotes_cli.safety import Sandbox


def make_library(root: Path) -> Path:
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


def active_pointer(data_dir: Path) -> dict[str, Any]:
    return json.loads((data_dir / "active.json").read_text(encoding="utf-8"))


def generated_snapshots(data_dir: Path) -> list[Path]:
    return sorted(path for path in (data_dir / "snapshots").iterdir() if path.is_dir())


def test_refresh_skips_capture_when_source_inventory_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    data_dir = tmp_path / "cache"
    first = cache_module.refresh_cache(source, data_dir)

    def fail_if_captured(*args: object, **kwargs: object) -> Path:
        raise AssertionError("unchanged refresh should not capture a snapshot")

    monkeypatch.setattr(cache_module, "create_snapshot", fail_if_captured)
    second = cache_module.refresh_cache(source, data_dir)

    assert first["status"] == "ready"
    assert second["status"] == "unchanged"
    assert second["snapshot"] == first["snapshot"]
    assert second["snapshot_id"] == first["snapshot_id"]
    assert second["captured_at"] == first["captured_at"]


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
    original_lstat = cache_module._lstat
    committed = False

    def stat_then_commit(path: Path) -> os.stat_result:
        nonlocal committed
        result = original_lstat(path)
        if path == wal_path and not committed:
            committed = True
            writer.execute("INSERT INTO item VALUES ('committed after first stat')")
            writer.commit()
        return result

    monkeypatch.setattr(cache_module, "_lstat", stat_then_commit)
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
    original_create = cache_module.create_snapshot

    def capture_then_advance(
        source_library: str | Path,
        destination: str | Path,
        *,
        max_attempts: int = 3,
    ) -> Path:
        snapshot = original_create(source_library, destination, max_attempts=max_attempts)
        attachment.write_bytes(b"source advanced after capture")
        return snapshot

    monkeypatch.setattr(cache_module, "create_snapshot", capture_then_advance)
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
    original_remove = cache_module._safe_remove_tree

    def interrupt_tombstone_cleanup(path: Path) -> None:
        if path.parent.name == "snapshots" and path.name.startswith(".staging-"):
            path.chmod(0o700)
            (path / "goodnotes-snapshot.json").unlink()
            raise GoodnotesError("simulated cleanup interruption")
        original_remove(path)

    with monkeypatch.context() as patcher:
        patcher.setattr(cache_module, "_safe_remove_tree", interrupt_tombstone_cleanup)
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

    monkeypatch.setattr(cache_module, "create_snapshot", fail_capture)
    with pytest.raises(GoodnotesError, match="capture failure"):
        cache_module.refresh_cache(source, data_dir, force=True)

    assert (data_dir / "active.json").read_bytes() == old_pointer
    assert Path(str(first["snapshot"])).is_dir()


def _refresh_in_process(
    source: str,
    data_dir: str,
    start: multiprocessing.synchronize.Event,
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
    captured: multiprocessing.synchronize.Event,
    publish_attempt: multiprocessing.synchronize.Event,
    complete: multiprocessing.synchronize.Event,
    output: str,
) -> None:
    original_create = cache_module.create_snapshot

    def capture_then_signal(
        source_library: str | Path,
        destination: str | Path,
        *,
        max_attempts: int = 3,
    ) -> Path:
        result = original_create(source_library, destination, max_attempts=max_attempts)
        captured.set()
        return result

    original_lock = cache_module._file_lock

    @contextmanager
    def observe_exclusive_view_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
        if path.name == cache_module._VIEW_LOCK and exclusive:
            publish_attempt.set()
        with original_lock(path, exclusive=exclusive):
            yield

    cache_module.create_snapshot = capture_then_signal
    cache_module._file_lock = observe_exclusive_view_lock
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
    load_started: multiprocessing.synchronize.Event,
    allow_load: multiprocessing.synchronize.Event,
    yielded: multiprocessing.synchronize.Event,
    release_reader: multiprocessing.synchronize.Event,
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

    lock_fd = os.open(data_dir / cache_module._VIEW_LOCK, os.O_CREAT | os.O_RDWR, 0o600)
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

    monkeypatch.setattr(cache_module, "create_snapshot", interrupt)
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
    original_lock = cache_module._file_lock

    @contextmanager
    def record_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
        acquired.append((path, exclusive))
        with original_lock(path, exclusive=exclusive):
            yield

    monkeypatch.setattr(cache_module, "_file_lock", record_lock)
    settings = Settings(sandbox=sandbox_path)
    with cache_module.read_view(settings) as sandbox:
        assert sandbox.root == sandbox_path

    assert acquired == [(data_dir / cache_module._VIEW_LOCK, False)]
    assert settings.snapshot_id == sandbox_path.name


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
