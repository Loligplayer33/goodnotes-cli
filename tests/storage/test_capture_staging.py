"""Generated regressions for capture ownership through failure and publication."""

from __future__ import annotations

import errno
import json
import os
import stat
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.safety import Sandbox
from goodnotes_cli.snapshot import create_snapshot
from goodnotes_cli.storage.cache import lifecycle as cache_lifecycle
from goodnotes_cli.storage.capture import lifecycle as capture_lifecycle
from goodnotes_cli.storage.capture import staging as capture_staging
from goodnotes_cli.storage.capture.types import _Entry, _SourceChanged
from goodnotes_cli.storage.sandbox import SNAPSHOT_MARKER
from tests.support.library import make_library


def _after_copy(monkeypatch: pytest.MonkeyPatch, action: Callable[[Path], None]) -> list[Path]:
    original = capture_lifecycle._copy_snapshot_tree
    attempts: list[Path] = []

    def copy_then_act(source: Path, destination: Path, entries: Iterable[_Entry]) -> dict[str, int]:
        attempts.append(destination)
        metrics = original(source, destination, entries)
        action(destination)
        return metrics

    monkeypatch.setattr(capture_lifecycle, "_copy_snapshot_tree", copy_then_act)
    return attempts


def _replacement(path: Path, moved: Path, foreign: Path, *, symlink: bool) -> None:
    path.rename(moved)
    if symlink:
        path.symlink_to(foreign, target_is_directory=True)
    else:
        path.mkdir()
        (path / "sentinel").write_bytes(b"foreign entry")
        path.chmod(0o510)


