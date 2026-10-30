"""Storage operations."""

from __future__ import annotations

import os
import sqlite3
import stat
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from goodnotes_cli.errors import CacheAccessError, GoodnotesError, _permission_denied
from goodnotes_cli.storage.metadata import SNAPSHOT_MARKER_LIMIT, read_json
from goodnotes_cli.storage.paths import OutputProtection, _require_not_production, safe_output

SNAPSHOT_MARKER = "goodnotes-snapshot.json"
_SQLITE_SIDECAR_PREFIX = "-"


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
            marker_data = read_json(marker, limit=SNAPSHOT_MARKER_LIMIT)
        except (OSError, ValueError) as exc:
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

    def output_protection(self) -> OutputProtection:
        """Interpret optional captured provenance only when authorizing writes."""

        if "source_path" not in self.marker_data:
            return OutputProtection(self.root)
        source = self.marker_data["source_path"]
        if (
            not isinstance(source, str)
            or not source
            or "\x00" in source
            or not Path(source).is_absolute()
        ):
            raise GoodnotesError(
                "The snapshot's captured source path is invalid for write authorization."
            )
        return OutputProtection(self.root, Path(source))

    def ensure_output(
        self,
        path: str | Path,
        *,
        inputs: tuple[str | Path, ...] | list[str | Path] = (),
    ) -> Path:
        """Return an absolute output path after checking it cannot replace input data."""

        return safe_output(path, self, inputs=inputs)


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
