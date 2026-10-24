"""Read-only checks for the paths selected by a CLI process."""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path
from typing import Any

from .cache import read_view
from .config import Settings
from .errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    _permission_denied,
)


def _failure(
    checks: list[dict[str, Any]],
    *,
    name: str,
    path: Path,
    operation: str,
    kind: str,
    exc: BaseException,
    required: bool = True,
) -> None:
    if isinstance(exc, FileNotFoundError):
        status = "missing"
    elif _permission_denied(exc):
        status = "denied"
    else:
        status = "invalid"
    if isinstance(exc, GoodnotesError) and hasattr(exc, "details"):
        error = exc
        if getattr(exc, "code", None) == "cache_not_initialized":
            status = "missing"
        elif str(getattr(exc, "code", "")).endswith("permission_denied"):
            status = "denied"
    elif kind in {"source", "output"}:
        error = FilesystemAccessError.from_exception(
            exc,
            operation=operation,
            path=path,
            kind="source" if kind == "source" else "output",
        )
    else:
        error = CacheAccessError.from_exception(
            exc,
            f"Could not inspect the selected cache path: {path}",
            operation=operation,
            path=path,
            missing_is_uninitialized=True,
        )
    details = getattr(error, "details", {})
    checks.append(
        {
            "name": name,
            "path": details.get("path", str(path)),
            "status": status,
            "required": required,
            "operation": details.get("operation", operation),
            "code": getattr(error, "code", "operation_failed"),
            "message": str(exc),
            "remedy": details.get("remedy", "Inspect the selected path and retry."),
        }
    )


def _probe(
    checks: list[dict[str, Any]],
    *,
    name: str,
    path: Path,
    directory: bool,
    kind: str = "cache",
    required: bool = True,
) -> bool:
    operation = f"inspect_{name}"
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode):
            raise ValueError("The selected path must not be a symlink.")
        if directory:
            if not stat.S_ISDIR(before.st_mode):
                raise ValueError("The selected path is not a directory.")
            # Iteration proves directory-read access without changing permissions or
            # creating probe files. Child probes establish search/read access too.
            with os.scandir(path) as entries:
                next(entries, None)
        else:
            if not stat.S_ISREG(before.st_mode):
                raise ValueError("The selected path is not a regular file.")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(path, flags)
            try:
                opened = os.fstat(descriptor)
                if not stat.S_ISREG(opened.st_mode) or (before.st_dev, before.st_ino) != (
                    opened.st_dev,
                    opened.st_ino,
                ):
                    raise ValueError("The selected file changed while being opened.")
                os.read(descriptor, 1)
            finally:
                os.close(descriptor)
        after = path.lstat()
        if (before.st_dev, before.st_ino, before.st_mode) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
        ):
            raise ValueError("The selected path changed during the access check.")
    except (OSError, ValueError) as exc:
        _failure(
            checks,
            name=name,
            path=path,
            operation=operation,
            kind=kind,
            exc=exc,
            required=required,
        )
        return False
    checks.append(
        {
            "name": name,
            "path": str(path),
            "status": "ok",
            "required": required,
            "operation": operation,
        }
    )
    return True


def _probe_writable_directory(checks: list[dict[str, Any]], *, name: str, path: Path) -> None:
    if not _probe(checks, name=name, path=path, directory=True, kind="output"):
        return
    check = checks[-1]
    # This is deliberately only an access hint. Creating a test file would turn
    # access diagnostics into a write operation and can fail in a restricted host.
    writable = os.access(path, os.W_OK | os.X_OK)
    check["write_access_hint"] = writable
    check["write_tested"] = False
    if not writable:
        error = FilesystemAccessError.from_exception(
            PermissionError("Write/search access is not indicated by os.access."),
            operation=f"inspect_{name}",
            path=path,
            kind="output",
        )
        check.update(
            status="denied",
            code=error.code,
            message="The directory is readable, but its write/search access check failed.",
            remedy=error.details["remedy"],
        )


