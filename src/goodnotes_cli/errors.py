"""Expected failures that the CLI presents without a traceback."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from typing import Literal


class GoodnotesError(Exception):
    """A library, input, or capability error with a user-facing explanation."""


CacheAccessCode = Literal[
    "cache_not_initialized",
    "cache_permission_denied",
    "cache_invalid",
]


def _os_cause(exc: BaseException) -> OSError | None:
    """Return the underlying filesystem error from one existing exception chain."""

    cause: BaseException | None = exc
    while cause is not None and not isinstance(cause, OSError):
        cause = cause.__cause__
    return cause


def _permission_denied(exc: BaseException) -> bool:
    cause = _os_cause(exc)
    return isinstance(cause, PermissionError) or (
        cause is not None and cause.errno in {errno.EACCES, errno.EPERM}
    )


class FilesystemAccessError(GoodnotesError):
    """A source-read or output-write failure with a concrete recovery action."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        operation: str,
        path: str | Path,
        remedy: str,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = {"operation": operation, "path": str(path), "remedy": remedy}

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        *,
        operation: str,
        path: str | Path,
        kind: Literal["source", "output"],
        message: str | None = None,
    ) -> FilesystemAccessError:
        cause = _os_cause(exc)
        failed_path = getattr(cause, "filename", None) if cause is not None else None
        if isinstance(failed_path, bytes):
            failed_path = os.fsdecode(failed_path)
        permission_denied = _permission_denied(exc)
        code = f"{kind}_{'permission_denied' if permission_denied else 'unavailable'}"
        if kind == "source":
            remedy = (
                "Grant this CLI process read access to the selected Library and search access "
                "to its parent directories. A sandboxed agent may need access granted by its "
                "host. Keep Goodnotes source permissions unchanged and use the same --library "
                "selection when retrying."
                if permission_denied
                else "Check that the selected Library exists and is readable, then retry "
                "with the same --library selection."
            )
        else:
            remedy = (
                "Choose an output directory writable by this CLI process, or grant it "
                "write/search access to that directory. A sandboxed agent may need access "
                "granted by its host. Keep the snapshot and Goodnotes source unchanged."
                if permission_denied
                else "Check that the output parent exists and is writable, then retry with "
                "a new output path outside the snapshot and Goodnotes source."
            )
        return cls(
            code,
            message or f"Could not access {kind} path: {failed_path or path}",
            operation=operation,
            path=failed_path or path,
            remedy=remedy,
        )


class CacheAccessError(GoodnotesError):
    """A cache access failure with structured operation and recovery context."""

    def __init__(
        self,
        code: CacheAccessCode,
        message: str,
        *,
        operation: str,
        path: str | Path,
        cache_dir: str | Path | None = None,
        writer: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        same_cache = (
            f"the same cache directory ({cache_dir})"
            if cache_dir is not None
            else "the same --data-dir, GOODNOTES_DATA_DIR, or --sandbox setting"
        )
        if code == "cache_permission_denied":
            remedy = (
                f"Run sync as the cache owner with {same_cache}, or restore write/search access."
                if writer
                else f"Run the read command as the cache owner or grant read/search access "
                f"to {same_cache}."
            )
        elif code == "cache_not_initialized":
            remedy = (
                f"After stopping other Goodnotes CLI processes, run 'goodnotes sync' with "
                f"{same_cache} "
                "to initialize the persistent lock. Do not create or unlink lock files manually."
                if "lock" in operation
                else f"Run 'goodnotes sync' with {same_cache} to create a local read view."
            )
        elif "lock" in operation:
            remedy = (
                f"Stop all Goodnotes CLI processes, restore a regular persistent lock, then run "
                f"'goodnotes sync' with {same_cache}. Never replace the lock while readers may be "
                "active."
            )
        else:
            remedy = (
                f"Inspect or restore this cache entry, then run 'goodnotes sync' with "
                f"{same_cache}. "
                "If the active pointer remains invalid, use a clean cache directory."
            )
        self.details = {
            "operation": operation,
            "path": str(path),
            "remedy": remedy,
        }
        if cache_dir is not None:
            self.details["cache_dir"] = str(cache_dir)

    @classmethod
    def from_exception(
        cls,
        exc: BaseException,
        message: str,
        *,
        operation: str,
        path: str | Path,
        cache_dir: str | Path | None = None,
        missing_is_uninitialized: bool = False,
        writer: bool = False,
    ) -> CacheAccessError:
        """Classify one cache setup, lock, or pointer failure and its cause chain."""

        os_error = _os_cause(exc)
        if _permission_denied(exc):
            code: CacheAccessCode = "cache_permission_denied"
        elif isinstance(os_error, FileNotFoundError) and missing_is_uninitialized:
            code = "cache_not_initialized"
        else:
            code = "cache_invalid"
        failed_path = getattr(os_error, "filename", None) if os_error is not None else None
        if isinstance(failed_path, bytes):
            failed_path = os.fsdecode(failed_path)
        return cls(
            code,
            message,
            operation=operation,
            path=failed_path or path,
            cache_dir=cache_dir,
            writer=writer,
        )


class StaleSnapshotError(GoodnotesError):
    """A continuation expects a different generation than the active read view."""

    code = "stale_snapshot"


class TextIndexUnavailableError(GoodnotesError):
    """The requested stored text index cannot answer this search request."""
