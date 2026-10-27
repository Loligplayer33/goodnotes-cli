"""Inspect capture inputs and plan incremental copies."""

from __future__ import annotations

import errno
import hashlib
import os
import stat
from pathlib import Path

from ...errors import FilesystemAccessError, GoodnotesError, _permission_denied
from ..paths import stat_key as _stat_key
from .sqlite import _PROJECTION_VALIDATION_ID
from .types import (
    _DATABASE_NAMES,
    _DATABASE_SIDECAR_SUFFIXES,
    _NATIVE_STORE_NAMES,
    _Entry,
    _InventoryChanged,
    _SourceChanged,
)


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
    from ..sandbox import Sandbox

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
