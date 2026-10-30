from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Any

import pytest

import goodnotes_cli.storage.artifacts.filesystem as filesystem_module
from goodnotes_cli.artifacts import ArtifactCache
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.paths import stat_key


@pytest.fixture
def cache(tmp_path: Path) -> ArtifactCache:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    return ArtifactCache(tmp_path / "state" / "artifacts", snapshot)


def _assert_closed(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        with pytest.raises(OSError) as error:
            os.fstat(descriptor)
        assert error.value.errno == errno.EBADF


def _close_remaining(descriptors: list[int]) -> None:
    for descriptor in descriptors:
        try:
            os.close(descriptor)
        except OSError as error:
            if error.errno != errno.EBADF:
                raise


@pytest.mark.parametrize("boundary", ["stat", "open"])
@pytest.mark.parametrize("borrow_parent", [False, True])
def test_copy_acquisition_failure_closes_owned_parents(
    cache: ArtifactCache,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
    borrow_parent: bool,
) -> None:
    owned: list[int] = []
    borrowed: int | None = None
    original_open, original_dup = os.open, os.dup
    original_directory = cache._fs._open_directory_fd
    original_stat = cache._fs._stat_at
    primary: BaseException = OSError(errno.EIO, "generated initial source stat failure")
    if boundary == "open":
        primary = KeyboardInterrupt("generated source open interruption")

    def open_directory(path: Path) -> int:
        descriptor = original_directory(path)
        owned.append(descriptor)
        return descriptor

    def duplicate(descriptor: int) -> int:
        duplicate_fd = original_dup(descriptor)
        if descriptor == borrowed:
            owned.append(duplicate_fd)
        return duplicate_fd

    def source_stat(directory_fd: int, name: str) -> os.stat_result | None:
        if name == "source.json" and boundary == "stat":
            raise primary
        return original_stat(directory_fd, name)

    def source_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if path == "source.json" and boundary == "open":
            raise primary
        return original_open(path, flags, *args, **kwargs)

    with cache, cache.staging() as stage:
        source = stage / "source.json"
        source.write_bytes(b"generated copy payload")
        if borrow_parent:
            borrowed = original_directory(stage)
        try:
            with monkeypatch.context() as fault:
                fault.setattr(cache._fs, "_open_directory_fd", open_directory)
                fault.setattr(cache._fs, "_stat_at", source_stat)
                fault.setattr(os, "dup", duplicate)
                fault.setattr(os, "open", source_open)
                with pytest.raises(type(primary)) as error:
                    cache._blobs._copy_private_file(
                        source,
                        stage / "copy.json",
                        expected_identity=None,
                        destination_parent_fd=borrowed,
                    )
            assert error.value is primary
            assert len(owned) == 2
            _assert_closed(owned)
            if borrowed is not None:
                assert os.fstat(borrowed).st_ino == stage.stat().st_ino
        finally:
            _close_remaining(owned)
            if borrowed is not None:
                os.close(borrowed)


@pytest.mark.parametrize("failure", [KeyboardInterrupt, OSError])
def test_initial_blob_scan_failure_closes_directory(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch, failure: type[BaseException]
) -> None:
    acquired: list[int] = []
    original_open = cache._fs._open_managed_directory
    primary = failure("generated initial blob scan failure")

    def open_directory(name: str) -> int:
        descriptor = original_open(name)
        acquired.append(descriptor)
        return descriptor

    def scan(directory_fd: int) -> None:
        assert directory_fd == acquired[0]
        raise primary

    with cache:
        try:
            with monkeypatch.context() as fault:
                fault.setattr(cache._fs, "_open_managed_directory", open_directory)
                fault.setattr(os, "scandir", scan)
                expected = GoodnotesError if failure is OSError else KeyboardInterrupt
                with pytest.raises(expected) as error:
                    cache._blobs._blob_inventory()
            if failure is OSError:
                assert error.value.__cause__ is primary
                assert "Could not inspect artifact blobs" in str(error.value)
            else:
                assert error.value is primary
            assert len(acquired) == 1
            _assert_closed(acquired)
        finally:
            _close_remaining(acquired)


@pytest.mark.parametrize("prior_error", [False, True])
@pytest.mark.parametrize("close_completes", [True, False], ids=["closed", "attempted"])
@pytest.mark.parametrize("unlock_fails", [False, True])
def test_release_detaches_before_attempting_every_resource_once(
    cache: ArtifactCache,
    monkeypatch: pytest.MonkeyPatch,
    prior_error: bool,
    close_completes: bool,
    unlock_fails: bool,
) -> None:
    cache.__enter__()
    fs = cache._fs
    lock_fd, root_fd = fs._lock_fd, fs._root_fd
    assert lock_fd is not None and root_fd is not None
    events: list[tuple[str, int]] = []
    original_close = os.close
    unlock_error = OSError(errno.EIO, "generated unlock failure")
    close_errors = {
        descriptor: OSError(errno.EIO, "generated close failure")
        for descriptor in (lock_fd, root_fd)
    }
    primary = RuntimeError("generated operation failure") if prior_error else None
    cause = ValueError("generated original cause")
    if primary is not None:
        primary.__cause__ = cause

    def detached() -> None:
        assert fs._lock_fd is None and fs._root_fd is None
        assert fs._root_identity is None
        assert not fs._managed_directory_identities

    def unlock(descriptor: int, operation: int) -> None:
        detached()
        assert operation == filesystem_module.fcntl.LOCK_UN
        events.append(("unlock", descriptor))
        if unlock_fails:
            raise unlock_error

    def close(descriptor: int) -> None:
        detached()
        events.append(("close", descriptor))
        if close_completes:
            original_close(descriptor)
        raise close_errors[descriptor]

    try:
        with monkeypatch.context() as fault:
            fault.setattr(filesystem_module.fcntl, "flock", unlock)
            fault.setattr(os, "close", close)
            if primary is None:
                with pytest.raises(OSError) as error:
                    fs._release_lock()
                assert error.value is (unlock_error if unlock_fails else close_errors[lock_fd])
            else:
                fs._release_lock(primary_error=primary)
                assert primary.__cause__ is cause
                notes = "\n".join(primary.__notes__)
                assert "OSError" in notes and "generated close failure" not in notes
        assert events == [("unlock", lock_fd), ("close", lock_fd), ("close", root_fd)]
        detached()
        if close_completes:
            _assert_closed([lock_fd, root_fd])
        # A close that raises before calling the OS proves attempts only.
    finally:
        _close_remaining([lock_fd, root_fd])
        fs._release_lock()


def test_unlock_failure_allows_reentry_without_losing_old_root(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    cache.__enter__()
    old_root, old_lock = cache._fs._root_fd, cache._fs._lock_fd
    assert old_root is not None and old_lock is not None
    original_flock = filesystem_module.fcntl.flock
    primary = OSError(errno.EIO, "generated unlock failure")

    def unlock(descriptor: int, operation: int) -> None:
        if operation == filesystem_module.fcntl.LOCK_UN:
            raise primary
        original_flock(descriptor, operation)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(filesystem_module.fcntl, "flock", unlock)
            with pytest.raises(OSError) as error:
                cache.__exit__(None, None, None)
        assert error.value is primary
        with cache:
            assert cache._fs._root_fd is not None
        _assert_closed([old_root, old_lock])
        assert cache._fs._root_fd is None
    finally:
        _close_remaining([old_root, old_lock])
        cache._fs._release_lock()


@pytest.mark.parametrize(
    ("body_fails", "renewal"),
    [(True, None), (True, GoodnotesError), (True, RuntimeError), (False, GoodnotesError)],
)
def test_exit_release_preserves_selected_body_or_renewal_error(
    cache: ArtifactCache,
    monkeypatch: pytest.MonkeyPatch,
    body_fails: bool,
    renewal: type[Exception] | None,
) -> None:
    body = ValueError("generated body failure")
    renewal_error = renewal("generated renewal failure") if renewal else None
    release_error = OSError(errno.EIO, "generated unlock failure")
    original_flock = filesystem_module.fcntl.flock
    descriptors: list[int] = []

    def renew() -> None:
        if renewal_error is not None:
            raise renewal_error

    def unlock(descriptor: int, operation: int) -> None:
        if operation == filesystem_module.fcntl.LOCK_UN:
            raise release_error
        original_flock(descriptor, operation)

    expected = body if body_fails and renewal in (None, GoodnotesError) else renewal_error
    assert expected is not None
    with monkeypatch.context() as fault:
        fault.setattr(filesystem_module.fcntl, "flock", unlock)
        with pytest.raises(type(expected)) as error:
            with cache:
                assert cache._fs._root_fd is not None and cache._fs._lock_fd is not None
                descriptors.extend([cache._fs._root_fd, cache._fs._lock_fd])
                fault.setattr(cache, "renew_leases", renew)
                if body_fails:
                    raise body
    try:
        assert error.value is expected
        _assert_closed(descriptors)
    finally:
        _close_remaining(descriptors)
        cache._fs._release_lock()


def test_entry_release_preserves_maintenance_failure(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = KeyboardInterrupt("generated maintenance interruption")
    original_flock = filesystem_module.fcntl.flock
    descriptors: list[int] = []

    def maintain() -> None:
        assert cache._fs._root_fd is not None and cache._fs._lock_fd is not None
        descriptors.extend([cache._fs._root_fd, cache._fs._lock_fd])
        raise primary

    def unlock(descriptor: int, operation: int) -> None:
        if operation == filesystem_module.fcntl.LOCK_UN:
            raise OSError(errno.EIO, "generated unlock failure")
        original_flock(descriptor, operation)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(cache, "_maintain", maintain)
            fault.setattr(filesystem_module.fcntl, "flock", unlock)
            with pytest.raises(KeyboardInterrupt) as error:
                cache.__enter__()
        assert error.value is primary
        _assert_closed(descriptors)
    finally:
        _close_remaining(descriptors)
        cache._fs._release_lock()


def test_prepare_failure_does_not_retry_closed_descriptor_after_integer_reuse(
    cache: ArtifactCache, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    primary = GoodnotesError("generated root validation failure")
    original_close, original_open = os.close, os.open
    original_tree = cache._fs._open_directory_tree
    acquired: list[int] = []
    unrelated: list[int] = []
    attempts: list[int] = []
    unrelated_path = tmp_path / "unrelated"
    unrelated_path.write_bytes(b"generated unrelated descriptor")

    def open_tree(path: Path) -> int:
        descriptor = original_tree(path)
        acquired.append(descriptor)
        return descriptor

    def validate() -> None:
        raise primary

    def close(descriptor: int) -> None:
        if descriptor in acquired:
            attempts.append(descriptor)
            original_close(descriptor)
            if len(attempts) == 1:
                replacement_fd = original_open(unrelated_path, os.O_RDONLY)
                if replacement_fd != descriptor:
                    os.dup2(replacement_fd, descriptor)
                    original_close(replacement_fd)
                unrelated.append(descriptor)
                raise OSError(errno.EIO, "generated completed root close failure")
        else:
            original_close(descriptor)

    try:
        with monkeypatch.context() as fault:
            fault.setattr(cache._fs, "_open_directory_tree", open_tree)
            fault.setattr(cache._fs, "_verify_root_path", validate)
            fault.setattr(os, "close", close)
            with pytest.raises(GoodnotesError) as error:
                cache.__enter__()
        assert error.value is primary
        assert attempts == acquired
        assert cache._fs._root_fd is None and cache._fs._root_identity is None
        assert os.fstat(unrelated[0]).st_ino == unrelated_path.stat().st_ino
    finally:
        _close_remaining(unrelated)
        cache._fs._release_lock()


def test_provisional_lock_close_preserves_acquisition_failure(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch
) -> None:
    fs = cache._fs
    fs._prepare_root()
    original_open, original_close = os.open, os.close
    acquired: list[int] = []
    primary = OSError(errno.EIO, "generated lock acquisition failure")

    def open_lock(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if path == ".artifacts.lock":
            acquired.append(descriptor)
        return descriptor

    def acquire(descriptor: int, operation: int) -> None:
        assert operation == filesystem_module.fcntl.LOCK_EX
        raise primary

    def close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor in acquired:
            raise OSError(errno.EIO, "generated completed provisional close failure")

    try:
        with monkeypatch.context() as fault:
            fault.setattr(os, "open", open_lock)
            fault.setattr(os, "close", close)
            fault.setattr(filesystem_module.fcntl, "flock", acquire)
            with pytest.raises(GoodnotesError) as error:
                fs._acquire_lock()
        assert error.value.__cause__ is primary
        assert fs._lock_fd is None
        assert len(acquired) == 1
        _assert_closed(acquired)
    finally:
        _close_remaining(acquired)
        fs._release_lock()


@pytest.mark.parametrize("cleanup", ["stat", "unlink", "close"])
def test_copy_rollback_failure_preserves_copy_error_and_closes_resources(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    primary = RuntimeError("generated copy failure")
    original_open, original_close, original_unlink = os.open, os.close, os.unlink
    original_stat = cache._fs._stat_at
    original_directory = cache._fs._open_directory_fd
    acquired: list[int] = []
    target_fd: list[int] = []

    def open_directory(path: Path) -> int:
        descriptor = original_directory(path)
        acquired.append(descriptor)
        return descriptor

    def open_file(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        descriptor = original_open(path, flags, *args, **kwargs)
        if path in {"source.json", "copy.json"}:
            acquired.append(descriptor)
        if path == "copy.json":
            target_fd.append(descriptor)
        return descriptor

    def read(descriptor: int, size: int) -> bytes:
        raise primary

    def current(directory_fd: int, name: str) -> os.stat_result | None:
        if name == "copy.json" and cleanup == "stat":
            raise OSError(errno.EIO, "generated rollback stat failure")
        return original_stat(directory_fd, name)

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if path == "copy.json" and cleanup == "unlink":
            raise OSError(errno.EIO, "generated rollback unlink failure")
        original_unlink(path, *args, **kwargs)

    def close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor in target_fd and cleanup == "close":
            raise OSError(errno.EIO, "generated completed target close failure")

    with cache, cache.staging() as stage:
        source = stage / "source.json"
        source.write_bytes(b"generated copy payload")
        try:
            with monkeypatch.context() as fault:
                fault.setattr(cache._fs, "_open_directory_fd", open_directory)
                fault.setattr(cache._fs, "_stat_at", current)
                fault.setattr(os, "open", open_file)
                fault.setattr(os, "read", read)
                fault.setattr(os, "unlink", unlink)
                fault.setattr(os, "close", close)
                with pytest.raises(RuntimeError) as error:
                    cache._blobs._copy_private_file(
                        source, stage / "copy.json", expected_identity=stat_key(source.stat())
                    )
            assert error.value is primary
            assert len(acquired) == 4
            _assert_closed(acquired)
        finally:
            _close_remaining(acquired)


@pytest.mark.parametrize("cleanup", ["stat", "unlink", "close"])
def test_temporary_blob_rollback_preserves_publication_error(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch, cleanup: str
) -> None:
    primary = OSError(errno.EIO, "generated blob publication failure")
    original_stat = cache._fs._stat_at
    original_directory = cache._fs._open_directory_fd
    original_close, original_unlink = os.close, os.unlink
    parents: list[int] = []
    publication_failed = False

    def open_directory(path: Path) -> int:
        descriptor = original_directory(path)
        parents.append(descriptor)
        return descriptor

    def link(*args: Any, **kwargs: Any) -> None:
        nonlocal publication_failed
        publication_failed = True
        raise primary

    def current(directory_fd: int, name: str) -> os.stat_result | None:
        if publication_failed and name.startswith(".blob-") and cleanup == "stat":
            raise OSError(errno.EIO, "generated temporary stat failure")
        return original_stat(directory_fd, name)

    def unlink(path: Any, *args: Any, **kwargs: Any) -> None:
        if str(path).startswith(".blob-") and cleanup == "unlink":
            raise OSError(errno.EIO, "generated temporary unlink failure")
        original_unlink(path, *args, **kwargs)

    def close(descriptor: int) -> None:
        original_close(descriptor)
        if publication_failed and descriptor == parents[1] and cleanup == "close":
            raise OSError(errno.EIO, "generated completed temporary parent close failure")

    with cache, cache.staging() as stage:
        (stage / "page-1.png").write_bytes(b"generated PNG payload")
        item = cache._blobs._prepare_stage_files(stage)[0]
        try:
            with monkeypatch.context() as fault:
                fault.setattr(cache._fs, "_open_directory_fd", open_directory)
                fault.setattr(cache._fs, "_stat_at", current)
                fault.setattr(os, "link", link)
                fault.setattr(os, "unlink", unlink)
                fault.setattr(os, "close", close)
                with pytest.raises(GoodnotesError) as error:
                    cache._blobs._ensure_blob(item)
            assert error.value.__cause__ is primary
            _assert_closed(parents[:2])
        finally:
            _close_remaining(parents[:2])


@pytest.mark.parametrize("body_fails", [False, True])
def test_blob_link_attempts_both_parent_closes_after_first_close_failure(
    cache: ArtifactCache, monkeypatch: pytest.MonkeyPatch, body_fails: bool
) -> None:
    parents: list[int] = []
    closed: list[int] = []
    original_directory = cache._fs._open_directory_fd
    original_close, original_link = os.close, os.link
    primary = RuntimeError("generated link failure")
    cleanup = OSError(errno.EIO, "generated completed parent close failure")

    def open_directory(path: Path) -> int:
        descriptor = original_directory(path)
        parents.append(descriptor)
        return descriptor

    def link(*args: Any, **kwargs: Any) -> None:
        if body_fails:
            raise primary
        original_link(*args, **kwargs)

    def close(descriptor: int) -> None:
        original_close(descriptor)
        if descriptor in parents:
            closed.append(descriptor)
            if len(closed) == 1:
                raise cleanup

    with cache, cache.staging() as stage:
        source = stage / "page-1.png"
        source.write_bytes(b"generated linked PNG")
        try:
            with monkeypatch.context() as fault:
                fault.setattr(cache._fs, "_open_directory_fd", open_directory)
                fault.setattr(os, "link", link)
                fault.setattr(os, "close", close)
                expected = primary if body_fails else cleanup
                with pytest.raises(type(expected)) as error:
                    cache._blobs._link_blob(source, stage / "page-2.png")
            assert error.value is expected
            assert len(parents) == 2 and sorted(closed) == sorted(parents)
            _assert_closed(parents)
        finally:
            _close_remaining(parents)
