"""Consolidate and validate copied SQLite files."""

from __future__ import annotations

import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path

from ...errors import GoodnotesError
from ..sandbox import open_database
from .types import _DATABASE_NAMES, _DATABASE_SIDECAR_SUFFIXES

_PROJECTION_COLUMNS = {
    "folders": {"id", "name", "updated_at", "deleted", "position"},
    "documents": {"id", "name", "updated_at", "deleted", "position"},
    "folder_to_folder_items": {
        "id",
        "item_type",
        "item_id",
        "parent_folder_id",
        "root_folder_id",
        "item_position",
        "deleted",
    },
    "pages": {"id", "document_id", "template_id", "position", "group_id", "deleted"},
}
_PROJECTION_VALIDATION_ID = "quick-check-and-schema-v1"


def _checkpoint_copied_database(database_path: Path) -> bool:
    """Checkpoint unpublished staging WAL; return false for a non-WAL database."""

    # A copied SHM caches another process's WAL index, checkpoint, and read marks.
    # SQLite rebuilds it from the WAL; only this private staging file is removed.
    try:
        Path(f"{database_path}-shm").unlink(missing_ok=True)
        # COW clones retain source permissions, including a read-only source
        # copy. Only these independently owned staging files need write access.
        for staging_file in (database_path, Path(f"{database_path}-wal")):
            staging_file.chmod(staging_file.stat().st_mode | stat.S_IRUSR | stat.S_IWUSR)
        uri = f"{database_path.resolve(strict=True).as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.0)) as conn:
            if conn.execute("PRAGMA main.journal_mode").fetchone() != ("wal",):
                return False
            conn.execute("PRAGMA wal_autocheckpoint = 0")
            result = conn.execute("PRAGMA main.wal_checkpoint(TRUNCATE)").fetchone()
            if result != (0, 0, 0):
                raise GoodnotesError(
                    f"Copied SQLite WAL checkpoint did not complete: {database_path}"
                )
            mode = conn.execute("PRAGMA main.journal_mode = DELETE").fetchone()
            if mode != ("delete",):
                raise GoodnotesError(f"Could not remove copied SQLite WAL mode: {database_path}")
        for suffix in _DATABASE_SIDECAR_SUFFIXES:
            sidecar = Path(f"{database_path}{suffix}")
            if sidecar.exists():
                if sidecar.stat().st_size:
                    raise GoodnotesError(
                        f"SQLite checkpoint left a nonempty copied sidecar: {sidecar}"
                    )
                sidecar.unlink()
        return True
    except (OSError, sqlite3.Error) as exc:
        raise GoodnotesError(
            f"Could not checkpoint copied SQLite database: {database_path}"
        ) from exc


def _recover_copied_hot_rollback_journal(database_path: Path) -> bool:
    """Recover a genuine hot rollback journal on its private staging copy only."""

    journal = Path(f"{database_path}-journal")
    try:
        journal_info = journal.lstat()
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect copied SQLite journal: {journal}") from exc
    if stat.S_ISLNK(journal_info.st_mode) or not stat.S_ISREG(journal_info.st_mode):
        raise GoodnotesError(f"Copied SQLite journal is not a regular file: {journal}")
    if journal_info.st_size == 0:
        return False

    try:
        for staging_file in (database_path, journal):
            info = staging_file.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if not mode & stat.S_IWUSR:
                staging_file.chmod(mode | stat.S_IWUSR)
        uri = f"{database_path.resolve(strict=True).as_uri()}?mode=rw"
        with closing(sqlite3.connect(uri, uri=True, isolation_level=None, timeout=0.0)) as conn:
            mode = conn.execute("PRAGMA main.journal_mode").fetchone()
            if mode != ("delete",):
                return False
            # Opening the copied database in read/write mode and reading its schema
            # invokes SQLite's own hot-journal recovery. DatabaseError is allowed to
            # fail capture; corruption is never mistaken for a retryable inventory race.
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            normalized = conn.execute("PRAGMA main.journal_mode = DELETE").fetchone()
            if normalized != ("delete",):
                raise GoodnotesError(
                    f"Could not normalize copied SQLite journal mode: {database_path}"
                )
        try:
            journal.lstat()
        except FileNotFoundError:
            return True
        return False
    except GoodnotesError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise GoodnotesError(
            f"Could not recover copied SQLite rollback journal: {database_path}"
        ) from exc


