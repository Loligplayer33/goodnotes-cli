"""Storage operations."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from pathlib import Path

from goodnotes_cli.errors import (
    FilesystemAccessError,
    GoodnotesError,
    _os_cause,
    _permission_denied,
)
from goodnotes_cli.storage.capture.types import (
    _DATABASE_NAMES,
    _DATABASE_SIDECAR_SUFFIXES,
    _NATIVE_STORE_NAMES,
    _InventoryChanged,
)
from goodnotes_cli.storage.paths import stat_key as _stat_key


def _lstat(path: Path) -> os.stat_result:
    try:
        info = path.lstat()
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect cache input: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise GoodnotesError(f"Symlinks are not allowed in cache paths: {path}")
    return info


def _require_directory(path: Path, *, purpose: str) -> None:
    info = _lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise GoodnotesError(f"{purpose} is not a directory: {path}")


def _source_lstat(path: Path) -> os.stat_result:
    try:
        return _lstat(path)
    except GoodnotesError as exc:
        if isinstance(_os_cause(exc), FileNotFoundError):
            raise _InventoryChanged(
                f"Source path disappeared while it was inspected: {path}"
            ) from exc
        raise


def _source_root_info(path: Path) -> os.stat_result:
    """Inspect the selected Library root before child inventory begins.

    A missing root is an invalid source selection, unlike a child disappearing
    after a valid root has been discovered, which remains a transient inventory
    race through ``_source_lstat``.
    """

    try:
        info = path.lstat()
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc,
            operation="inspect_source_library",
            path=path,
            kind="source",
        ) from exc
    if stat.S_ISLNK(info.st_mode):
        raise GoodnotesError("The source Library directory must not be a symlink.")
    return info


def _require_source_directory(path: Path, *, purpose: str) -> None:
    info = _source_lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise GoodnotesError(f"{purpose} is not a directory: {path}")


def _input_signature(source: Path, *, include_root: bool = False) -> str:
    """Fingerprint the complete copied input inventory using metadata only."""

    source_info = _source_root_info(source)
    if not stat.S_ISDIR(source_info.st_mode):
        raise GoodnotesError(f"Source Library path is not a directory: {source}")
    database_dir = source / "Databases"
    attachments_dir = source / "Attachments"
    _require_source_directory(database_dir, purpose="Source database path")
    _require_source_directory(attachments_dir, purpose="Source attachments path")

    entries: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
    paths_to_recheck: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    if include_root:
        root_key = _stat_key(source_info)
        entries.append((".", root_key))
        paths_to_recheck.append((source, root_key))

    def add(path: Path, relative: Path, *, directory: bool) -> os.stat_result:
        info = _source_lstat(path)
        expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected_type:
            kind = "directory" if directory else "regular file"
            raise GoodnotesError(f"Snapshot input is not a {kind}: {path}")
        key = _stat_key(info)
        entries.append((relative.as_posix(), key))
        paths_to_recheck.append((path, key))
        return info

    database_dir_before = add(database_dir, Path("Databases"), directory=True)
    add(attachments_dir, Path("Attachments"), directory=True)

    try:
        with os.scandir(database_dir) as iterator:
            database_children = sorted(iterator, key=lambda entry: entry.name)
    except FileNotFoundError as exc:
        raise _InventoryChanged(
            f"Source database directory disappeared while it was inspected: {database_dir}"
        ) from exc
    except OSError as exc:
        raise GoodnotesError(
            f"Could not inspect source database directory: {database_dir}"
        ) from exc
    names = {entry.name for entry in database_children}
    malformed_entries: list[str] = []
    for database_name in _DATABASE_NAMES:
        database_path = database_dir / database_name
        if database_name in names:
            add(database_path, Path("Databases") / database_name, directory=False)
        sidecars = sorted(name for name in names if name.startswith(database_name + "-"))
        if sidecars and database_name not in names:
            malformed_entries.append(
                f"SQLite sidecars exist without their database: {database_path}"
            )
        for sidecar_name in sidecars:
            if not any(
                sidecar_name == database_name + suffix for suffix in _DATABASE_SIDECAR_SUFFIXES
            ):
                malformed_entries.append(
                    f"Unsupported SQLite sidecar: {database_dir / sidecar_name}"
                )
                continue
            add(
                database_dir / sidecar_name,
                Path("Databases") / sidecar_name,
                directory=False,
            )
    missing_projection = "projection.sqlite" not in names

    def scan(directory: Path, relative: Path) -> None:
        before = add(directory, relative, directory=True)
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except FileNotFoundError as exc:
            raise _InventoryChanged(
                f"Source directory disappeared while it was inspected: {directory}"
            ) from exc
        except OSError as exc:
            raise GoodnotesError(
                f"Could not inspect source attachment directory: {directory}"
            ) from exc
        for child in children:
            child_path = directory / child.name
            child_relative = relative / child.name
            info = _source_lstat(child_path)
            if stat.S_ISDIR(info.st_mode):
                key = _stat_key(info)
                entries.append((child_relative.as_posix(), key))
                paths_to_recheck.append((child_path, key))
                scan(child_path, child_relative)
            elif stat.S_ISREG(info.st_mode):
                key = _stat_key(info)
                entries.append((child_relative.as_posix(), key))
                paths_to_recheck.append((child_path, key))
            else:
                raise GoodnotesError(f"Unsupported snapshot input type: {child_path}")
        after = _source_lstat(directory)
        if _stat_key(before) != _stat_key(after):
            raise _InventoryChanged(f"Source directory changed while it was inspected: {directory}")

    scan(attachments_dir, Path("Attachments"))
    for name in _NATIVE_STORE_NAMES:
        if name in names:
            scan(database_dir / name, Path("Databases") / name)
    database_dir_after = _source_lstat(database_dir)
    try:
        with os.scandir(database_dir) as iterator:
            names_after = {entry.name for entry in iterator}
    except FileNotFoundError as exc:
        raise _InventoryChanged(
            f"Source database directory disappeared while it was fingerprinted: {database_dir}"
        ) from exc
    except OSError as exc:
        raise GoodnotesError(
            f"Could not inspect source database directory: {database_dir}"
        ) from exc
    if _stat_key(database_dir_before) != _stat_key(database_dir_after) or names != names_after:
        raise _InventoryChanged(
            f"Source database directory changed while it was inspected: {database_dir}"
        )
    if malformed_entries:
        raise GoodnotesError(malformed_entries[0])
    if missing_projection:
        raise GoodnotesError(
            f"Required projection database is missing: {database_dir / 'projection.sqlite'}"
        )

    for path, expected_key in paths_to_recheck:
        if _stat_key(_source_lstat(path)) != expected_key:
            raise _InventoryChanged(f"Snapshot input changed while it was fingerprinted: {path}")
    entries.sort(key=lambda entry: entry[0])
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _source_input_signature(source: Path) -> str:
    try:
        return _input_signature(source)
    except (GoodnotesError, OSError) as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inventory_source_library", path=source, kind="source"
            ) from exc
        raise
