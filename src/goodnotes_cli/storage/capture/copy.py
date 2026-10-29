"""Make and verify independent file copies."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import stat
import sys
from collections.abc import Iterable
from pathlib import Path

from ...errors import FilesystemAccessError, GoodnotesError, _permission_denied
from .inventory import _hash_file, _lstat, _stat_key
from .types import _Entry, _SourceChanged

_CLONE_FALLBACK_ERRNOS = {
    errno.EXDEV,
    errno.ENOTSUP,
    getattr(errno, "EOPNOTSUPP", errno.ENOTSUP),
}
_CLONEFILE_LIBRARY = None
_FCLONEFILEAT = None
if sys.platform == "darwin":
    try:
        _CLONEFILE_LIBRARY = ctypes.CDLL(None, use_errno=True)
        _FCLONEFILEAT = getattr(_CLONEFILE_LIBRARY, "fclonefileat", None)
        if _FCLONEFILEAT is not None:
            _FCLONEFILEAT.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_char_p,
                ctypes.c_uint32,
            ]
            _FCLONEFILEAT.restype = ctypes.c_int
    except OSError:
        _FCLONEFILEAT = None


def _clone_file_from_fd(source_fd: int, destination: Path) -> bool:
    """Clone one open regular file when the destination filesystem supports it."""

    if _FCLONEFILEAT is None:
        return False
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        destination_dir_fd = os.open(destination.parent, directory_flags)
    except OSError as exc:
        raise GoodnotesError(f"Could not open snapshot directory: {destination.parent}") from exc
    try:
        ctypes.set_errno(0)
        result = _FCLONEFILEAT(
            source_fd,
            destination_dir_fd,
            os.fsencode(destination.name),
            0,
        )
        if result == 0:
            return True
        error_code = ctypes.get_errno()
        raise OSError(error_code, os.strerror(error_code), str(destination))
    finally:
        os.close(destination_dir_fd)


def _copy_regular_file(source: Path, destination: Path, expected: _Entry) -> bool:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT}:
            raise _SourceChanged(f"Source file changed while copying: {source}") from exc
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="copy_snapshot_input", path=source, kind="source"
            ) from exc
        raise GoodnotesError(f"Could not copy snapshot input: {source}") from exc

    try:
        source_info = os.fstat(source_fd)
        if not stat.S_ISREG(source_info.st_mode) or _stat_key(source_info) != expected.stat_key:
            raise _SourceChanged(f"Source file changed before copying: {source}")
        try:
            cloned = _clone_file_from_fd(source_fd, destination)
        except OSError as exc:
            if exc.errno not in _CLONE_FALLBACK_ERRNOS:
                raise GoodnotesError(f"Could not clone snapshot input: {source}") from exc
            cloned = False

        if not cloned:
            try:
                destination_fd = os.open(
                    destination,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
            except OSError as exc:
                raise GoodnotesError(f"Could not create snapshot file: {destination}") from exc
            digest = hashlib.sha256()
            source_stream = os.fdopen(source_fd, "rb", closefd=False)
            try:
                dest_stream = os.fdopen(destination_fd, "wb")
            except Exception:
                source_stream.close()
                os.close(destination_fd)
                raise
            with source_stream, dest_stream:
                for chunk in iter(lambda: source_stream.read(1024 * 1024), b""):
                    dest_stream.write(chunk)
                    digest.update(chunk)
                dest_stream.flush()
                os.fsync(dest_stream.fileno())
            if digest.hexdigest() != expected.sha256:
                raise _SourceChanged(f"Source file contents changed while copying: {source}")
            os.utime(destination, ns=(source_info.st_atime_ns, source_info.st_mtime_ns))

        after_fd = _stat_key(os.fstat(source_fd))
        after_path = _stat_key(_lstat(source))
        if after_fd != expected.stat_key or after_path != expected.stat_key:
            raise _SourceChanged(f"Source file changed while copying: {source}")
        copied = _lstat(destination)
        if not stat.S_ISREG(copied.st_mode) or (copied.st_dev, copied.st_ino) == (
            source_info.st_dev,
            source_info.st_ino,
        ):
            raise GoodnotesError(
                f"Snapshot copy must have an independent regular inode: {destination}"
            )
        return cloned
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _copy_snapshot_tree(
    source_library: Path, destination_library: Path, entries: Iterable[_Entry]
) -> dict[str, int]:
    all_entries = list(entries)
    directories = sorted(
        (entry for entry in all_entries if entry.is_directory),
        key=lambda entry: (len(entry.relative.parts), entry.relative.as_posix()),
    )
    files = [entry for entry in all_entries if not entry.is_directory]

    for entry in directories:
        target = destination_library / entry.relative
        if target != destination_library:
            target.mkdir(mode=0o700)
    metrics = {
        "cloned_files": 0,
        "cloned_logical_bytes": 0,
        "byte_copied_files": 0,
        "byte_copied_bytes": 0,
        "source_copy_bytes_hashed": 0,
    }
    for entry in files:
        target = destination_library / entry.relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        cloned = _copy_regular_file(
            entry.copy_from or source_library / entry.relative, target, entry
        )
        if cloned:
            metrics["cloned_files"] += 1
            metrics["cloned_logical_bytes"] += entry.size
        else:
            metrics["byte_copied_files"] += 1
            metrics["byte_copied_bytes"] += entry.size
            if not entry.reused:
                metrics["source_copy_bytes_hashed"] += entry.size
    return metrics


def _verify_copy(destination_library: Path, entries: Iterable[_Entry]) -> None:
    for entry in entries:
        target = destination_library / entry.relative
        if entry.is_directory:
            info = _lstat(target)
            if not stat.S_ISDIR(info.st_mode):
                raise GoodnotesError(f"Snapshot directory verification failed: {target}")
            continue
        if entry.reused:
            # The previous digest was verified during its original capture. Its
            # saved stat key was checked before reuse; clonefile produces an
            # independent inode with the same bytes. The fallback byte copier
            # verifies its streaming digest. A COW clone is trusted under that
            # fingerprint and the clonefile byte-preservation contract; it is not
            # read back and rehashed.
            info = _lstat(target)
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry.size:
                raise GoodnotesError(f"Reused snapshot file verification failed: {target}")
            continue
        _, size, digest = _hash_file(target)
        if size != entry.size or digest != entry.sha256:
            raise GoodnotesError(f"Snapshot file verification failed: {target}")
