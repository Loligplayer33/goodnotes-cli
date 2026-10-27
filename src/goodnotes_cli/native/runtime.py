"""Optional RocksDB C API discovery and bindings."""

from __future__ import annotations

import ctypes as C
import ctypes.util
import os
import re
from pathlib import Path
from typing import Any

from .errors import NativeError


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
