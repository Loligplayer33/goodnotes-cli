"""Create a stable, read-only copy of the Goodnotes library inputs."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .errors import GoodnotesError
from .safety import SNAPSHOT_MARKER, _is_production_goodnotes_path, open_database

_DATABASE_NAMES = ("projection.sqlite", "fts.sqlite")
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


@dataclass(frozen=True)
class _Entry:
    relative: Path
    is_directory: bool
    stat_key: tuple[int, int, int, int, int, int]
    size: int = 0
    sha256: str | None = None


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _lstat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError as exc:
        raise _SourceChanged(f"Source path disappeared during snapshot: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise GoodnotesError(f"Symlinks are not allowed in snapshot inputs: {path}")
    return info


def _hash_file(path: Path) -> tuple[tuple[int, int, int, int, int, int], int, str]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT}:
            raise _SourceChanged(f"Source file changed while opening: {path}") from exc
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


def _file_entry(path: Path, relative: Path) -> _Entry:
    info = _lstat(path)
    if not stat.S_ISREG(info.st_mode):
        raise GoodnotesError(f"Snapshot input is not a regular file: {path}")
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


def _collect_manifest(source_library: Path) -> list[_Entry]:
    database_dir = source_library / "Databases"
    attachments_dir = source_library / "Attachments"
    entries: list[_Entry] = [
        _directory_entry(source_library / "Databases", Path("Databases")),
        _directory_entry(attachments_dir, Path("Attachments")),
    ]

    # Preserve the main database and its SQLite journals as bytes. No SQLite
    # connection, backup, checkpoint, or extension load is made against source.
    for database_name in _DATABASE_NAMES:
        database_path = database_dir / database_name
        try:
            database_info = database_path.lstat()
        except FileNotFoundError:
            database_info = None
        if database_info is not None:
            if stat.S_ISLNK(database_info.st_mode):
                raise GoodnotesError(
                    f"Symlinks are not allowed in snapshot inputs: {database_path}"
                )
            if not stat.S_ISREG(database_info.st_mode):
                raise GoodnotesError(f"Snapshot input is not a regular file: {database_path}")
            entries.append(_file_entry(database_path, Path("Databases") / database_name))

        sidecars = sorted(database_dir.glob(database_name + "-*"))
        if sidecars and database_info is None:
            raise GoodnotesError(f"SQLite sidecars exist without their database: {database_path}")
        for sidecar in sidecars:
            if not any(
                sidecar.name == database_name + suffix for suffix in _DATABASE_SIDECAR_SUFFIXES
            ):
                raise GoodnotesError(f"Unsupported SQLite sidecar: {sidecar}")
            entries.append(_file_entry(sidecar, Path("Databases") / sidecar.name))

    if not any(
        entry.relative == Path("Databases/projection.sqlite") and not entry.is_directory
        for entry in entries
    ):
        raise GoodnotesError(
            f"Required projection database is missing: {database_dir / 'projection.sqlite'}"
        )

    def scan_directory(directory: Path, relative: Path) -> None:
        before = _directory_entry(directory, relative)
        try:
            children = sorted(os.scandir(directory), key=lambda child: child.name)
        except OSError as exc:
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
                entries.append(_file_entry(child_path, child_relative))
            else:
                raise GoodnotesError(f"Unsupported snapshot input type: {child_path}")
        after = _directory_entry(directory, relative)
        if before.stat_key != after.stat_key:
            raise _SourceChanged(f"Source directory changed while scanning: {directory}")

    scan_directory(attachments_dir, Path("Attachments"))
    entries.sort(key=lambda entry: (entry.relative.as_posix(), not entry.is_directory))
    return entries


def _copy_regular_file(source: Path, destination: Path, expected: _Entry) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        source_fd = os.open(source, flags)
    except OSError as exc:
        if exc.errno in {errno.ELOOP, errno.ENOENT}:
            raise _SourceChanged(f"Source file changed while copying: {source}") from exc
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
    finally:
        if source_fd >= 0:
            os.close(source_fd)


def _copy_snapshot_tree(
    source_library: Path, destination_library: Path, entries: Iterable[_Entry]
) -> None:
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
    for entry in files:
        target = destination_library / entry.relative
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_regular_file(source_library / entry.relative, target, entry)


def _verify_copy(destination_library: Path, entries: Iterable[_Entry]) -> None:
    for entry in entries:
        target = destination_library / entry.relative
        if entry.is_directory:
            info = _lstat(target)
            if not stat.S_ISDIR(info.st_mode):
                raise GoodnotesError(f"Snapshot directory verification failed: {target}")
            continue
        _, size, digest = _hash_file(target)
        if size != entry.size or digest != entry.sha256:
            raise GoodnotesError(f"Snapshot file verification failed: {target}")


def _consolidate_copied_databases(library_copy: Path) -> None:
    """Fold copied SQLite sidecars into copied database files only."""

    database_dir = library_copy / "Databases"
    for database_name in _DATABASE_NAMES:
        database_path = database_dir / database_name
        if not database_path.is_file():
            continue
        sidecars = [Path(f"{database_path}{suffix}") for suffix in _DATABASE_SIDECAR_SUFFIXES]
        if not any(sidecar.exists() for sidecar in sidecars):
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


def _validate_projection_database(database_path: Path) -> None:
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
    except sqlite3.Error as exc:
        raise GoodnotesError(
            "Copied projection database failed SQLite integrity or schema validation."
        ) from exc


def _write_marker(destination: Path, entries: Iterable[_Entry]) -> None:
    marker_data = {
        "format_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "files": [
            {
                "path": (Path("Library") / entry.relative).as_posix(),
                "size": entry.size,
                "sha256": entry.sha256,
            }
            for entry in entries
            if not entry.is_directory
        ],
    }
    marker = destination / SNAPSHOT_MARKER
    fd, temporary_name = tempfile.mkstemp(prefix=".snapshot-marker-", dir=destination)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(marker_data, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, marker)
    finally:
        temporary.unlink(missing_ok=True)


def _make_read_only(destination: Path, entries: Iterable[_Entry]) -> None:
    for entry in entries:
        target = destination / "Library" / entry.relative
        if not entry.is_directory:
            target.chmod(0o444)
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
    if not path.exists() or path.is_symlink():
        return
    for current, directories, files in os.walk(path):
        current_path = Path(current)
        try:
            current_path.chmod(0o700)
        except OSError:
            pass
        for name in files:
            try:
                (current_path / name).chmod(0o600)
            except OSError:
                pass
        for name in directories:
            try:
                (current_path / name).chmod(0o700)
            except OSError:
                pass
    shutil.rmtree(path, ignore_errors=True)


def _paths_overlap(left: Path, right: Path) -> bool:
    return left == right or left in right.parents or right in left.parents


def create_snapshot(
    source_library: str | Path,
    destination: str | Path,
    *,
    max_attempts: int = 3,
) -> Path:
    """Copy the Goodnotes Library tree into a new, validated read-only snapshot.

    The source is inspected and copied as ordinary files. SQLite is never opened
    at the source path, which preserves the production databases byte-for-byte.
    A bounded manifest comparison detects changes that overlap the copy window.
    """

    if max_attempts < 1:
        raise ValueError("max_attempts must be at least one")
    requested_source = Path(source_library).expanduser()
    if requested_source.is_symlink():
        raise GoodnotesError("The source Library directory must not be a symlink.")
    try:
        source = requested_source.resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(
            f"Source Library directory does not exist: {requested_source}"
        ) from exc
    if not source.is_dir():
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

    try:
        for attempt in range(max_attempts):
            try:
                manifest_before = _collect_manifest(source)
                library_destination = target / "Library"
                _copy_snapshot_tree(source, library_destination, manifest_before)
                manifest_after = _collect_manifest(source)
                if manifest_before != manifest_after:
                    raise _SourceChanged("Source files changed during snapshot copy.")
                _verify_copy(library_destination, manifest_before)
                _consolidate_copied_databases(library_destination)
                _validate_projection_database(
                    library_destination / "Databases" / "projection.sqlite"
                )
                final_manifest = _collect_manifest(library_destination)
                _write_marker(target, final_manifest)
                _make_read_only(target, final_manifest)
                return target
            except _SourceChanged as exc:
                _make_writable_and_remove(target / "Library")
                if attempt + 1 >= max_attempts:
                    raise GoodnotesError(
                        f"Source kept changing after {max_attempts} snapshot attempts."
                    ) from exc
    except BaseException:
        _make_writable_and_remove(target)
        raise

    # The loop returns on success or raises on its final failed attempt.
    raise GoodnotesError("Snapshot creation failed without a stable source copy.")