def inspect_environment(
    data_dir: Path,
    library: Path | None = None,
    *,
    sandbox: Path | None = None,
    scratch_dir: Path | None = None,
    output_dir: Path | None = None,
) -> dict[str, Any]:
    """Probe selected cache/source paths without writes or source database opens.

    A source Library is inspected only when the caller explicitly supplies it.
    This establishes point-in-time filesystem access, not database compatibility,
    source freshness, actual write success, or native annotation completeness.
    """

    state = Path(data_dir).expanduser().absolute()
    source = Path(library).expanduser().absolute() if library is not None else None
    explicit = Path(sandbox).expanduser().absolute() if sandbox is not None else None
    scratch = (
        Path(scratch_dir or os.environ.get("GOODNOTES_SCRATCH_DIR") or tempfile.gettempdir())
        .expanduser()
        .absolute()
    )
    output = Path(output_dir).expanduser().absolute() if output_dir is not None else None
    checks: list[dict[str, Any]] = []
    warnings = [
        "These checks establish point-in-time read access. Write access is an os.access hint; "
        "no probe files are created. The checks do not establish actual write success, "
        "database compatibility, current iPad sync, or annotation completeness."
    ]
    selected_cache: str | None = str(state) if explicit is None else None
    cache_ready = explicit is not None or _probe(
        checks, name="cache_directory", path=state, directory=True
    )
    if cache_ready:
        if explicit is None:
            _probe(checks, name="view_lock", path=state / ".view.lock", directory=False)
            _probe(checks, name="active_pointer", path=state / "active.json", directory=False)
        cfg = Settings(data_dir=state, sandbox=explicit)
        try:
            # Use the normal existing shared lock. This does not initialize a cache
            # or create a missing lock, and protects the selected view from pruning.
            with read_view(cfg) as selected:
                selected_cache = str(cfg.cache_dir) if cfg.cache_dir is not None else None
                checks.append(
                    {
                        "name": "snapshot",
                        "status": "ok",
                        "path": str(selected.root),
                        "required": True,
                        "operation": "resolve_read_view",
                        "captured_at": selected.captured_at,
                    }
                )
                _probe(
                    checks,
                    name="projection_database",
                    path=selected.projection_path,
                    directory=False,
                )
                if selected.fts_path is not None:
                    _probe(
                        checks,
                        name="search_database",
                        path=selected.fts_path,
                        directory=False,
                        required=False,
                    )
                _probe(
                    checks,
                    name="attachments_directory",
                    path=selected.attachments_dir,
                    directory=True,
                )
                _probe(
                    checks,
                    name="native_store",
                    path=selected.library_dir / "Databases" / "notes_main.db",
                    directory=True,
                    required=False,
                )
        except (OSError, GoodnotesError, ValueError, RuntimeError) as exc:
            _failure(
                checks,
                name="snapshot",
                path=explicit or state,
                operation="resolve_read_view",
                kind="cache",
                exc=exc,
            )
    if source is not None and _probe(
        checks, name="source_library", path=source, directory=True, kind="source"
    ):
        source_databases_ready = _probe(
            checks,
            name="source_databases",
            path=source / "Databases",
            directory=True,
            kind="source",
        )
        if source_databases_ready:
            _probe(
                checks,
                name="source_projection",
                path=source / "Databases" / "projection.sqlite",
                directory=False,
                kind="source",
            )
        _probe(
            checks,
            name="source_attachments",
            path=source / "Attachments",
            directory=True,
            kind="source",
        )
        if source_databases_ready:
            _probe(
                checks,
                name="source_native_store",
                path=source / "Databases" / "notes_main.db",
                directory=True,
                kind="source",
                required=False,
            )
    _probe_writable_directory(checks, name="scratch_directory", path=scratch)
    if output is not None:
        _probe_writable_directory(checks, name="output_directory", path=output)
    errors = [check for check in checks if check["required"] and check["status"] != "ok"]
    return {
        "scope": "filesystem-access",
        "cache_dir": selected_cache,
        "sandbox": str(explicit) if explicit is not None else None,
        "library": str(source) if source is not None else None,
        "scratch_dir": str(scratch),
        "output_dir": str(output) if output is not None else None,
        "status": "issues" if errors else "ready",
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
    }