@pytest.mark.parametrize("entry", ["root", "Library"])
@pytest.mark.parametrize("symlink", [False, True], ids=["directory", "symlink"])
@pytest.mark.parametrize("source_changed", [False, True], ids=["copy-failure", "source-change"])
def test_capture_preserves_replaced_root_or_library(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    symlink: bool,
    source_changed: bool,
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    moved = tmp_path / "relocated-owned-entry"
    foreign = tmp_path / "foreign-target"
    foreign.mkdir()
    (foreign / "sentinel").write_bytes(b"foreign entry")
    foreign.chmod(0o510)
    foreign_mode = stat.S_IMODE(foreign.stat().st_mode)
    error = (
        _SourceChanged("generated source change")
        if source_changed
        else GoodnotesError("generated copy failure")
    )
    source_bytes = (source / "Attachments/page.pdf").read_bytes()

    def replace_and_fail(library: Path) -> None:
        path = library.parent if entry == "root" else library
        _replacement(path, moved, foreign, symlink=symlink)
        raise error

    attempts = _after_copy(monkeypatch, replace_and_fail)
    with pytest.raises(GoodnotesError) as caught:
        create_snapshot(source, destination)

    if source_changed:
        assert caught.value.__cause__ is error
    else:
        assert caught.value is error
    assert len(attempts) == 1
    replaced = destination if entry == "root" else destination / "Library"
    assert replaced.is_symlink() is symlink
    assert (replaced / "sentinel").read_bytes() == b"foreign entry"
    assert stat.S_IMODE(replaced.stat().st_mode) == foreign_mode
    assert (foreign / "sentinel").read_bytes() == b"foreign entry"
    assert stat.S_IMODE(foreign.stat().st_mode) == foreign_mode
    relocated_library = moved / "Library" if entry == "root" else moved
    assert (relocated_library / "Attachments/page.pdf").read_bytes() == source_bytes
    assert (source / "Attachments/page.pdf").read_bytes() == source_bytes


def test_returned_copy_cannot_substitute_source_for_owned_library(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    moved = tmp_path / "relocated-owned-library"

    def source_state() -> dict[Path, tuple[bytes | None, int]]:
        return {
            path.relative_to(source): (
                path.read_bytes() if path.is_file() else None,
                stat.S_IMODE(path.stat().st_mode),
            )
            for path in [source, *source.rglob("*")]
        }

    before = source_state()

    def substitute_source(library: Path) -> None:
        library.rename(moved)
        library.symlink_to(source, target_is_directory=True)

    def fail_if_sqlite_opened(library: Path) -> None:
        pytest.fail("replaced Library reached the copied SQLite phase")

    attempts = _after_copy(monkeypatch, substitute_source)
    monkeypatch.setattr(capture_lifecycle, "_consolidate_copied_databases", fail_if_sqlite_opened)
    with pytest.raises(GoodnotesError, match="entry changed"):
        create_snapshot(source, destination)

    assert len(attempts) == 1
    assert (destination / "Library").is_symlink()
    assert (destination / "Library").resolve() == source
    assert (moved / "Attachments/page.pdf").read_bytes() == before[Path("Attachments/page.pdf")][0]
    assert source_state() == before
    assert not (destination / SNAPSHOT_MARKER).exists()


def _record_directory_opens(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    original = os.open
    descriptors: list[int] = []

    def record_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original(path, flags, *args, **kwargs)
        if flags & getattr(os, "O_DIRECTORY", 0):
            descriptors.append(descriptor)
        return descriptor

    monkeypatch.setattr(os, "open", record_open)
    return descriptors


def _assert_closed(descriptors: list[int]) -> None:
    assert descriptors
    for descriptor in descriptors:
        with pytest.raises(OSError) as caught:
            os.fstat(descriptor)
        assert caught.value.errno == errno.EBADF


def test_failure_before_library_creation_removes_empty_owned_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    error = RuntimeError("generated inventory failure")
    descriptors = _record_directory_opens(monkeypatch)

    def fail_plan(*args: object) -> object:
        assert destination.is_dir()
        assert list(destination.iterdir()) == []
        raise error

    monkeypatch.setattr(capture_lifecycle, "_plan_capture", fail_plan)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert not destination.exists()
    _assert_closed(descriptors)


@pytest.mark.parametrize("source_changed", [False, True], ids=["creation", "source-change"])
def test_capture_does_not_adopt_library_created_before_its_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_changed: bool
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    original_plan = capture_lifecycle._plan_capture
    error = _SourceChanged("generated change before Library creation")
    attempts = 0

    def insert_foreign_library(
        source: Path, entries: list[_Entry], previous: Path | None
    ) -> tuple[list[_Entry], list[_Entry]]:
        nonlocal attempts
        attempts += 1
        planned = original_plan(source, entries, previous)
        foreign = destination / "Library"
        foreign.mkdir()
        (foreign / "sentinel").write_bytes(b"foreign Library")
        foreign.chmod(0o510)
        if source_changed:
            raise error
        return planned

    monkeypatch.setattr(capture_lifecycle, "_plan_capture", insert_foreign_library)
    with pytest.raises(GoodnotesError) as caught:
        create_snapshot(source, destination)

    if source_changed:
        assert caught.value.__cause__ is error
    assert attempts == 1
    assert (destination / "Library/sentinel").read_bytes() == b"foreign Library"
    assert stat.S_IMODE((destination / "Library").stat().st_mode) == 0o510
    assert not (destination / "goodnotes-snapshot.json").exists()


def test_capture_preserves_unknown_root_child_after_copy_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    error = RuntimeError("generated copy failure")

    def insert_foreign_sibling(library: Path) -> None:
        sibling = library.parent / "foreign-sibling"
        sibling.write_bytes(b"foreign root child")
        sibling.chmod(0o400)
        raise error

    _after_copy(monkeypatch, insert_foreign_sibling)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert not (destination / "Library").exists()
    assert (destination / "foreign-sibling").read_bytes() == b"foreign root child"
    assert stat.S_IMODE((destination / "foreign-sibling").stat().st_mode) == 0o400


def test_capture_cleanup_unlinks_nested_symlink_without_changing_its_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    foreign = tmp_path / "foreign-target"
    foreign.mkdir()
    sentinel = foreign / "sentinel"
    sentinel.write_bytes(b"foreign symlink target")
    sentinel.chmod(0o400)
    foreign.chmod(0o500)
    error = RuntimeError("generated copy failure")

    def insert_symlink(library: Path) -> None:
        (library / "Attachments/foreign-link").symlink_to(foreign, target_is_directory=True)
        raise error

    _after_copy(monkeypatch, insert_symlink)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert not destination.exists()
    assert sentinel.read_bytes() == b"foreign symlink target"
    assert stat.S_IMODE(sentinel.stat().st_mode) == 0o400
    assert stat.S_IMODE(foreign.stat().st_mode) == 0o500


@pytest.mark.parametrize("original_type", [RuntimeError, KeyboardInterrupt])
@pytest.mark.parametrize("cleanup_type", [OSError, GoodnotesError, KeyboardInterrupt])
def test_cleanup_failure_preserves_original_failure_and_closes_descriptors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    original_type: type[BaseException],
    cleanup_type: type[BaseException],
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    error = original_type("generated capture failure")
    cleanup_error = cleanup_type("generated cleanup failure")
    descriptors = _record_directory_opens(monkeypatch)

    def fail_copy(library: Path) -> None:
        raise error

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        raise cleanup_error

    _after_copy(monkeypatch, fail_copy)
    monkeypatch.setattr(capture_staging, "_remove_entry_at", fail_cleanup)
    with pytest.raises(original_type) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert (destination / "Library/Attachments/page.pdf").read_bytes() == (
        source / "Attachments/page.pdf"
    ).read_bytes()
    _assert_closed(descriptors)


@pytest.mark.parametrize("directory", ["parent", "root", "Library"])
def test_failed_directory_initialization_closes_acquired_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, directory: str
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    original_open = os.open
    original_fstat = os.fstat
    descriptors: list[int] = []
    fault_descriptor: int | None = None
    error = RuntimeError("generated directory validation failure")
    failed = False
    selected_path = {
        "parent": destination.parent,
        "root": destination.name,
        "Library": "Library",
    }[directory]

    def record_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        nonlocal fault_descriptor
        descriptor = original_open(path, flags, *args, **kwargs)
        if flags & getattr(os, "O_DIRECTORY", 0):
            descriptors.append(descriptor)
            if path == selected_path:
                fault_descriptor = descriptor
        return descriptor

    def fail_selected_fstat(descriptor: int) -> os.stat_result:
        nonlocal failed
        if descriptor == fault_descriptor and not failed:
            failed = True
            raise error
        return original_fstat(descriptor)

    monkeypatch.setattr(os, "open", record_open)
    monkeypatch.setattr(os, "fstat", fail_selected_fstat)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert failed
    assert not destination.exists()
    _assert_closed(descriptors)


def test_retry_replaces_only_owned_attempt_and_returns_immutable_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    descriptors = _record_directory_opens(monkeypatch)
    attempts: list[tuple[int, int]] = []

    def change_once(library: Path) -> None:
        info = library.stat()
        attempts.append((info.st_dev, info.st_ino))
        if len(attempts) == 1:
            raise _SourceChanged("generated first-attempt change")

    _after_copy(monkeypatch, change_once)
    result = create_snapshot(source, destination, max_attempts=2)

    assert result == destination
    assert len(attempts) == 2
    sandbox = Sandbox.load(result)
    assert sandbox.marker_data["capture_stats"]["attempts"] == 2
    assert (
        sandbox.projection_path.stat().st_ino
        != (source / "Databases/projection.sqlite").stat().st_ino
    )
    for path in [destination, *destination.rglob("*")]:
        assert path.stat().st_mode & 0o222 == 0
    _assert_closed(descriptors)


def test_source_change_retries_are_bounded_and_remove_owned_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    changes: list[_SourceChanged] = []

    def always_change(library: Path) -> None:
        error = _SourceChanged("generated repeated source change")
        changes.append(error)
        raise error

    attempts = _after_copy(monkeypatch, always_change)
    with pytest.raises(GoodnotesError, match="after 2 snapshot attempts") as caught:
        create_snapshot(source, destination, max_attempts=2)

    assert len(attempts) == len(changes) == 2
    assert caught.value.__cause__ is changes[-1]
    assert not destination.exists()


@pytest.mark.parametrize("cleanup_type", [OSError, GoodnotesError])
def test_failed_retry_reset_preserves_source_change_cause_and_stops_retrying(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleanup_type: type[Exception]
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    error = _SourceChanged("generated source change")
    descriptors = _record_directory_opens(monkeypatch)

    def change_source(library: Path) -> None:
        raise error

    def fail_cleanup(*args: object, **kwargs: object) -> None:
        raise cleanup_type("generated retry cleanup failure")

    attempts = _after_copy(monkeypatch, change_source)
    monkeypatch.setattr(capture_staging, "_remove_entry_at", fail_cleanup)
    with pytest.raises(GoodnotesError, match="could not be safely reset") as caught:
        create_snapshot(source, destination)

    assert caught.value.__cause__ is error
    assert len(attempts) == 1
    _assert_closed(descriptors)


def test_failure_after_marker_publication_removes_owned_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    error = RuntimeError("generated freeze failure")
    descriptors = _record_directory_opens(monkeypatch)

    def fail_freeze(path: Path, entries: Iterable[_Entry]) -> None:
        assert path == destination
        assert json.loads((path / SNAPSHOT_MARKER).read_text())["format_version"] == 1
        raise error

    monkeypatch.setattr(capture_lifecycle, "_make_read_only", fail_freeze)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert not destination.exists()
    _assert_closed(descriptors)


@pytest.mark.parametrize("body_type", [GoodnotesError, KeyboardInterrupt, None])
@pytest.mark.parametrize("close_type", [OSError, KeyboardInterrupt])
def test_marker_stream_close_preserves_body_error_or_propagates_its_own_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body_type: type[BaseException] | None,
    close_type: type[BaseException],
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    body_error = body_type("generated marker write failure") if body_type else None
    close_error = close_type("generated marker close failure")
    expected = body_error if body_error is not None else close_error
    original_fdopen = os.fdopen
    descriptors = _record_directory_opens(monkeypatch)
    marker_streams: list[BinaryIO] = []

    class FailingClose:
        def __init__(self, stream: BinaryIO) -> None:
            self.stream = stream

        def write(self, data: bytes) -> int:
            if body_error is not None:
                raise body_error
            return self.stream.write(data)

        def close(self) -> None:
            self.stream.close()
            raise close_error

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

        def __enter__(self) -> FailingClose:
            return self

        def __exit__(self, *args: object) -> None:
            self.close()

    def marker_fdopen(descriptor: int, *args: Any, **kwargs: Any) -> Any:
        stream = original_fdopen(descriptor, *args, **kwargs)
        if args == ("wb",) and list(destination.glob(".snapshot-marker-*")):
            marker_streams.append(stream)
            descriptors.append(descriptor)
            return FailingClose(stream)
        return stream

    monkeypatch.setattr(os, "fdopen", marker_fdopen)
    with pytest.raises(BaseException) as caught:
        create_snapshot(source, destination)

    assert caught.value is expected
    assert len(marker_streams) == 1
    assert marker_streams[0].closed
    assert not destination.exists()
    _assert_closed(descriptors)


def test_capture_preserves_replaced_completed_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    moved = tmp_path / "relocated-owned-marker"
    error = RuntimeError("generated freeze failure")

    def replace_marker(path: Path, entries: Iterable[_Entry]) -> None:
        marker = path / SNAPSHOT_MARKER
        marker.rename(moved)
        marker.write_bytes(b"foreign marker")
        marker.chmod(0o400)
        raise error

    monkeypatch.setattr(capture_lifecycle, "_make_read_only", replace_marker)
    with pytest.raises(RuntimeError) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert (destination / SNAPSHOT_MARKER).read_bytes() == b"foreign marker"
    assert stat.S_IMODE((destination / SNAPSHOT_MARKER).stat().st_mode) == 0o400
    assert json.loads(moved.read_text())["format_version"] == 1
    assert not (destination / "Library").exists()


def test_capture_preserves_temporary_marker_replaced_before_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    moved = tmp_path / "relocated-owned-marker"
    original_fsync = os.fsync
    replaced: list[Path] = []

    def replace_marker_after_flush(descriptor: int) -> None:
        original_fsync(descriptor)
        candidates = list(destination.glob(".snapshot-marker-*"))
        if candidates and not replaced:
            temporary = candidates[0]
            temporary.rename(moved)
            temporary.write_bytes(b"foreign temporary marker")
            temporary.chmod(0o400)
            replaced.append(temporary)

    monkeypatch.setattr(os, "fsync", replace_marker_after_flush)
    with pytest.raises(GoodnotesError, match="entry changed"):
        create_snapshot(source, destination)

    assert len(replaced) == 1
    assert replaced[0].read_bytes() == b"foreign temporary marker"
    assert stat.S_IMODE(replaced[0].stat().st_mode) == 0o400
    assert not (destination / SNAPSHOT_MARKER).exists()
    assert not (destination / "Library").exists()
    assert json.loads(moved.read_text())["format_version"] == 1


def test_interrupted_marker_publication_preserves_uncertain_final_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    destination = tmp_path / "snapshot"
    original_replace = os.replace
    error = KeyboardInterrupt("generated interrupted marker publication")

    def interrupt_replace(src: Any, dst: Any, **kwargs: Any) -> None:
        original_replace(src, dst, **kwargs)
        if dst == SNAPSHOT_MARKER:
            raise error

    monkeypatch.setattr(os, "replace", interrupt_replace)
    with pytest.raises(KeyboardInterrupt) as caught:
        create_snapshot(source, destination)

    assert caught.value is error
    assert json.loads((destination / SNAPSHOT_MARKER).read_text())["format_version"] == 1
    assert not (destination / "Library").exists()
    assert list(destination.glob(".snapshot-marker-*")) == []


def _managed_cache(tmp_path: Path) -> tuple[Path, Path, Path, bytes]:
    source = make_library(tmp_path / "source")
    state = tmp_path / "cache"
    first = cache_lifecycle.refresh_cache(source, state)
    previous = Path(str(first["snapshot"]))
    pointer = (state / "active.json").read_bytes()
    return source, state, previous, pointer


@pytest.mark.parametrize("entry", ["root", "Library"])
@pytest.mark.parametrize("source_changed", [False, True], ids=["copy-failure", "source-change"])
def test_managed_refresh_cannot_undo_replacement_preserved_by_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry: str,
    source_changed: bool,
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    moved = tmp_path / "relocated-owned-entry"
    replaced: list[Path] = []
    error = (
        _SourceChanged("generated source change")
        if source_changed
        else RuntimeError("generated copy failure")
    )

    def replace_staging(library: Path) -> None:
        path = library.parent if entry == "root" else library
        path.rename(moved)
        path.mkdir()
        (path / "sentinel").write_bytes(b"foreign staging entry")
        path.chmod(0o510)
        replaced.append(path)
        raise error

    attempts = _after_copy(monkeypatch, replace_staging)
    with pytest.raises(GoodnotesError if source_changed else RuntimeError) as caught:
        cache_lifecycle.refresh_cache(source, state, force=True)

    assert (caught.value.__cause__ if source_changed else caught.value) is error
    assert len(attempts) == len(replaced) == 1
    assert (replaced[0] / "sentinel").read_bytes() == b"foreign staging entry"
    assert stat.S_IMODE(replaced[0].stat().st_mode) == 0o510
    assert (state / "active.json").read_bytes() == pointer
    assert Sandbox.load(previous).root == previous
    relocated_library = moved / "Library" if entry == "root" else moved
    assert (relocated_library / "Attachments/page.pdf").read_bytes() == (
        source / "Attachments/page.pdf"
    ).read_bytes()


@pytest.mark.parametrize(
    "phase", ["capture", "staging-validation", "rename", "generation-validation", "pointer"]
)
def test_managed_failure_cleans_owned_tree_and_preserves_previous_pointer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    snapshots = state / "snapshots"
    before = {path.name for path in snapshots.iterdir()}
    error = RuntimeError(f"generated {phase} failure")
    descriptors = _record_directory_opens(monkeypatch)
    reached: list[str] = []
    original_load = Sandbox.load
    original_replace = os.replace
    original_write = cache_lifecycle._write_json_atomic

    def fail_copy(library: Path) -> None:
        reached.append("capture")
        raise error

    def fail_validation(path: str | Path) -> Sandbox:
        result = original_load(path)
        root = Path(path)
        staging = root.name.startswith(".staging-")
        if root != previous and (
            (phase == "staging-validation" and staging)
            or (phase == "generation-validation" and not staging)
        ):
            reached.append(phase)
            assert root.stat().st_mode & 0o222 == 0
            raise error
        return result

    def fail_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        if str(src).startswith(".staging-"):
            reached.append("rename")
            raise error
        original_replace(src, dst, **kwargs)

    def fail_pointer_write(path: Path, name: str, data: dict[str, Any]) -> None:
        if name == "active.json":
            reached.append("pointer")
            completed = Path(data["snapshot"])
            assert original_load(completed).root == completed
            raise error
        original_write(path, name, data)

    if phase == "capture":
        _after_copy(monkeypatch, fail_copy)
    elif phase in {"staging-validation", "generation-validation"}:
        monkeypatch.setattr(Sandbox, "load", fail_validation)
    elif phase == "rename":
        monkeypatch.setattr(os, "replace", fail_rename)
    else:
        monkeypatch.setattr(cache_lifecycle, "_write_json_atomic", fail_pointer_write)

    with pytest.raises(RuntimeError) as caught:
        cache_lifecycle.refresh_cache(source, state, force=True)

    assert caught.value is error
    assert reached == [phase]
    assert (state / "active.json").read_bytes() == pointer
    assert {path.name for path in snapshots.iterdir()} == before
    assert original_load(previous).root == previous
    _assert_closed(descriptors)


def test_managed_failure_after_rename_preserves_replacement_generation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    original_signature = cache_lifecycle._input_signature
    moved = tmp_path / "relocated-owned-generation"
    replaced: list[Path] = []
    error = RuntimeError("generated generation validation failure")

    def replace_generation(library: Path, *, include_root: bool = False) -> str:
        root = library.parent
        if root.parent == state / "snapshots" and root != previous:
            root.chmod(0o755)
            root.rename(moved)
            root.mkdir()
            (root / "sentinel").write_bytes(b"foreign generation")
            root.chmod(0o510)
            replaced.append(root)
            raise error
        return original_signature(library, include_root=include_root)

    monkeypatch.setattr(cache_lifecycle, "_input_signature", replace_generation)
    with pytest.raises(RuntimeError) as caught:
        cache_lifecycle.refresh_cache(source, state, force=True)

    assert caught.value is error
    assert len(replaced) == 1
    assert (replaced[0] / "sentinel").read_bytes() == b"foreign generation"
    assert stat.S_IMODE(replaced[0].stat().st_mode) == 0o510
    assert Sandbox.load(moved).root == moved
    assert (state / "active.json").read_bytes() == pointer
    assert Sandbox.load(previous).root == previous


def test_interrupted_generation_rename_preserves_uncertain_completed_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    original_replace = os.replace
    destinations: list[Path] = []
    error = KeyboardInterrupt("generated interrupted generation rename")

    def interrupt_rename(src: Any, dst: Any, **kwargs: Any) -> None:
        original_replace(src, dst, **kwargs)
        if str(src).startswith(".staging-"):
            destinations.append(state / "snapshots" / os.fsdecode(dst))
            raise error

    monkeypatch.setattr(os, "replace", interrupt_rename)
    with pytest.raises(KeyboardInterrupt) as caught:
        cache_lifecycle.refresh_cache(source, state, force=True)

    assert caught.value is error
    assert len(destinations) == 1
    assert Sandbox.load(destinations[0]).root == destinations[0]
    assert (state / "active.json").read_bytes() == pointer
    assert Sandbox.load(previous).root == previous
    assert list((state / "snapshots").glob(".staging-*")) == []


@pytest.mark.parametrize("pointer_read_fails", [False, True], ids=["confirmed", "uncertain"])
def test_interrupted_pointer_write_preserves_published_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, pointer_read_fails: bool
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    original_write = cache_lifecycle._write_json_atomic
    original_read = cache_lifecycle._read_pointer
    error = KeyboardInterrupt("generated interrupted pointer publication")
    written = False
    descriptors = _record_directory_opens(monkeypatch)

    def publish_then_interrupt(path: Path, name: str, data: dict[str, Any]) -> None:
        nonlocal written
        original_write(path, name, data)
        if name == "active.json":
            written = True
            raise error

    def fail_fallback_read(*args: Any, **kwargs: Any) -> Any:
        if written and pointer_read_fails:
            raise OSError("generated pointer-read failure")
        return original_read(*args, **kwargs)

    monkeypatch.setattr(cache_lifecycle, "_write_json_atomic", publish_then_interrupt)
    monkeypatch.setattr(cache_lifecycle, "_read_pointer", fail_fallback_read)
    with pytest.raises(KeyboardInterrupt) as caught:
        cache_lifecycle.refresh_cache(source, state, force=True)

    assert caught.value is error
    assert written
    assert (state / "active.json").read_bytes() != pointer
    active = json.loads((state / "active.json").read_text())
    published = Path(active["snapshot"])
    assert published != previous
    assert Sandbox.load(published).root == published
    assert Sandbox.load(previous).root == previous
    _assert_closed(descriptors)


def test_managed_success_releases_read_only_generation_and_closes_descriptors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, state, previous, pointer = _managed_cache(tmp_path)
    descriptors = _record_directory_opens(monkeypatch)
    result = cache_lifecycle.refresh_cache(source, state, force=True)

    assert result["status"] == "ready"
    published = Path(str(result["snapshot"]))
    assert published != previous
    active = json.loads((state / "active.json").read_text())
    assert active["snapshot"] == str(published)
    assert Sandbox.load(published).root == published
    assert (published / "Library/Attachments/page.pdf").read_bytes() == (
        source / "Attachments/page.pdf"
    ).read_bytes()
    for path in [published, *published.rglob("*")]:
        assert path.stat().st_mode & 0o222 == 0
    assert list((state / "snapshots").glob(".staging-*")) == []
    _assert_closed(descriptors)
