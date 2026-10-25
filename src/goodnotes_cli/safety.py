"""Read-only sandbox validation and safe output-path checks."""

from __future__ import annotations

import errno
import json
import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import CacheAccessError, FilesystemAccessError, GoodnotesError, _permission_denied

SNAPSHOT_MARKER = "goodnotes-snapshot.json"
_SQLITE_SIDECAR_PREFIX = "-"


def _is_production_goodnotes_path(path: Path) -> bool:
    """Recognize macOS Goodnotes app containers from a path without opening it."""

    parts = [part.casefold() for part in path.parts]
    for index, part in enumerate(parts[:-1]):
        if part in {"containers", "group containers"} and "goodnotes" in parts[index + 1]:
            return True
        if part == "application support" and parts[index + 1] in {"goodnotes", "goodnotes 6"}:
            return True
    return False


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def stat_key(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the shared identity and freshness signature for a filesystem entry."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _lexically_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _samefile(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return False
        raise


def path_contains(parent: str | Path, child: str | Path) -> bool:
    """Check path containment using resolved paths and existing directory identity.

    Lexical ancestry handles missing descendants. When spellings differ, an existing
    ancestor is compared by filesystem identity so case aliases on insensitive volumes
    are recognized without treating case variants as aliases on sensitive volumes.
    """

    resolved_parent = _resolved(parent)
    resolved_child = _resolved(child)
    if _lexically_contains(resolved_parent, resolved_child):
        return True

    try:
        parent_info = resolved_parent.stat()
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return False
        raise
    if _samefile(resolved_parent, resolved_child):
        return True
    if not stat.S_ISDIR(parent_info.st_mode):
        return False

    for depth in range(1, len(resolved_child.parts) + 1):
        child_prefix = Path(*resolved_child.parts[:depth])
        if _samefile(resolved_parent, child_prefix):
            return True
    return False


def paths_overlap(left: str | Path, right: str | Path) -> bool:
    """Check whether two paths refer to overlapping filesystem locations."""

    return path_contains(left, right) or path_contains(right, left)


# Keep the old private names while existing package callers migrate to the shared API.
_contains = path_contains
_overlaps = paths_overlap


def safe_remove_tree(path: str | Path) -> None:
    """Remove a path tree without following symlinks or changing their targets.

    Directory traversal is anchored to open descriptors and uses ``O_NOFOLLOW``.
    Only opened directory descriptors are made owner-writable; every non-directory
    entry, including a symlink, is unlinked as an entry in its containing directory.
    Filesystem errors are allowed to propagate so callers can preserve their existing
    source/cache permission classification.
    """

    target = Path(path).expanduser()
    if target.name in {"", ".", ".."}:
        raise GoodnotesError(f"Refusing to remove an unsafe filesystem root: {target}")

    try:
        requested_parent_info = target.parent.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(requested_parent_info.st_mode):
        raise GoodnotesError(f"Refusing to clean through a non-directory parent: {target.parent}")

    try:
        parent = target.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise GoodnotesError(f"Filesystem parent changed during cleanup: {target.parent}") from exc

    expected_parent = parent.lstat()
    if not stat.S_ISDIR(expected_parent.st_mode):
        raise GoodnotesError(f"Refusing to clean through a non-directory parent: {parent}")
    if stat_key(expected_parent)[:2] != stat_key(requested_parent_info)[:2]:
        raise GoodnotesError(f"Filesystem parent changed during cleanup: {parent}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, directory_flags)
    try:
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat_key(opened_parent)[:2] != stat_key(expected_parent)[:2]
        ):
            raise GoodnotesError(f"Filesystem parent changed during cleanup: {parent}")
        _remove_entry_at(parent_fd, target.name, target)
    finally:
        os.close(parent_fd)


def _remove_entry_at(parent_fd: int, name: str, display_path: Path) -> None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return

    if not stat.S_ISDIR(before.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode) or stat_key(opened)[:2] != stat_key(before)[:2]:
            raise GoodnotesError(f"Filesystem entry changed during cleanup: {display_path}")

        writable_mode = opened.st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if writable_mode != opened.st_mode:
            os.fchmod(directory_fd, writable_mode)

        for child_name in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, child_name, display_path / child_name)

        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_key(current)[:2] != stat_key(opened)[:2]:
            raise GoodnotesError(f"Filesystem entry changed during cleanup: {display_path}")
    finally:
        os.close(directory_fd)

    os.rmdir(name, dir_fd=parent_fd)


