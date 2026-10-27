"""Read native bytes from disposable independent copies of validated snapshots."""

from __future__ import annotations

import ctypes as C
import errno
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, Protocol

from ..storage.capture.copy import _clone_file_from_fd
from ..storage.paths import _require_not_production, path_contains, stat_key
from ..storage.sandbox import Sandbox
from .errors import NativeError
from .runtime import NativeRuntime

_MAX_VALUE = 16 * 1024 * 1024
_MAX_PAGE_BYTES = 64 * 1024 * 1024
_MAX_PAGE_RECORDS = 100_000
_MAX_COPY_FALLBACK = 64 * 1024 * 1024


class SnapshotProtocol(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...


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
        entries[child.name] = stat_key(info)
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
        if stat_key(os.fstat(fd)) != expected or expected[3] > limit:
            raise ValueError("Native metadata changed or exceeds its read budget")
        with os.fdopen(os.dup(fd), "rb") as stream:
            data = stream.read(limit + 1)
        if (
            len(data) != expected[3]
            or stat_key(os.fstat(fd)) != expected
            or stat_key(path.lstat()) != expected
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
        try:
            options_text = raw_options.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise NativeError(
                "native_data_unavailable",
                "Native options are not valid UTF-8.",
                operation="inspect_native_options",
                path=source / name,
                context={
                    "encoding": exc.encoding,
                    "encoding_start": exc.start,
                    "encoding_end": exc.end,
                },
            ) from exc
        for line in options_text.splitlines():
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


def _validated_snapshot_store(sandbox: SnapshotProtocol) -> tuple[Sandbox, Path]:
    validated = Sandbox.load(sandbox.root)
    if Path(sandbox.library_dir).resolve() != validated.library_dir:
        raise NativeError(
            "native_data_unavailable",
            "The native library does not match its snapshot.",
            operation="resolve_native_snapshot",
            path=sandbox.library_dir,
        )
    return validated, validated.library_dir / "Databases" / "notes_main.db"


def _validated_inventory(source: Path) -> dict[str, tuple[int, int, int, int, int, int]]:
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
    return before


def native_store_fingerprint(
    sandbox: SnapshotProtocol,
) -> dict[str, tuple[int, int, int, int, int, int]]:
    """Validate the copied native store and return metadata for every regular file."""
    _, source = _validated_snapshot_store(sandbox)
    return _validated_inventory(source)


@contextmanager
def _scratch_store(
    source: Path, snapshot_root: Path, scratch_dir: Path | str | None = None
) -> Generator[Path, None, None]:
    before = _validated_inventory(source)
    configured = scratch_dir if scratch_dir is not None else os.environ.get("GOODNOTES_SCRATCH_DIR")
    scratch_parent = (
        Path(configured).expanduser() if configured is not None else Path(tempfile.gettempdir())
    )
    try:
        if scratch_parent.is_symlink():
            raise OSError("The scratch directory must not be a symlink")
        scratch_parent = scratch_parent.resolve(strict=True)
        _require_not_production(scratch_parent, purpose="native scratch")
        if path_contains(snapshot_root, scratch_parent):
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
            # Validate the copied options above, but never copy them into engine scratch.
            if name.startswith("OPTIONS-"):
                continue
            fd = os.open(
                source / name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
            )
            try:
                info = os.fstat(fd)
                if stat_key(info) != expected:
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
                    or stat_key(os.fstat(fd)) != expected
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


class NativeStoreSession:
    """Own one validated disposable native-store copy and engine for an operation."""

    def __init__(self, sandbox: SnapshotProtocol, *, scratch_dir: Path | str | None = None) -> None:
        self._sandbox = sandbox
        self._scratch_dir = scratch_dir
        self._scratch_context: Any = None
        self._runtime: Any = None
        self._scratch: Path | None = None
        self._options: Any = None
        self._db: Any = None
        self._read_options: Any = None
        self._iterator: Any = None
        self._entered = False
        self._active = False

    def __enter__(self) -> NativeStoreSession:
        if self._entered:
            raise RuntimeError("A NativeStoreSession can only be entered once.")
        self._entered = True
        try:
            validated, source = _validated_snapshot_store(self._sandbox)
            self._scratch_context = _scratch_store(
                source, validated.root, scratch_dir=self._scratch_dir
            )
            self._scratch = self._scratch_context.__enter__()
            self._runtime = NativeRuntime()

            runtime = self._runtime
            self._options = runtime.options_create()
            if not self._options:
                raise NativeError(
                    "native_data_unavailable",
                    "RocksDB could not create read-only options.",
                    operation="read_native_copy",
                    path=source,
                )
            runtime.options_set_create_if_missing(self._options, 0)
            runtime.options_set_paranoid_checks(self._options, 1)
            runtime.options_set_max_open_files(self._options, 64)
            runtime.options_set_db_log_dir(self._options, os.fsencode(self._scratch.parent))
            runtime.options_set_wal_dir(self._options, os.fsencode(self._scratch))

            error = C.c_void_p()
            self._db = runtime.open_for_read_only(
                self._options, os.fsencode(self._scratch), 0, C.byref(error)
            )
            runtime.check(error)
            if not self._db:
                raise NativeError(
                    "native_data_unavailable",
                    "Native read-only open returned no database.",
                    operation="read_native_copy",
                    path=source,
                )

            self._read_options = runtime.readoptions_create()
            if not self._read_options:
                raise NativeError(
                    "native_data_unavailable",
                    "RocksDB could not create read options.",
                    operation="read_native_copy",
                    path=source,
                )
            runtime.readoptions_set_fill_cache(self._read_options, 0)
            runtime.readoptions_set_verify_checksums(self._read_options, 1)
            self._iterator = runtime.create_iterator(self._db, self._read_options)
            if not self._iterator:
                raise NativeError(
                    "native_data_unavailable",
                    "RocksDB could not create a native record iterator.",
                    operation="read_native_copy",
                    path=source,
                )
            self._active = True
            return self
        except BaseException as exc:
            self._release_runtime(suppress_errors=True)
            scratch_context, self._scratch_context = self._scratch_context, None
            if scratch_context is not None:
                try:
                    scratch_context.__exit__(type(exc), exc, exc.__traceback__)
                except BaseException as cleanup_exc:
                    raise cleanup_exc from exc
            raise

    def read_page_records(self, root_uuid: str) -> list[tuple[bytes, bytes]]:
        """Return bounded page records; an empty list means a successful negative lookup.

        Store availability and iterator failures raise instead of returning no records.
        This observation is limited to the requested namespace in the copied store.
        """
        if not self._active or self._runtime is None or self._iterator is None:
            raise RuntimeError("NativeStoreSession must be active to read page records.")
        runtime = self._runtime
        source = self._sandbox.library_dir / "Databases" / "notes_main.db"
        try:
            prefix = root_uuid.encode("ascii") + b"."
            runtime.iter_seek(self._iterator, prefix, len(prefix))
            records: list[tuple[bytes, bytes]] = []
            total = 0
            while runtime.iter_valid(self._iterator):
                key_size, value_size = C.c_size_t(), C.c_size_t()
                key_ptr = runtime.iter_key(self._iterator, C.byref(key_size))
                if key_size.value > 65_536:
                    raise ValueError("Native key exceeds the extraction budget")
                key = C.string_at(key_ptr, key_size.value)
                if not key.startswith(prefix):
                    break
                value_ptr = runtime.iter_value(self._iterator, C.byref(value_size))
                total += key_size.value + value_size.value
                if (
                    value_size.value > _MAX_VALUE
                    or total > _MAX_PAGE_BYTES
                    or len(records) >= _MAX_PAGE_RECORDS
                ):
                    raise ValueError("Native page exceeds the extraction budget")
                records.append((key, C.string_at(value_ptr, value_size.value)))
                runtime.iter_next(self._iterator)
            error = C.c_void_p()
            runtime.iter_get_error(self._iterator, C.byref(error))
            runtime.check(error)
            return records
        except (OSError, ValueError) as exc:
            raise NativeError(
                "native_data_unavailable", str(exc), operation="read_native_copy", path=source
            ) from exc

    def _release_runtime(self, *, suppress_errors: bool = False) -> None:
        runtime = self._runtime
        if runtime is None:
            return
        resources = (
            ("iter_destroy", self._iterator),
            ("readoptions_destroy", self._read_options),
            ("close", self._db),
            ("options_destroy", self._options),
        )
        self._iterator = self._read_options = self._db = self._options = None
        failure: BaseException | None = None
        for name, resource in resources:
            if resource:
                try:
                    getattr(runtime, name)(resource)
                except BaseException as exc:
                    if failure is None:
                        failure = exc
        if failure is not None and not suppress_errors:
            raise failure

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        self._active = False
        cleanup_error: BaseException | None = None
        try:
            self._release_runtime()
        except BaseException as cleanup_exc:
            cleanup_error = cleanup_exc
        scratch_context, self._scratch_context = self._scratch_context, None
        if scratch_context is not None:
            try:
                scratch_context.__exit__(exc_type, exc, traceback)
            except BaseException as scratch_exc:
                if cleanup_error is None:
                    cleanup_error = scratch_exc
        if cleanup_error is not None:
            raise cleanup_error
        return False


def read_page_records(sandbox: SnapshotProtocol, root_uuid: str) -> list[tuple[bytes, bytes]]:
    """Compatibility helper that reads one page using an operation-scoped session."""
    with NativeStoreSession(sandbox) as session:
        return session.read_page_records(root_uuid)
