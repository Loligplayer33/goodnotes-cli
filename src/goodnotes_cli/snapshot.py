"""Create a stable, read-only copy of the Goodnotes library inputs."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import plistlib
import sqlite3
import stat
import sys
import tempfile
import time
from collections.abc import Iterable
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import build_info
from .errors import FilesystemAccessError, GoodnotesError, _permission_denied
from .safety import (
    SNAPSHOT_MARKER,
    _is_production_goodnotes_path,
    open_database,
    paths_overlap,
    safe_remove_tree,
    stat_key,
)

_DATABASE_NAMES = ("projection.sqlite", "fts.sqlite")
_NATIVE_STORE_NAMES = ("notes_main.db",)
_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")
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
_GOODNOTES_BUNDLE_IDENTIFIER = "com.goodnotesapp.x"
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


class _SourceChanged(Exception):
    """The source changed while it was being inspected or copied."""


class _InventoryChanged(_SourceChanged, GoodnotesError):
    """A directory inventory changed during one metadata scan."""


@dataclass(frozen=True)
class _Entry:
    relative: Path
    is_directory: bool
    stat_key: tuple[int, int, int, int, int, int]
    size: int = 0
    sha256: str | None = None
    copy_from: Path | None = None
    reused: bool = False
    source_digest_reused: bool = False
    projection_validated: bool = False


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return stat_key(info)


def _lstat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _InventoryChanged(f"Source path disappeared during snapshot: {path}") from exc
    except OSError as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inspect_snapshot_input", path=path, kind="source"
            ) from exc
        raise
    if stat.S_ISLNK(info.st_mode):
        raise GoodnotesError(f"Symlinks are not allowed in snapshot inputs: {path}")
    return info


def _hash_file(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT}:
            raise _SourceChanged(f"Source file changed while opening: {path}") from exc
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="read_snapshot_input", path=path, kind="source"
            ) from exc
        raise GoodnotesError(f"Could not read snapshot input: {path}") from exc

    digest = hashlib.sha256()
    with os.fdopen(descriptor, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if not stat.S_ISREG(opened.st_mode):
            raise GoodnotesError(f"Snapshot input is not a regular file: {path}")
        opened_key = _stat_key(opened)
        path_key = _stat_key(_lstat(path))
        if opened_key != path_key:
            raise _SourceChanged(f"Source file changed while opening: {path}")
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
        after_fd = _stat_key(os.fstat(stream.fileno()))
        after_path = _stat_key(_lstat(path))
        if opened_key != after_fd or opened_key != after_path:
            raise _SourceChanged(f"Source file changed while hashing: {path}")
    return opened_key, opened.st_size, digest.hexdigest()


def _directory_entry(path: Path, relative: Path) -> _Entry:
    info = _lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise GoodnotesError(f"Snapshot input is not a directory: {path}")
    return _Entry(relative=relative, is_directory=True, stat_key=_stat_key(info))


def _file_entry(path: Path, relative: Path, *, hash_contents: bool = True) -> _Entry:
    info = _lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise GoodnotesError(f"Snapshot input is not a regular file: {path}")
    if not hash_contents:
        return _Entry(relative, False, _stat_key(info), info.st_size)
    stat_key, size, digest = _hash_file(path)
    if stat_key != _stat_key(info):
        raise _SourceChanged(f"Source file changed while hashing: {path}")
    return _Entry(
        relative=relative,
        is_directory=False,
        stat_key=stat_key,
        size=size,
        sha256=digest,
    )


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


def _collect_manifest(source_library: Path, *, hash_contents: bool = True) -> list[_Entry]:
    database_dir = source_library / "Databases"
    attachments_dir = source_library / "Attachments"
    entries: list[_Entry] = [
        _directory_entry(source_library / "Databases", Path("Databases")),
        _directory_entry(attachments_dir, Path("Attachments")),
    ]

    # Preserve the main database and its SQLite journals as bytes. No SQLite
    # connection, backup, checkpoint, or extension load is made against source.
    try:
        with os.scandir(database_dir) as iterator:
            database_children = sorted(iterator, key=lambda child: child.name)
    except OSError as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inventory_snapshot_input", path=database_dir, kind="source"
            ) from exc
        raise GoodnotesError(f"Could not inspect snapshot input directory: {database_dir}") from exc
    database_entries = {child.name: child for child in database_children}
    database_names = set(database_entries)
    database_before = _directory_entry(database_dir, Path("Databases"))
    malformed_entries: list[str] = []

    for database_name in _DATABASE_NAMES:
        database_path = database_dir / database_name
        database_info = database_entries.get(database_name)
        if database_info is not None:
            entries.append(
                _file_entry(
                    database_path, Path("Databases") / database_name, hash_contents=hash_contents
                )
            )

        sidecars = [
            child for child in database_children if child.name.startswith(database_name + "-")
        ]
        if sidecars and database_info is None:
            malformed_entries.append(
                f"SQLite sidecars exist without their database: {database_path}"
            )
        for sidecar in sidecars:
            if not any(
                sidecar.name == database_name + suffix for suffix in _DATABASE_SIDECAR_SUFFIXES
            ):
                malformed_entries.append(
                    f"Unsupported SQLite sidecar: {database_dir / sidecar.name}"
                )
                continue
            entries.append(
                _file_entry(
                    database_dir / sidecar.name,
                    Path("Databases") / sidecar.name,
                    hash_contents=hash_contents,
                )
            )

    missing_projection = not any(
        entry.relative == Path("Databases/projection.sqlite") and not entry.is_directory
        for entry in entries
    )

    def scan_directory(directory: Path, relative: Path) -> None:
        before = _directory_entry(directory, relative)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda child: child.name)
        except FileNotFoundError as exc:
            raise _InventoryChanged(
                f"Source directory disappeared while scanning: {directory}"
            ) from exc
        except OSError as exc:
            if _permission_denied(exc):
                raise FilesystemAccessError.from_exception(
                    exc, operation="inventory_snapshot_input", path=directory, kind="source"
                ) from exc
            raise GoodnotesError(
                f"Could not inspect snapshot input directory: {directory}"
            ) from exc
        for child in children:
            child_path = directory / child.name
            child_relative = relative / child.name
            child_info = _lstat(child_path)
            if stat.S_ISDIR(child_info.st_mode):
                entry = _directory_entry(child_path, child_relative)
                entries.append(entry)
                scan_directory(child_path, child_relative)
            elif stat.S_ISREG(child_info.st_mode):
                entries.append(_file_entry(child_path, child_relative, hash_contents=hash_contents))
            else:
                raise GoodnotesError(f"Unsupported snapshot input type: {child_path}")
        after = _directory_entry(directory, relative)
        if before.stat_key != after.stat_key:
            raise _InventoryChanged(f"Source directory changed while scanning: {directory}")

    database_after = _directory_entry(database_dir, Path("Databases"))
    try:
        with os.scandir(database_dir) as iterator:
            database_names_after = {child.name for child in iterator}
    except FileNotFoundError as exc:
        raise _InventoryChanged(
            f"Source database directory disappeared while scanning: {database_dir}"
        ) from exc
    except OSError as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inventory_snapshot_input", path=database_dir, kind="source"
            ) from exc
        raise GoodnotesError(f"Could not inspect snapshot input directory: {database_dir}") from exc
    if (
        database_before.stat_key != database_after.stat_key
        or database_names != database_names_after
    ):
        raise _InventoryChanged(f"Source database directory changed while scanning: {database_dir}")
    if malformed_entries:
        raise GoodnotesError(malformed_entries[0])
    if missing_projection:
        raise GoodnotesError(
            f"Required projection database is missing: {database_dir / 'projection.sqlite'}"
        )

    scan_directory(attachments_dir, Path("Attachments"))
    for name in _NATIVE_STORE_NAMES:
        native_path = database_dir / name
        try:
            native_path.lstat()
        except FileNotFoundError:
            continue
        entries.append(_directory_entry(native_path, Path("Databases") / name))
        scan_directory(native_path, Path("Databases") / name)
    entries.sort(key=lambda entry: (entry.relative.as_posix(), not entry.is_directory))
    return entries


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
    destination_library.mkdir(mode=0o700)
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


def _goodnotes_info_plist_candidates() -> tuple[Path, ...]:
    return (
        Path("/Applications/Goodnotes.app/Contents/Info.plist"),
        Path.home() / "Applications/Goodnotes.app/Contents/Info.plist",
    )


def _read_installed_goodnotes_info() -> dict[str, str | None] | None:
    """Read installed app identity from Info.plist only; never launch or query the app."""

    for info_path in _goodnotes_info_plist_candidates():
        try:
            descriptor = os.open(
                info_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            try:
                info = os.fstat(descriptor)
                if not stat.S_ISREG(info.st_mode):
                    continue
                with os.fdopen(os.dup(descriptor), "rb") as stream:
                    raw_metadata = stream.read(1_000_001)
            finally:
                os.close(descriptor)
            if len(raw_metadata) > 1_000_000:
                continue
            metadata = plistlib.loads(raw_metadata)
        except (OSError, EOFError, ValueError, plistlib.InvalidFileException):
            continue
        if not isinstance(metadata, dict):
            continue
        if metadata.get("CFBundleIdentifier") != _GOODNOTES_BUNDLE_IDENTIFIER:
            continue

        fields = {
            key: value.strip() or None if isinstance(value, str) else None
            for key, value in metadata.items()
        }
        return {
            "name": fields.get("CFBundleDisplayName") or fields.get("CFBundleName") or "Goodnotes",
            "bundle_id": _GOODNOTES_BUNDLE_IDENTIFIER,
            "version": fields.get("CFBundleShortVersionString"),
            "build": fields.get("CFBundleVersion"),
            "basis": "installed-app-at-capture",
        }
    return None


def _source_application_info(source: Path) -> dict[str, str | None] | None:
    """Probe app metadata only for the configured live Goodnotes Library path."""

    from .config import default_library

    try:
        default_source = default_library().resolve(strict=False)
        resolved_source = source.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved_source != default_source:
        return None
    return _read_installed_goodnotes_info()


def _valid_relative(value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {".", ".."} for part in path.parts):
        return None
    if path.as_posix() != value:
        return None
    return path


def _saved_stat(value: object) -> tuple[int, int, int, int, int, int] | None:
    if (
        not isinstance(value, list)
        or len(value) != 6
        or any(type(item) is not int for item in value)
    ):
        return None
    return (value[0], value[1], value[2], value[3], value[4], value[5])


def _saved_digest(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(char in "0123456789abcdef" for char in value) else None


def _previous_inventory(
    previous: Path | None, source: Path
) -> tuple[dict[Path, _Entry], dict[Path, _Entry]]:
    """Load verified digests, accepting only unchanged regular copied files.

    A marker from an older CLI still works as a snapshot but is not eligible for
    incremental reuse. Damage to one cached file invalidates reuse for that file.
    """

    if previous is None:
        return {}, {}
    from .safety import Sandbox

    try:
        sandbox = Sandbox.load(previous)
        marker = sandbox.marker_data
        if marker.get("source_path") != str(source):
            return {}, {}
        source_records, file_records = marker.get("source_inventory"), marker.get("files")
        if not isinstance(source_records, list) or not isinstance(file_records, list):
            return {}, {}
        source_entries: dict[Path, _Entry] = {}
        for record in source_records:
            if not isinstance(record, dict):
                return {}, {}
            relative = _valid_relative(record.get("path"))
            saved_stat = _saved_stat(record.get("stat_key"))
            digest = _saved_digest(record.get("sha256"))
            if relative is None or saved_stat is None or digest is None:
                return {}, {}
            if relative in source_entries or not stat.S_ISREG(saved_stat[2]):
                return {}, {}
            source_entries[relative] = _Entry(relative, False, saved_stat, saved_stat[3], digest)
        validation = marker.get("projection_validation")
        projection_validation_digest = (
            validation.get("sha256")
            if isinstance(validation, dict)
            and validation.get("validation_id") == _PROJECTION_VALIDATION_ID
            else None
        )
        copied_entries: dict[Path, _Entry] = {}
        for record in file_records:
            if not isinstance(record, dict):
                return {}, {}
            saved_path = _valid_relative(record.get("path"))
            saved_stat = _saved_stat(record.get("cache_stat_key"))
            digest = _saved_digest(record.get("sha256"))
            if saved_path is None or saved_path.parts[0] != "Library":
                return {}, {}
            relative = Path(*saved_path.parts[1:])
            if not relative.parts or relative in copied_entries:
                return {}, {}
            if saved_stat is None or digest is None or record.get("size") != saved_stat[3]:
                continue
            copied_file = sandbox.library_dir / relative
            try:
                for parent in reversed(copied_file.parents):
                    if parent == sandbox.library_dir or sandbox.library_dir in parent.parents:
                        info = _lstat(parent)
                        if not stat.S_ISDIR(info.st_mode):
                            raise GoodnotesError("Previous copied parent is not a directory")
                current = _lstat(copied_file)
            except (GoodnotesError, OSError):
                continue
            if not stat.S_ISREG(current.st_mode) or _stat_key(current) != saved_stat:
                continue
            copied_entries[relative] = _Entry(
                relative,
                False,
                saved_stat,
                current.st_size,
                digest,
                copied_file,
                True,
                projection_validated=(
                    relative == Path("Databases/projection.sqlite")
                    and projection_validation_digest == digest
                ),
            )
        return source_entries, copied_entries
    except (GoodnotesError, OSError, ValueError, TypeError):
        return {}, {}


def _sqlite_family(relative: Path, name: str) -> bool:
    return relative == Path("Databases") / name or relative in {
        Path("Databases") / (name + suffix) for suffix in _DATABASE_SIDECAR_SUFFIXES
    }


def _plan_capture(
    source: Path, entries: list[_Entry], previous: Path | None
) -> tuple[list[_Entry], list[_Entry]]:
    """Choose previous independent files or hash current source bytes.

    A completed SQLite copy may have consolidated WAL data. Reuse it only when
    its entire original source family is unchanged; otherwise recapture that
    family as bytes. Native stores and attachments can reuse individual files.
    """

    previous_source, previous_copy = _previous_inventory(previous, source)
    files = {entry.relative: entry for entry in entries if not entry.is_directory}
    reused_families: dict[str, _Entry] = {}
    for name in _DATABASE_NAMES:
        current_family = {
            path: entry.stat_key for path, entry in files.items() if _sqlite_family(path, name)
        }
        old_family = {
            path: entry.stat_key
            for path, entry in previous_source.items()
            if _sqlite_family(path, name)
        }
        copied = previous_copy.get(Path("Databases") / name)
        if current_family and current_family == old_family and copied is not None:
            reused_families[name] = copied

    copy_entries: list[_Entry] = []
    source_entries: list[_Entry] = []
    for entry in entries:
        if entry.is_directory:
            copy_entries.append(entry)
            continue
        family = next(
            (name for name in _DATABASE_NAMES if _sqlite_family(entry.relative, name)), None
        )
        if family is not None and family in reused_families:
            source_entries.append(previous_source[entry.relative])
            if entry.relative == Path("Databases") / family:
                copy_entries.append(reused_families[family])
            continue
        old_source = previous_source.get(entry.relative)
        copied = previous_copy.get(entry.relative)
        if (
            family is None
            and old_source is not None
            and old_source.stat_key == entry.stat_key
            and copied is not None
            and copied.sha256 == old_source.sha256
        ):
            source_entries.append(old_source)
            copy_entries.append(copied)
        else:
            if (
                family is not None
                and entry.relative.name == family
                and old_source is not None
                and old_source.stat_key == entry.stat_key
                and old_source.sha256 is not None
            ):
                # A changed SQLite family cannot reuse consolidated cache bytes,
                # but an unchanged source member still has a previously verified
                # digest. Copy the current source file and verify the destination
                # against that digest instead of hashing the source member again.
                hashed = _Entry(
                    entry.relative,
                    False,
                    entry.stat_key,
                    entry.size,
                    old_source.sha256,
                    source_digest_reused=True,
                )
            else:
                hashed = _file_entry(source / entry.relative, entry.relative)
                if hashed.stat_key != entry.stat_key:
                    raise _SourceChanged(
                        f"Source file changed before capture: {source / entry.relative}"
                    )
            source_entries.append(hashed)
            copy_entries.append(hashed)
    return copy_entries, source_entries


def _capture_stats(entries: Iterable[_Entry], metrics: dict[str, int]) -> dict[str, Any]:
    entries = list(entries)
    files = [entry for entry in entries if not entry.is_directory]
    return {
        "scope": "successful-attempt",
        "source_captured_files": sum(not entry.reused for entry in files),
        "source_captured_bytes": sum(entry.size for entry in files if not entry.reused),
        "cache_reused_files": sum(entry.reused for entry in files),
        "cache_reused_bytes": sum(entry.size for entry in files if entry.reused),
        "source_bytes_hashed": sum(
            entry.size for entry in files if not entry.reused and not entry.source_digest_reused
        )
        + metrics["source_copy_bytes_hashed"],
        **{name: value for name, value in metrics.items() if name != "source_copy_bytes_hashed"},
        "total_files": len(files),
        "total_bytes": sum(entry.size for entry in files),
        "bytes_are_logical": True,
        "native_store_present": any(
            entry.relative.parts[:2] == ("Databases", "notes_main.db") for entry in entries
        ),
    }


def _final_manifest(library: Path, planned: list[_Entry]) -> list[_Entry]:
    """Retain known file digests; hash only SQLite bytes changed by consolidation."""

    known = {entry.relative: entry for entry in planned if not entry.is_directory}
    consolidated = {
        name
        for name in _DATABASE_NAMES
        if any(
            _sqlite_family(entry.relative, name) and entry.relative.name != name
            for entry in planned
        )
    }
    final = []
    for entry in _collect_manifest(library, hash_contents=False):
        if entry.is_directory:
            final.append(entry)
            continue
        if entry.relative in {Path("Databases") / name for name in consolidated}:
            final.append(_file_entry(library / entry.relative, entry.relative))
        else:
            original = known[entry.relative]
            final.append(_Entry(entry.relative, False, entry.stat_key, entry.size, original.sha256))
    return final


def _write_marker(
    destination: Path,
    entries: Iterable[_Entry],
    *,
    source: Path | None = None,
    source_entries: Iterable[_Entry] = (),
    capture_stats: dict[str, Any] | None = None,
    projection_library_format: dict[str, int] | None = None,
) -> None:
    entries = list(entries)
    # Freeze files before recording their metadata, so subsequent refreshes can
    # distinguish cache damage from files whose digest was already verified.
    for entry in entries:
        if not entry.is_directory:
            path = destination / "Library" / entry.relative
            if stat.S_IMODE(path.lstat().st_mode) != 0o444:
                path.chmod(0o444)
    marker_data: dict[str, Any] = {
        "format_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "producer": {"distribution": "goodnotes-local-cli", **build_info()},
        "source_app": _source_application_info(source) if source is not None else None,
        "files": [
            {
                "path": (Path("Library") / entry.relative).as_posix(),
                "size": entry.size,
                "sha256": entry.sha256,
                "cache_stat_key": _stat_key((destination / "Library" / entry.relative).lstat()),
            }
            for entry in entries
            if not entry.is_directory
        ],
    }
    if source is not None:
        marker_data["source_path"] = str(source)
        marker_data["source_inventory"] = [
            {"path": entry.relative.as_posix(), "stat_key": entry.stat_key, "sha256": entry.sha256}
            for entry in source_entries
            if not entry.is_directory
        ]
    if capture_stats is not None:
        marker_data["capture_stats"] = capture_stats
    projection = next(
        (
            entry
            for entry in entries
            if not entry.is_directory and entry.relative == Path("Databases/projection.sqlite")
        ),
        None,
    )
    if projection is not None and projection.sha256 is not None:
        marker_data["projection_validation"] = {
            "validation_id": _PROJECTION_VALIDATION_ID,
            "sha256": projection.sha256,
            "library_format": projection_library_format,
        }
    marker = destination / SNAPSHOT_MARKER
    fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-marker-", dir=destination)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(marker_data, stream, separators=(",", ":"), sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _make_read_only(destination: Path, entries: Iterable[_Entry]) -> None:
    for entry in sorted(
        (entry for entry in entries if entry.is_directory),
        key=lambda item: len(item.relative.parts),
        reverse=True,
    ):
        (destination / "Library" / entry.relative).chmod(0o555)
    (destination / "Library").chmod(0o555)
    (destination / SNAPSHOT_MARKER).chmod(0o444)
    destination.chmod(0o555)


def _make_writable_and_remove(path: Path) -> None:
    if path.is_symlink():
        return
    safe_remove_tree(path)


def _paths_overlap(left: Path, right: Path) -> bool:
    return paths_overlap(left, right)


def create_snapshot(
    source_library: str | Path,
    destination: str | Path,
    *,
    max_attempts: int = 3,
    previous_snapshot: Path | None = None,
) -> Path:
    """Copy the Goodnotes Library tree into a new, validated read-only snapshot.

    The source is inspected and copied as ordinary files. SQLite is never opened
    at the source path, which preserves the production databases byte-for-byte.
    A bounded manifest comparison detects changes that overlap the copy window.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    requested_source = Path(source_library).expanduser()
    try:
        if requested_source.is_symlink():
            raise GoodnotesError("The source Library directory must not be a symlink.")
        source = requested_source.resolve(strict=True)
        source_info = source.lstat()
    except OSError as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="resolve_source_library", path=requested_source, kind="source"
            ) from exc
        raise GoodnotesError(
            f"Source Library directory does not exist: {requested_source}"
        ) from exc
    if not stat.S_ISDIR(source_info.st_mode):
        raise GoodnotesError(f"Source Library path is not a directory: {source}")

    requested_destination = Path(destination).expanduser()
    if requested_destination.exists() or requested_destination.is_symlink():
        raise GoodnotesError(
            f"Refusing to overwrite an existing snapshot destination: {requested_destination}"
        )
    target = requested_destination.resolve(strict=False)
    if _is_production_goodnotes_path(target):
        raise GoodnotesError(
            f"Refusing to create a snapshot inside a production Goodnotes path: {target}"
        )
    if _paths_overlap(source, target):
        raise GoodnotesError("Snapshot source and destination must not overlap.")

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise GoodnotesError(
            f"Refusing to overwrite an existing snapshot destination: {target}"
        ) from exc
    except OSError as exc:
        raise GoodnotesError(f"Could not create snapshot destination: {target}") from exc

    started = time.monotonic()
    try:
        for attempt in range(max_attempts):
            try:
                manifest_before = _collect_manifest(source, hash_contents=False)
                planned, source_files = _plan_capture(source, manifest_before, previous_snapshot)
                library_destination = target / "Library"
                copy_metrics = _copy_snapshot_tree(source, library_destination, planned)
                manifest_after = _collect_manifest(source, hash_contents=False)
                if manifest_before != manifest_after:
                    raise _SourceChanged("Source files changed during snapshot copy.")
                _verify_copy(library_destination, planned)
                _consolidate_copied_databases(library_destination)
                projection_copy = next(
                    entry
                    for entry in planned
                    if not entry.is_directory
                    and entry.relative == Path("Databases/projection.sqlite")
                )
                projection_copy_path = library_destination / "Databases" / "projection.sqlite"
                # This attestation can only accompany a reused projection after
                # the entire source SQLite family matched its saved fingerprints
                # and the previous copy's stat key and digest record were valid.
                # A COW clone preserves those bytes; changed families and damaged
                # cache members take the normal integrity and schema check path.
                if not (projection_copy.reused and projection_copy.projection_validated):
                    projection_library_format = _validate_projection_database(projection_copy_path)
                else:
                    projection_library_format = _read_projection_library_format(
                        projection_copy_path
                    )
                final_manifest = _final_manifest(library_destination, planned)
                capture_stats = _capture_stats(planned, copy_metrics)
                capture_stats["attempts"] = attempt + 1
                capture_stats["capture_seconds"] = round(time.monotonic() - started, 6)
                _write_marker(
                    target,
                    final_manifest,
                    source=source,
                    source_entries=source_files,
                    capture_stats=capture_stats,
                    projection_library_format=projection_library_format,
                )
                _make_read_only(target, final_manifest)
                return target
            except _SourceChanged as exc:
                _make_writable_and_remove(target / "Library")
                if attempt + 1 >= max_attempts:
                    raise GoodnotesError(
                        f"Source kept changing after {max_attempts} snapshot attempts."
                    ) from exc
    except BaseException:
        try:
            _make_writable_and_remove(target)
        except GoodnotesError:
            # Preserve the original capture failure. Unsafe staging is left in
            # place for inspection rather than following links during cleanup.
            pass
        raise

    # The loop returns on success or raises on its final failed attempt.
    raise GoodnotesError("Snapshot creation failed without a stable source copy.")