def resolve_confined_file(root: str | Path, candidate: str | Path) -> Path | None:
    """Resolve an existing regular file below ``root``.

    A missing candidate returns ``None``. An existing path that resolves outside the
    root, or to something other than a regular file, raises ``GoodnotesError``. OS
    errors such as permission failures propagate unchanged for caller classification.
    ``candidate`` may be absolute or relative to ``root``.
    """

    resolved_root = Path(root).expanduser().resolve(strict=True)
    raw_candidate = Path(candidate).expanduser()
    requested = raw_candidate if raw_candidate.is_absolute() else resolved_root / raw_candidate
    prospective = requested.resolve(strict=False)
    if not path_contains(resolved_root, prospective):
        raise GoodnotesError("The requested file resolves outside its allowed directory.")

    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not path_contains(resolved_root, resolved):
        raise GoodnotesError("The requested file resolves outside its allowed directory.")

    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise GoodnotesError("The requested path is not a regular file.")
    return resolved


def _require_not_production(path: Path, *, purpose: str) -> None:
    if _is_production_goodnotes_path(path):
        raise GoodnotesError(f"Refusing to use a production Goodnotes path for {purpose}: {path}")


def _reject_sqlite_sidecars(database_path: Path) -> None:
    """Reject every SQLite companion file so immutable reads cannot skip WAL data."""

    try:
        prefix = database_path.name + _SQLITE_SIDECAR_PREFIX
        sidecars = sorted(
            candidate
            for candidate in database_path.parent.iterdir()
            if candidate.name.startswith(prefix)
        )
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect SQLite sidecars for {database_path}") from exc
    if sidecars:
        names = ", ".join(sidecar.name for sidecar in sidecars)
        raise GoodnotesError(
            f"SQLite database has sidecars and must be refreshed before reading: {names}"
        )


@dataclass(frozen=True)
class Sandbox:
    """Validated paths for a marked, private Goodnotes library snapshot."""

    root: Path
    library_dir: Path
    projection_path: Path
    fts_path: Path | None
    attachments_dir: Path
    captured_at: str | None = None
    marker_data: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: str | Path) -> Sandbox:
        try:
            return cls._load(path)
        except CacheAccessError:
            raise
        except (OSError, GoodnotesError) as exc:
            if not _permission_denied(exc):
                raise
            raise CacheAccessError.from_exception(
                exc,
                f"Could not read the selected snapshot: {path}",
                operation="resolve_read_view",
                path=Path(path).expanduser(),
            ) from exc

    @classmethod
    def _load(cls, path: str | Path) -> Sandbox:
        requested_root = Path(path).expanduser()
        if requested_root.is_symlink():
            raise GoodnotesError("The snapshot root must not be a symlink.")
        try:
            root = requested_root.resolve(strict=True)
        except OSError as exc:
            raise GoodnotesError(f"Snapshot directory does not exist: {requested_root}") from exc
        _require_not_production(root, purpose="a sandbox")
        if not root.is_dir():
            raise GoodnotesError(f"Snapshot path is not a directory: {root}")

        marker = root / SNAPSHOT_MARKER
        if marker.is_symlink() or not marker.is_file():
            raise GoodnotesError(
                f"This directory is not a Goodnotes snapshot; missing {SNAPSHOT_MARKER}."
            )
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GoodnotesError(f"Snapshot marker is unreadable: {marker}") from exc
        if not isinstance(marker_data, dict) or marker_data.get("format_version") != 1:
            raise GoodnotesError(f"Unsupported or invalid snapshot marker: {marker}")
        captured_at = marker_data.get("created_at_utc")
        if captured_at is not None and not isinstance(captured_at, str):
            raise GoodnotesError(f"Snapshot marker has an invalid capture time: {marker}")

        library_dir = root / "Library"
        database_dir = library_dir / "Databases"
        projection_path = database_dir / "projection.sqlite"
        fts_path = database_dir / "fts.sqlite"
        attachments_dir = library_dir / "Attachments"

        for required_dir in (library_dir, database_dir, attachments_dir):
            if required_dir.is_symlink() or not required_dir.is_dir():
                raise GoodnotesError(f"Snapshot is missing a required directory: {required_dir}")
        if projection_path.is_symlink():
            raise GoodnotesError(f"Snapshot database must not be a symlink: {projection_path}")
        if not projection_path.is_file():
            raise GoodnotesError(f"Snapshot is missing its projection database: {projection_path}")
        if fts_path.is_symlink():
            raise GoodnotesError(f"Snapshot database must not be a symlink: {fts_path}")
        if fts_path.exists() and not fts_path.is_file():
            raise GoodnotesError(f"Snapshot search database is not a regular file: {fts_path}")
        _reject_sqlite_sidecars(projection_path)
        _reject_sqlite_sidecars(fts_path)

        return cls(
            root=root,
            library_dir=library_dir,
            projection_path=projection_path,
            fts_path=fts_path if fts_path.exists() else None,
            attachments_dir=attachments_dir,
            captured_at=captured_at,
            marker_data=dict(marker_data),
        )

    def ensure_output(
        self,
        path: str | Path,
        *,
        inputs: tuple[str | Path, ...] | list[str | Path] = (),
    ) -> Path:
        """Return an absolute output path after checking it cannot replace input data."""

        return safe_output(path, self, inputs=inputs)


