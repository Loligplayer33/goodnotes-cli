"""Expected failures that the CLI presents without a traceback."""

from __future__ import annotations

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
        only_if_os_error: bool = False,
    ) -> CacheAccessError | None:
        """Classify one bounded cache setup/lock/pointer failure and its cause chain."""

        cause: BaseException | None = exc
        while cause is not None and not isinstance(cause, OSError):
            cause = cause.__cause__
        os_error = cause
        if os_error is None and only_if_os_error:
            return None
        if isinstance(os_error, PermissionError):
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
