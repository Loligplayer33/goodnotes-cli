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
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .errors import FilesystemAccessError, GoodnotesError, _permission_denied
from .safety import SNAPSHOT_MARKER, _is_production_goodnotes_path, open_database

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
    copy_from: Path | None = None
    reused: bool = False


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
            entries.append(
                _file_entry(
                    database_path, Path("Databases") / database_name, hash_contents=hash_contents
                )
            )

        sidecars = sorted(database_dir.glob(database_name + "-*"))
        if sidecars and database_info is None:
            raise GoodnotesError(f"SQLite sidecars exist without their database: {database_path}")
        for sidecar in sidecars:
            if not any(
                sidecar.name == database_name + suffix for suffix in _DATABASE_SIDECAR_SUFFIXES
            ):
                raise GoodnotesError(f"Unsupported SQLite sidecar: {sidecar}")
            entries.append(
                _file_entry(sidecar, Path("Databases") / sidecar.name, hash_contents=hash_contents)
            )

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
            raise _SourceChanged(f"Source directory changed while scanning: {directory}")

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
            # verifies its streaming digest. Neither path rehashes a COW clone.
            info = _lstat(target)
            if not stat.S_ISREG(info.st_mode) or info.st_size != entry.size:
                raise GoodnotesError(f"Reused snapshot file verification failed: {target}")
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
        marker = json.loads((sandbox.root / SNAPSHOT_MARKER).read_text(encoding="utf-8"))
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
                relative, False, saved_stat, current.st_size, digest, copied_file, True
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
        "source_bytes_hashed": sum(entry.size for entry in files if not entry.reused)
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
) -> None:
    entries = list(entries)
    # Freeze files before recording their metadata, so subsequent refreshes can
    # distinguish cache damage from files whose digest was already verified.
    for entry in entries:
        if not entry.is_directory:
            (destination / "Library" / entry.relative).chmod(0o444)
    marker_data: dict[str, Any] = {
        "format_version": 1,
        "created_at_utc": datetime.now(UTC).isoformat(),
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
            if stat.S_IMODE(target.lstat().st_mode) != 0o444:
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
                _validate_projection_database(
                    library_destination / "Databases" / "projection.sqlite"
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
        _make_writable_and_remove(target)
        raise

    # The loop returns on success or raises on its final failed attempt.
    raise GoodnotesError("Snapshot creation failed without a stable source copy.")