def safe_output(
    path: str | Path,
    sandbox: Sandbox,
    *,
    inputs: tuple[str | Path, ...] | list[str | Path] = (),
) -> Path:
    """Validate a new output path, including paths reached through symlinked parents."""

    try:
        return _safe_output(path, sandbox, inputs=inputs)
    except OSError as exc:
        if not _permission_denied(exc):
            raise
        raise FilesystemAccessError.from_exception(
            exc, operation="validate_output", path=path, kind="output"
        ) from exc


def _safe_output(
    path: str | Path,
    sandbox: Sandbox,
    *,
    inputs: tuple[str | Path, ...] | list[str | Path] = (),
) -> Path:

    requested = Path(path).expanduser()
    resolved = _resolved(requested)
    for candidate in (requested.absolute(), resolved):
        _require_not_production(candidate, purpose="an output")
    if _overlaps(resolved, sandbox.root):
        raise GoodnotesError("Output paths must be outside the Goodnotes snapshot.")
    for input_path in inputs:
        resolved_input = _resolved(input_path)
        if _overlaps(resolved, resolved_input):
            raise GoodnotesError("Output paths must not overlap any input path.")

    if requested.exists() or requested.is_symlink():
        raise GoodnotesError(f"Refusing to overwrite an existing output: {requested}")
    if resolved.parent.exists() and not resolved.parent.is_dir():
        raise GoodnotesError(f"Output parent is not a directory: {resolved.parent}")
    return resolved


def _check_database_read_access(path: Path) -> None:
    """Classify SQLite open failures without adding I/O to successful reads."""

    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError as exc:
        raise CacheAccessError.from_exception(
            exc,
            f"Could not read snapshot database: {path}",
            operation="open_snapshot_database",
            path=path,
        ) from exc

    try:
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or not stat.S_ISREG(current.st_mode)
            or (
                opened.st_dev,
                opened.st_ino,
            )
            != (current.st_dev, current.st_ino)
        ):
            raise CacheAccessError(
                "cache_invalid",
                f"Snapshot database is not a stable regular file: {path}",
                operation="open_snapshot_database",
                path=path,
            )
    except OSError as exc:
        raise CacheAccessError.from_exception(
            exc,
            f"Could not verify snapshot database: {path}",
            operation="open_snapshot_database",
            path=path,
        ) from exc
    finally:
        os.close(descriptor)


@contextmanager
def open_database(path: str | Path) -> Generator[sqlite3.Connection, None, None]:
    """Open a snapshot SQLite file in immutable read-only mode."""

    try:
        connection = _open_database(path)
    except CacheAccessError:
        raise
    except (OSError, GoodnotesError) as exc:
        if not _permission_denied(exc):
            raise
        raise CacheAccessError.from_exception(
            exc,
            f"Could not read snapshot database: {path}",
            operation="open_snapshot_database",
            path=Path(path).expanduser(),
        ) from exc
    try:
        yield connection
    finally:
        connection.close()


def _open_database(path: str | Path) -> sqlite3.Connection:

    database_path = Path(path).expanduser()
    if database_path.is_symlink():
        raise GoodnotesError(f"Refusing to open a symlinked SQLite database: {database_path}")
    _require_not_production(database_path.absolute(), purpose="a SQLite connection")
    try:
        absolute_path = database_path.resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(f"SQLite database does not exist: {database_path}") from exc
    if not absolute_path.is_file():
        raise GoodnotesError(f"SQLite database is not a regular file: {absolute_path}")
    _require_not_production(absolute_path, purpose="a SQLite connection")
    _reject_sqlite_sidecars(absolute_path)

    # immutable=1 prevents SQLite from creating or updating journal/shared-memory files.
    uri = f"{absolute_path.as_uri()}?mode=ro&immutable=1"
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        _check_database_read_access(absolute_path)
        raise CacheAccessError(
            "cache_invalid",
            f"Could not open SQLite database read-only: {absolute_path}",
            operation="open_snapshot_database",
            path=absolute_path,
        ) from exc
    assert connection is not None
    return connection