def _consolidate_copied_databases(library_copy: Path) -> None:
    """Fold sidecars into independent, unpublished staging database files only."""

    database_dir = library_copy / "Databases"
    for database_name in _DATABASE_NAMES:
        database_path = database_dir / database_name
        if not database_path.is_file():
            continue
        sidecars = [Path(f"{database_path}{suffix}") for suffix in _DATABASE_SIDECAR_SUFFIXES]
        if not any(sidecar.exists() for sidecar in sidecars):
            continue
        if Path(f"{database_path}-wal").is_file() and not Path(f"{database_path}-journal").exists():
            if _checkpoint_copied_database(database_path):
                continue
        if Path(f"{database_path}-journal").exists() and _recover_copied_hot_rollback_journal(
            database_path
        ):
            continue

        temporary_database = database_path.with_name(database_path.name + ".consolidated")
        temporary_sidecars = [
            Path(f"{temporary_database}{suffix}") for suffix in ("-wal", "-shm", "-journal")
        ]
        source_connection: sqlite3.Connection | None = None
        target_connection: sqlite3.Connection | None = None
        try:
            source_uri = f"{database_path.resolve(strict=True).as_uri()}?mode=ro"
            source_connection = sqlite3.connect(source_uri, uri=True)
            source_connection.execute("PRAGMA query_only = ON")
            target_connection = sqlite3.connect(temporary_database)
            source_connection.backup(target_connection)
            target_connection.commit()
            mode = target_connection.execute("PRAGMA main.journal_mode = DELETE").fetchone()
            if mode != ("delete",):
                raise GoodnotesError(
                    f"Could not normalize consolidated SQLite journal mode: {temporary_database}"
                )
        except (OSError, sqlite3.Error) as exc:
            raise GoodnotesError(
                f"Could not consolidate copied SQLite database: {database_path}"
            ) from exc
        finally:
            if target_connection is not None:
                target_connection.close()
            if source_connection is not None:
                source_connection.close()

        if not temporary_database.is_file():
            raise GoodnotesError(f"SQLite backup did not create a database: {temporary_database}")
        os.replace(temporary_database, database_path)
        for sidecar in (*sidecars, *temporary_sidecars):
            sidecar.unlink(missing_ok=True)


def _projection_library_format(connection: sqlite3.Connection) -> dict[str, int]:
    """Read SQLite format identifiers from a copied projection database."""

    user_version = connection.execute("PRAGMA user_version").fetchone()
    application_id = connection.execute("PRAGMA application_id").fetchone()
    if user_version is None or application_id is None:
        raise GoodnotesError("Copied projection database did not report SQLite format metadata.")
    return {
        "sqlite_user_version": int(user_version[0]),
        "sqlite_application_id": int(application_id[0]),
    }


def _read_projection_library_format(database_path: Path) -> dict[str, int]:
    """Read format identifiers from an already-validated copied projection."""

    try:
        with open_database(database_path) as connection:
            return _projection_library_format(connection)
    except sqlite3.Error as exc:
        raise GoodnotesError(
            "Could not read SQLite format metadata from the copied projection database."
        ) from exc


def _validate_projection_database(database_path: Path) -> dict[str, int]:
    """Check the copied projection's SQLite integrity and reader-facing schema."""

    try:
        with open_database(database_path) as connection:
            check_rows = connection.execute("PRAGMA quick_check").fetchall()
            check_messages = [str(row[0]) for row in check_rows]
            if check_messages != ["ok"]:
                detail = "; ".join(check_messages[:3]) or "no integrity result"
                raise GoodnotesError(
                    f"Copied projection database failed SQLite quick_check: {detail}"
                )

            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = set(_PROJECTION_COLUMNS) - tables
            if missing_tables:
                missing = ", ".join(sorted(missing_tables))
                raise GoodnotesError(
                    f"Copied projection database is missing core tables: {missing}"
                )
            for table, required_columns in _PROJECTION_COLUMNS.items():
                columns = {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
                missing_columns = required_columns - columns
                if missing_columns:
                    missing = ", ".join(sorted(missing_columns))
                    raise GoodnotesError(
                        f"Copied projection table {table} is missing columns: {missing}"
                    )
            return _projection_library_format(connection)
    except sqlite3.Error as exc:
        raise GoodnotesError(
            "Copied projection database failed SQLite integrity or schema validation."
        ) from exc
