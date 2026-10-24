"""Read native bytes only from disposable independent copies of validated snapshots.

RocksDB may write logs even on a read-only open. The engine never receives the
completed snapshot path. It receives a private temporary clone instead.
"""

from __future__ import annotations

import ctypes as C
import ctypes.util
import errno
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol

from .errors import GoodnotesError
from .safety import Sandbox, _require_not_production
from .snapshot import _clone_file_from_fd

_MAX_VALUE = 16 * 1024 * 1024
_MAX_PAGE_BYTES = 64 * 1024 * 1024
_MAX_PAGE_RECORDS = 100_000
_MAX_COPY_FALLBACK = 64 * 1024 * 1024


class NativeError(GoodnotesError):
    """Expected native-data failures with a stable recovery contract."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        path: Path | str = "",
        context: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        if code == "native_runtime_unavailable":
            remedy = (
                "Install a current RocksDB shared library, for example 'brew install rocksdb' "
                "on macOS, or set GOODNOTES_ROCKSDB_LIBRARY to its absolute path. "
                "The library and its compression dependencies must be loadable by this process."
            )
        elif operation == "create_native_scratch":
            remedy = (
                "Grant write/search access to the temporary directory or set "
                "GOODNOTES_SCRATCH_DIR to a writable private directory outside the snapshot."
            )
        else:
            remedy = (
                "Refresh the copied library with sync. Native records may be unavailable or use "
                "an unsupported Goodnotes format. Inspect the reported coverage before "
                "evaluating handwritten notes."
            )
        self.details: dict[str, object] = {
            "operation": operation,
            "path": str(path),
            "remedy": remedy,
        }
        if context is not None:
            self.details.update(context)


class SnapshotProtocol(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...


def _library_candidate() -> str | None:
    configured = os.environ.get("GOODNOTES_ROCKSDB_LIBRARY")
    if configured:
        path = Path(configured).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise NativeError(
                "native_runtime_unavailable",
                "GOODNOTES_ROCKSDB_LIBRARY must name an existing absolute shared-library file.",
                operation="load_native_runtime",
                path=path,
            )
        return str(path)
    for path in (
        Path("/opt/homebrew/opt/rocksdb/lib/librocksdb.dylib"),
        Path("/usr/local/opt/rocksdb/lib/librocksdb.dylib"),
    ):
        if path.is_file():
            return str(path)
    return ctypes.util.find_library("rocksdb")


class NativeRuntime:
    """Small typed binding to the stable RocksDB C API; does not open databases."""

    def __init__(self) -> None:
        try:
            candidate = _library_candidate()
        except OSError as exc:
            raise NativeError(
                "native_runtime_unavailable",
                f"Could not inspect the RocksDB library: {exc}",
                operation="load_native_runtime",
                path=exc.filename or "",
            ) from exc
        if candidate is None:
            raise NativeError(
                "native_runtime_unavailable",
                "No RocksDB shared library was found.",
                operation="load_native_runtime",
            )
        try:
            self.library = C.CDLL(candidate)
            self.path = candidate
            ptr, size, text, byte = C.c_void_p, C.c_size_t, C.c_char_p, C.c_ubyte
            error = C.POINTER(ptr)
            signatures = {
                "options_create": ([], ptr),
                "options_destroy": ([ptr], None),
                "options_set_create_if_missing": ([ptr, byte], None),
                "options_set_paranoid_checks": ([ptr, byte], None),
                "options_set_max_open_files": ([ptr, C.c_int], None),
                "options_set_db_log_dir": ([ptr, text], None),
                "options_set_wal_dir": ([ptr, text], None),
                "open_for_read_only": ([ptr, text, byte, error], ptr),
                "close": ([ptr], None),
                "free": ([ptr], None),
                "readoptions_create": ([], ptr),
                "readoptions_destroy": ([ptr], None),
                "readoptions_set_fill_cache": ([ptr, byte], None),
                "readoptions_set_verify_checksums": ([ptr, byte], None),
                "create_iterator": ([ptr, ptr], ptr),
                "iter_destroy": ([ptr], None),
                "iter_seek": ([ptr, text, size], None),
                "iter_valid": ([ptr], byte),
                "iter_next": ([ptr], None),
                "iter_key": ([ptr, C.POINTER(size)], ptr),
                "iter_value": ([ptr, C.POINTER(size)], ptr),
                "iter_get_error": ([ptr, error], None),
            }
            for name, (arguments, result) in signatures.items():
                function = getattr(self.library, "rocksdb_" + name)
                function.argtypes = arguments
                function.restype = result
                setattr(self, name, function)
        except (OSError, AttributeError) as exc:
            raise NativeError(
                "native_runtime_unavailable",
                f"Could not load the RocksDB C API: {exc}",
                operation="load_native_runtime",
                path=candidate,
            ) from exc

    def check(self, error: C.c_void_p) -> None:
        if error.value:
            try:
                message = C.string_at(error.value).decode("utf-8", errors="replace")
            finally:
                self.free(error.value)
            raise NativeError(
                "native_data_unavailable",
                f"RocksDB could not read the copy: {message}",
                operation="read_native_copy",
                path=self.path,
            )

    # C functions are installed above with their exact signatures.
    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)


def runtime_status() -> dict[str, object]:
    """Probe library availability without opening a database or creating files."""
    try:
        runtime = NativeRuntime()
    except NativeError as exc:
        return {"available": False, "code": exc.code, "message": str(exc), **exc.details}
    # The C API has no version accessor. A filename is a hint, not verification.
    version = re.search(r"(?:rocksdb[.-])([0-9]+(?:\.[0-9]+)+)", runtime.path)
    return {
        "available": True,
        "library": runtime.path,
        "version": None,
        "filename_version_hint": version.group(1) if version else None,
        "version_status": "not-exposed-by-c-api",
        "database_opened": False,
    }


def _signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _inventory(path: Path) -> dict[str, tuple[int, int, int, int, int, int]]:
    if path.is_symlink() or not path.is_dir():
        raise NativeError(
            "native_data_unavailable",
            "The copied native store is missing or unsafe.",
            operation="inspect_native_store",
            path=path,
        )
    entries = {}
    for child in sorted(path.iterdir()):
        info = child.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise NativeError(
                "native_data_unavailable",
                "The native store contains a symlink or nonregular entry.",
                operation="inspect_native_store",
                path=child,
            )
        entries[child.name] = _signature(info)
    if not entries or "CURRENT" not in entries:
        raise NativeError(
            "native_data_unavailable",
            "The copied native store has no CURRENT file.",
            operation="inspect_native_store",
            path=path,
        )
    return entries


def _read_small_regular(path: Path, expected: tuple[int, ...], limit: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        if _signature(os.fstat(fd)) != expected or expected[3] > limit:
            raise ValueError("Native metadata changed or exceeds its read budget")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if (
            len(data) != expected[3]
            or _signature(os.fstat(fd)) != expected
            or _signature(path.lstat()) != expected
        ):
            raise ValueError("Native metadata changed while reading")
        return data
    except (OSError, ValueError) as exc:
        raise NativeError(
            "native_data_unavailable", str(exc), operation="inspect_native_store", path=path
        ) from exc
    finally:
        os.close(fd)


def _validate_options(source: Path, entries: Mapping[str, tuple[int, ...]]) -> None:
    for name, signature in entries.items():
        if not name.startswith("OPTIONS-"):
            continue
        if signature[3] > 4 * 1024 * 1024:
            raise NativeError(
                "native_data_unavailable",
                "Native options exceed the safety limit.",
                operation="inspect_native_options",
                path=source / name,
            )
        # Only inspect compatibility. Never pass copied options to RocksDB.
        raw_options = _read_small_regular(source / name, signature, 4 * 1024 * 1024)
        for line in raw_options.decode("utf-8", errors="strict").splitlines():
            key, separator, value = line.strip().partition("=")
            key, value = key.strip(), value.strip()
            if separator and (
                (key == "comparator" and value != "leveldb.BytewiseComparator")
                or (
                    key in {"merge_operator", "prefix_extractor"}
                    and value not in {"nullptr", "null", ""}
                )
            ):
                raise NativeError(
                    "native_data_unavailable",
                    "Unsupported native comparator, merge operator, or prefix extractor.",
                    operation="inspect_native_options",
                    path=source / name,
                )


@contextmanager
def _scratch_store(source: Path, snapshot_root: Path) -> Iterator[Path]:
    before = _inventory(source)
    current = _read_small_regular(source / "CURRENT", before["CURRENT"], 4096)
    if (
        not re.fullmatch(rb"MANIFEST-[0-9]+\n", current)
        or current[:-1].decode("ascii") not in before
    ):
        raise NativeError(
            "native_data_unavailable",
            "The copied native CURRENT file does not name a local manifest.",
            operation="inspect_native_store",
            path=source,
        )
    _validate_options(source, before)
    configured = os.environ.get("GOODNOTES_SCRATCH_DIR")
    scratch_parent = Path(configured).expanduser() if configured else Path(tempfile.gettempdir())
    try:
        if scratch_parent.is_symlink():
            raise OSError("The scratch directory must not be a symlink")
        scratch_parent = scratch_parent.resolve(strict=True)
        _require_not_production(scratch_parent, purpose="native scratch")
        if scratch_parent == snapshot_root or scratch_parent.is_relative_to(snapshot_root):
            raise OSError("The scratch directory must stay outside the snapshot")
        temporary = tempfile.TemporaryDirectory(prefix="goodnotes-native-", dir=scratch_parent)
    except OSError as exc:
        raise NativeError(
            "native_data_unavailable",
            f"Could not create private native scratch: {exc}",
            operation="create_native_scratch",
            path=scratch_parent,
        ) from exc
    try:
        target = Path(temporary.name) / "store"
        target.mkdir(mode=0o700)
        copy_budget = _MAX_COPY_FALLBACK
        for name, expected in before.items():
            fd = os.open(
                source / name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                info = os.fstat(fd)
                if _signature(info) != expected:
                    raise NativeError(
                        "native_data_unavailable",
                        "The snapshot native store changed during capture.",
                        operation="clone_native_store",
                        path=source,
                    )
                copied = target / name
                try:
                    cloned_ok = _clone_file_from_fd(fd, copied)
                except OSError as exc:
                    if exc.errno not in {
                        errno.EXDEV,
                        errno.ENOTSUP,
                        errno.EOPNOTSUPP,
                        errno.ENOSYS,
                        errno.EINVAL,
                    }:
                        raise NativeError(
                            "native_data_unavailable",
                            f"Could not clone native scratch: {exc}",
                            operation="clone_native_store",
                            path=copied,
                        ) from exc
                    cloned_ok = False
                if not cloned_ok:
                    if expected[3] > copy_budget:
                        raise NativeError(
                            "native_data_unavailable",
                            "Native scratch requires "
                            "copy-on-write support for stores larger than 64 MiB.",
                            operation="clone_native_store",
                            path=source,
                        )
                    copy_budget -= expected[3]
                    with os.fdopen(os.dup(fd), "rb") as reader, copied.open("xb") as writer:
                        shutil.copyfileobj(reader, writer)
                copied.chmod(0o600)
                cloned = copied.stat()
                if (
                    (cloned.st_dev, cloned.st_ino) == (info.st_dev, info.st_ino)
                    or cloned.st_size != info.st_size
                    or _signature(os.fstat(fd)) != expected
                ):
                    raise NativeError(
                        "native_data_unavailable",
                        "Native scratch is not an independent stable copy.",
                        operation="clone_native_store",
                        path=source,
                    )
            finally:
                os.close(fd)
        if _inventory(source) != before:
            raise NativeError(
                "native_data_unavailable",
                "The native snapshot changed during capture.",
                operation="clone_native_store",
                path=source,
            )
        for options in target.glob("OPTIONS-*"):
            options.unlink()
        yield target
    finally:
        temporary.cleanup()
        if _inventory(source) != before:
            raise NativeError(
                "native_data_unavailable",
                "The native snapshot changed while reading.",
                operation="read_native_copy",
                path=source,
            )


def read_page_records(sandbox: SnapshotProtocol, root_uuid: str) -> list[tuple[bytes, bytes]]:
    """Return bounded raw records. The engine only opens a disposable scratch copy."""
    validated = Sandbox.load(sandbox.root)
    if Path(sandbox.library_dir).resolve() != validated.library_dir:
        raise NativeError(
            "native_data_unavailable",
            "The native library does not match its snapshot.",
            operation="resolve_native_snapshot",
            path=sandbox.library_dir,
        )
    source = validated.library_dir / "Databases" / "notes_main.db"
    runtime = NativeRuntime()
    with _scratch_store(source, validated.root) as scratch:
        options = runtime.options_create()
        db = read_options = iterator = None
        try:
            runtime.options_set_create_if_missing(options, 0)
            runtime.options_set_paranoid_checks(options, 1)
            runtime.options_set_max_open_files(options, 64)
            runtime.options_set_db_log_dir(options, os.fsencode(scratch.parent))
            runtime.options_set_wal_dir(options, os.fsencode(scratch))
            error = C.c_void_p()
            db = runtime.open_for_read_only(options, os.fsencode(scratch), 0, C.byref(error))
            runtime.check(error)
            if not db:
                raise NativeError(
                    "native_data_unavailable",
                    "Native read-only open returned no database.",
                    operation="read_native_copy",
                    path=source,
                )
            read_options = runtime.readoptions_create()
            runtime.readoptions_set_fill_cache(read_options, 0)
            runtime.readoptions_set_verify_checksums(read_options, 1)
            iterator = runtime.create_iterator(db, read_options)
            prefix = root_uuid.encode("ascii") + b"."
            runtime.iter_seek(iterator, prefix, len(prefix))
            records: list[tuple[bytes, bytes]] = []
            total = 0
            while runtime.iter_valid(iterator):
                key_size, value_size = C.c_size_t(), C.c_size_t()
                key_ptr = runtime.iter_key(iterator, C.byref(key_size))
                if key_size.value > 65_536:
                    raise ValueError("Native key exceeds the extraction budget")
                key = C.string_at(key_ptr, key_size.value)
                if not key.startswith(prefix):
                    break
                value_ptr = runtime.iter_value(iterator, C.byref(value_size))
                total += key_size.value + value_size.value
                if (
                    value_size.value > _MAX_VALUE
                    or total > _MAX_PAGE_BYTES
                    or len(records) >= _MAX_PAGE_RECORDS
                ):
                    raise ValueError("Native page exceeds the extraction budget")
                records.append((key, C.string_at(value_ptr, value_size.value)))
                runtime.iter_next(iterator)
            error = C.c_void_p()
            runtime.iter_get_error(iterator, C.byref(error))
            runtime.check(error)
            return records
        except (OSError, ValueError) as exc:
            raise NativeError(
                "native_data_unavailable", str(exc), operation="read_native_copy", path=source
            ) from exc
        finally:
            if iterator:
                runtime.iter_destroy(iterator)
            if read_options:
                runtime.readoptions_destroy(read_options)
            if db:
                runtime.close(db)
            runtime.options_destroy(options)
