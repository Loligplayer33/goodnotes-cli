"""Storage operations."""

from __future__ import annotations

import fcntl
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from goodnotes_cli.errors import CacheAccessError

_SYNC_LOCK = ".sync.lock"
_VIEW_LOCK = ".view.lock"


def _lock_error(
    path: Path,
    operation: str,
    message: str,
    *,
    exc: OSError | None = None,
    missing_is_uninitialized: bool = False,
    writer: bool,
) -> CacheAccessError:
    if exc is not None:
        return CacheAccessError.from_exception(
            exc,
            f"{message}: {path}",
            operation=operation,
            path=path,
            cache_dir=path.parent,
            missing_is_uninitialized=missing_is_uninitialized,
            writer=writer,
        )
    return CacheAccessError(
        "cache_invalid",
        f"{message}: {path}",
        operation=operation,
        path=path,
        cache_dir=path.parent,
        writer=writer,
    )


def _verify_lock_path(path: Path, opened: os.stat_result, *, operation: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise CacheAccessError.from_exception(
            exc,
            f"Could not verify cache lock after acquisition: {path}",
            operation=operation,
            path=path,
            cache_dir=path.parent,
            writer=operation != "read_view_lock",
        ) from exc
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise _lock_error(
            path,
            operation,
            "The cache lock changed while it was being acquired",
            writer=operation != "read_view_lock",
        )


@contextmanager
def _file_lock(path: Path, *, exclusive: bool, create: bool = True) -> Generator[None, None, None]:
    """Lock a persistent CLI-owned file, optionally requiring an existing read-only open."""

    operation = (
        "read_view_lock"
        if not create
        else ("sync_lock" if path.name == _SYNC_LOCK else "sync_view_lock")
    )
    if not create and exclusive:
        raise ValueError("An existing read-only cache lock can only be acquired shared.")

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        if not create:
            raise _lock_error(
                path,
                operation,
                "The existing cache view lock is missing",
                exc=exc,
                missing_is_uninitialized=True,
                writer=False,
            ) from exc
        before = None
    except OSError as exc:
        raise _lock_error(
            path,
            operation,
            "Could not inspect the cache lock",
            exc=exc,
            writer=create,
        ) from exc
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise _lock_error(
            path,
            operation,
            "The cache lock must be a regular, non-symlink file",
            writer=create,
        )

    flags = getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_RDWR
    else:
        # O_NONBLOCK prevents a raced replacement with a FIFO from hanging the reader.
        flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _lock_error(
            path,
            operation,
            "Could not open the cache lock",
            exc=exc,
            missing_is_uninitialized=not create,
            writer=create,
        ) from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _lock_error(
                path,
                operation,
                "The cache lock is invalid or changed while it was being opened",
                writer=create,
            )
        _verify_lock_path(path, opened, operation=operation)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            raise _lock_error(
                path,
                operation,
                "Could not acquire the cache lock",
                exc=exc,
                writer=create,
            ) from exc
        # Recheck after flock in case the lock path changed while acquisition waited.
        _verify_lock_path(path, opened, operation=operation)
        yield
    finally:
        os.close(descriptor)
