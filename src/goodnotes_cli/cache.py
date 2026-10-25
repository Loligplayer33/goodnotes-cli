"""Manage the CLI's immutable snapshot cache and read-view locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    StaleSnapshotError,
    _os_cause,
    _permission_denied,
)
from .safety import (
    SNAPSHOT_MARKER,
    Sandbox,
    _require_not_production,
    path_contains,
    paths_overlap,
    safe_remove_tree,
    stat_key,
)
from .snapshot import _InventoryChanged, create_snapshot

if TYPE_CHECKING:
    from .config import Settings

_CACHE_MARKER = ".goodnotes-cli-cache.json"
_SYNC_LOCK = ".sync.lock"
_VIEW_LOCK = ".view.lock"
_GENERATION = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")
_STAGING = re.compile(r"^\.staging-[0-9a-f]{32}$")
_SIDECAR_KEY = re.compile(r"^[0-9a-f]{24}$")
_DATABASES = ("projection.sqlite", "fts.sqlite")
_DATABASE_SIDECARS = ("-wal", "-shm", "-journal")
_NATIVE_STORES = ("notes_main.db",)


def _stat_key(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return stat_key(info)


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


def _prepare_state(data_dir: Path, source: Path | None = None) -> tuple[Path, Path]:
    requested = Path(data_dir).expanduser()
    if requested.is_symlink():
        raise GoodnotesError("The CLI cache directory must not be a symlink.")
    state = requested.resolve(strict=False)
    _require_not_production(state, purpose="CLI cache storage")
    if source is not None and paths_overlap(state, source):
        raise GoodnotesError("The CLI cache and source Library must not overlap.")

    try:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        raise GoodnotesError(f"Could not create CLI cache directory: {state}") from exc
    if requested.is_symlink():
        raise GoodnotesError("The CLI cache directory must not be a symlink.")
    try:
        resolved_state = requested.resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(f"Could not resolve CLI cache directory: {requested}") from exc
    if resolved_state != state:
        raise GoodnotesError("The CLI cache directory changed while it was being prepared.")
    _require_directory(state, purpose="CLI cache directory")
    _require_not_production(state, purpose="CLI cache storage")
    if source is not None and paths_overlap(state, source):
        raise GoodnotesError("The CLI cache and source Library must not overlap.")
    try:
        state.chmod(0o700)
    except OSError as exc:
        raise GoodnotesError(f"Could not protect CLI cache directory: {state}") from exc

    snapshots = state / "snapshots"
    if snapshots.is_symlink():
        raise GoodnotesError("The snapshots directory must not be a symlink.")
    if not snapshots.exists():
        try:
            snapshots.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise GoodnotesError(f"Could not create snapshots directory: {snapshots}") from exc
    _require_directory(snapshots, purpose="Snapshots path")
    return state, snapshots


def _lock_error(
    path: Path,
    operation: str,
    message: str,
    *,
    exc: OSError | None = None,
    missing_is_uninitialized: bool = False,
    writer: bool,
) -> CacheAccessError:
    if exc is not None:
        return CacheAccessError.from_exception(
            exc,
            f"{message}: {path}",
            operation=operation,
            path=path,
            cache_dir=path.parent,
            missing_is_uninitialized=missing_is_uninitialized,
            writer=writer,
        )
    return CacheAccessError(
        "cache_invalid",
        f"{message}: {path}",
        operation=operation,
        path=path,
        cache_dir=path.parent,
        writer=writer,
    )


def _verify_lock_path(path: Path, opened: os.stat_result, *, operation: str) -> None:
    try:
        current = path.lstat()
    except OSError as exc:
        raise CacheAccessError.from_exception(
            exc,
            f"Could not verify cache lock after acquisition: {path}",
            operation=operation,
            path=path,
            cache_dir=path.parent,
            writer=operation != "read_view_lock",
        ) from exc
    if not stat.S_ISREG(current.st_mode) or (current.st_dev, current.st_ino) != (
        opened.st_dev,
        opened.st_ino,
    ):
        raise _lock_error(
            path,
            operation,
            "The cache lock changed while it was being acquired",
            writer=operation != "read_view_lock",
        )


@contextmanager
def _file_lock(path: Path, *, exclusive: bool, create: bool = True) -> Generator[None, None, None]:
    """Lock a persistent CLI-owned file, optionally requiring an existing read-only open."""

    operation = (
        "read_view_lock"
        if not create
        else ("sync_lock" if path.name == _SYNC_LOCK else "sync_view_lock")
    )
    if not create and exclusive:
        raise ValueError("An existing read-only cache lock can only be acquired shared.")

    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        if not create:
            raise _lock_error(
                path,
                operation,
                "The existing cache view lock is missing",
                exc=exc,
                missing_is_uninitialized=True,
                writer=False,
            ) from exc
        before = None
    except OSError as exc:
        raise _lock_error(
            path,
            operation,
            "Could not inspect the cache lock",
            exc=exc,
            writer=create,
        ) from exc
    if before is not None and not stat.S_ISREG(before.st_mode):
        raise _lock_error(
            path,
            operation,
            "The cache lock must be a regular, non-symlink file",
            writer=create,
        )

    flags = getattr(os, "O_NOFOLLOW", 0)
    if create:
        flags |= os.O_CREAT | os.O_RDWR
    else:
        # O_NONBLOCK prevents a raced replacement with a FIFO from hanging the reader.
        flags |= os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise _lock_error(
            path,
            operation,
            "Could not open the cache lock",
            exc=exc,
            missing_is_uninitialized=not create,
            writer=create,
        ) from exc

    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or (
            before is not None and (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
        ):
            raise _lock_error(
                path,
                operation,
                "The cache lock is invalid or changed while it was being opened",
                writer=create,
            )
        _verify_lock_path(path, opened, operation=operation)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            raise _lock_error(
                path,
                operation,
                "Could not acquire the cache lock",
                exc=exc,
                writer=create,
            ) from exc
        # Recheck after flock in case the lock path changed while acquisition waited.
        _verify_lock_path(path, opened, operation=operation)
        yield
    finally:
        os.close(descriptor)


def _validate_cache_marker(state: Path, *, create: bool) -> bool:
    marker = state / _CACHE_MARKER
    if marker.is_symlink():
        raise GoodnotesError("The CLI cache ownership marker must not be a symlink.")
    if not marker.exists():
        if not create:
            return False
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(marker, flags, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                json.dump({"format_version": 1, "owner": "goodnotes-cli"}, stream)
                stream.flush()
                os.fsync(stream.fileno())
        except FileExistsError:
            return _validate_cache_marker(state, create=False)
        except OSError as exc:
            raise GoodnotesError(f"Could not write CLI cache ownership marker: {marker}") from exc
        return True
    try:
        info = marker.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise GoodnotesError("The CLI cache ownership marker must be a regular file.")
        value = json.loads(marker.read_text(encoding="utf-8"))
    except GoodnotesError:
        raise
    except (OSError, ValueError) as exc:
        raise GoodnotesError("The CLI cache ownership marker is invalid.") from exc
    if value != {"format_version": 1, "owner": "goodnotes-cli"}:
        raise GoodnotesError("The CLI cache ownership marker is invalid.")
    return True


def _active_path(data: dict[str, Any]) -> str | None:
    snapshot = data.get("snapshot")
    absolute_path = data.get("absolutePath")
    if snapshot is not None:
        if not isinstance(snapshot, str):
            return None
        if absolute_path is not None and absolute_path != snapshot:
            raise GoodnotesError("The active snapshot paths disagree.")
        return snapshot
    return absolute_path if isinstance(absolute_path, str) else None


def _read_pointer(
    state: Path, snapshots: Path, *, allow_missing_snapshot: bool = False
) -> tuple[dict[str, Any], Path] | None:
    if snapshots.is_symlink():
        raise GoodnotesError("The snapshots directory must not be a symlink.")
    _require_directory(snapshots, purpose="Snapshots path")
    pointer = state / "active.json"
    if pointer.is_symlink():
        raise GoodnotesError("The active library pointer must not be a symlink.")
    if not pointer.exists():
        return None
    try:
        pointer_info = pointer.lstat()
        if not stat.S_ISREG(pointer_info.st_mode):
            raise GoodnotesError("The active library pointer must be a regular file.")
        data = json.loads(pointer.read_text(encoding="utf-8"))
    except GoodnotesError:
        raise
    except (OSError, ValueError) as exc:
        raise GoodnotesError("The active library pointer is invalid.") from exc
    if not isinstance(data, dict):
        raise GoodnotesError("The active library pointer is invalid.")
    raw_path = _active_path(data)
    if raw_path is None:
        raise GoodnotesError("The active library pointer is invalid.")
    root = Path(raw_path).expanduser()
    if (
        not root.is_absolute()
        or root.parent != snapshots
        or not path_contains(snapshots, root)
        or not _GENERATION.fullmatch(root.name)
    ):
        raise GoodnotesError("The active library pointer must name a generated cache snapshot.")
    if root.is_symlink():
        raise GoodnotesError("The active cache snapshot must not be a symlink.")
    if not root.exists() and not allow_missing_snapshot:
        raise GoodnotesError("The active cache snapshot is missing.")
    return data, root


def _load_active(
    state: Path, snapshots: Path
) -> tuple[dict[str, Any], Path, Sandbox | None] | None:
    pointer = _read_pointer(state, snapshots, allow_missing_snapshot=True)
    if pointer is None:
        return None
    data, root = pointer
    try:
        sandbox = Sandbox.load(root)
    except GoodnotesError:
        # A damaged active generation can be replaced by the next verified capture.
        sandbox = None
    return data, root, sandbox


def _active_cache_signature(
    active: tuple[dict[str, Any], Path, Sandbox | None] | None,
) -> str | None:
    if active is None or active[2] is None:
        return None
    try:
        return _input_signature(active[2].library_dir, include_root=True)
    except GoodnotesError:
        return None


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
    for database_name in _DATABASES:
        database_path = database_dir / database_name
        if database_name in names:
            add(database_path, Path("Databases") / database_name, directory=False)
        sidecars = sorted(name for name in names if name.startswith(database_name + "-"))
        if sidecars and database_name not in names:
            malformed_entries.append(
                f"SQLite sidecars exist without their database: {database_path}"
            )
        for sidecar_name in sidecars:
            if not any(sidecar_name == database_name + suffix for suffix in _DATABASE_SIDECARS):
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
    for name in _NATIVE_STORES:
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


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_captured_at(root: Path, marker_data: dict[str, Any] | None = None) -> str:
    marker = root / SNAPSHOT_MARKER
    if marker_data is None:
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GoodnotesError(f"Could not read snapshot capture time: {marker}") from exc
    value = marker_data.get("created_at_utc")
    if not isinstance(value, str):
        raise GoodnotesError(f"Snapshot marker has no capture time: {marker}")
    return value


def _snapshot_capture_stats(
    root: Path,
    *,
    unchanged: bool = False,
    marker_data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    marker = root / SNAPSHOT_MARKER
    if marker_data is None:
        try:
            marker_data = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise GoodnotesError(f"Could not read snapshot capture statistics: {marker}") from exc
    stats = marker_data.get("capture_stats", {})
    result = dict(stats) if isinstance(stats, dict) else {}
    if unchanged:
        result["scope"] = "unchanged"
        for name in (
            "source_captured_files",
            "source_captured_bytes",
            "cache_reused_files",
            "cache_reused_bytes",
            "source_bytes_hashed",
            "cloned_files",
            "cloned_logical_bytes",
            "byte_copied_files",
            "byte_copied_bytes",
        ):
            result[name] = 0
        result["attempts"] = 0
        result["capture_seconds"] = 0.0
        result["bytes_are_logical"] = True
    return result


def _source_input_signature(source: Path) -> str:
    try:
        return _input_signature(source)
    except (GoodnotesError, OSError) as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inventory_source_library", path=source, kind="source"
            ) from exc
        raise


def _write_json_atomic(state: Path, name: str, data: dict[str, Any]) -> None:
    target = state / name
    if target.is_symlink():
        label = "active library pointer" if name == "active.json" else "cache file"
        raise GoodnotesError(f"The {label} must not be a symlink.")
    if target.exists() and not stat.S_ISREG(target.lstat().st_mode):
        raise GoodnotesError(f"The cache file must be a regular file: {target}")
    temporary = state / f".{name.removesuffix('.json')}-{uuid4().hex}.json"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(data, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        if target.is_symlink():
            raise GoodnotesError(f"The active library pointer must not be a symlink: {target}")
        os.replace(temporary, target)
        try:
            directory_fd = os.open(state, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        except OSError:
            directory_fd = None
        if directory_fd is not None:
            try:
                try:
                    os.fsync(directory_fd)
                except OSError:
                    pass
            finally:
                os.close(directory_fd)
    except GoodnotesError:
        raise
    except OSError as exc:
        raise GoodnotesError(f"Could not update CLI cache file: {target}") from exc
    finally:
        if temporary.exists() and not temporary.is_symlink():
            try:
                temporary.unlink()
            except OSError:
                pass


def _safe_remove_tree(path: Path) -> None:
    """Remove a CLI-owned tree through the shared no-follow cleanup routine."""

    safe_remove_tree(path)


def _assert_safe_tree(path: Path) -> None:
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect generated snapshot: {directory}") from exc
        for child in children:
            child_path = directory / child.name
            info = _lstat(child_path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child_path)
            elif not stat.S_ISREG(info.st_mode):
                raise GoodnotesError(f"Refusing to remove a special cache file: {child_path}")


def _remove_generation(snapshots: Path, root: Path) -> None:
    _assert_safe_tree(root)
    _remove_registration_sidecar(root)
    tombstone = snapshots / f".staging-{uuid4().hex}"
    if tombstone.exists() or tombstone.is_symlink():
        raise GoodnotesError(f"Refusing to overwrite a staging snapshot: {tombstone}")
    try:
        os.rename(root, tombstone)
    except OSError as exc:
        raise GoodnotesError(f"Could not stage generated snapshot cleanup: {root}") from exc
    _safe_remove_tree(tombstone)


def _clean_abandoned_staging(snapshots: Path) -> None:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect snapshots directory: {snapshots}") from exc
    for entry in entries:
        if not _STAGING.fullmatch(entry.name):
            continue
        staging = snapshots / entry.name
        if staging.is_symlink():
            raise GoodnotesError(f"A staging snapshot must not be a symlink: {staging}")
        if not staging.is_dir():
            raise GoodnotesError(f"A staging snapshot must be a directory: {staging}")
        _safe_remove_tree(staging)


def _validate_generated_snapshots(snapshots: Path) -> None:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect snapshots directory: {snapshots}") from exc
    for entry in entries:
        if not _GENERATION.fullmatch(entry.name):
            continue
        root = snapshots / entry.name
        if root.is_symlink():
            raise GoodnotesError(f"A generated snapshot must not be a symlink: {root}")


def _has_valid_marker(root: Path) -> bool:
    marker = root / SNAPSHOT_MARKER
    if marker.is_symlink():
        raise GoodnotesError(f"A generated snapshot marker must not be a symlink: {marker}")
    if not marker.exists():
        return False
    try:
        if not stat.S_ISREG(marker.lstat().st_mode):
            return False
        value = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("format_version") == 1


def _remove_registration_sidecar(root: Path) -> None:
    key = hashlib.sha256(os.fsencode(root.resolve(strict=True))).hexdigest()[:24]
    sidecar_parent = root.parent / ".goodnotes-cli"
    sidecar = sidecar_parent / key
    if sidecar_parent.is_symlink() or sidecar.is_symlink():
        raise GoodnotesError("The annotated-PDF registration sidecar must not be a symlink.")
    if not _SIDECAR_KEY.fullmatch(sidecar.name):
        return
    if sidecar.exists():
        if not sidecar.is_dir():
            raise GoodnotesError(
                f"The annotated-PDF registration sidecar is not a directory: {sidecar}"
            )
        _safe_remove_tree(sidecar)
    try:
        sidecar_parent.rmdir()
    except OSError:
        pass


def _prune_snapshots(
    snapshots: Path, active_name: str, previous_active_name: str | None = None
) -> list[str]:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        return [f"Could not inspect snapshots directory {snapshots}: {exc}"]
    warnings: list[str] = []
    for entry in entries:
        if not _GENERATION.fullmatch(entry.name):
            continue
        root = snapshots / entry.name
        try:
            if root.is_symlink():
                raise GoodnotesError(f"A generated snapshot must not be a symlink: {root}")
            if not root.is_dir() or entry.name == active_name:
                continue
            if entry.name != previous_active_name:
                if not _has_valid_marker(root):
                    continue
                # A valid marker and snapshot structure prove CLI ownership before pruning.
                Sandbox.load(root)
            _remove_generation(snapshots, root)
        except (GoodnotesError, OSError) as exc:
            warnings.append(f"Could not prune generated snapshot {entry.name}: {exc}")
    return warnings


def _recognized_managed_state(state: Path) -> bool:
    """Return whether an explicit snapshot belongs to a cache that this CLI owns."""

    if state.is_symlink() or not state.exists() or not state.is_dir():
        return False
    snapshots = state / "snapshots"
    if snapshots.is_symlink() or not snapshots.is_dir():
        return False
    if _validate_cache_marker(state, create=False):
        return True
    pointer = _read_pointer(state, snapshots)
    if pointer is None:
        return False
    _, root = pointer
    try:
        Sandbox.load(root)
    except GoodnotesError:
        return False
    return True


def _managed_state_for_explicit(path: Path) -> Path | None:
    requested = Path(path).expanduser()
    # Normalize dot segments and parent aliases before deciding whether this is a
    # managed generation. Otherwise an explicit path such as
    # snapshots/<generation>/../<generation> can bypass the shared view lock.
    # An explicit sandbox must already exist. Strict resolution also rejects symlink
    # loops instead of leaving an unresolved path that could bypass managed-state checks.
    resolved = requested.resolve(strict=True)
    if resolved.parent.name != "snapshots":
        return None
    resolved_state = resolved.parent.parent.resolve(strict=False)
    _require_not_production(resolved_state, purpose="CLI cache storage")
    if _recognized_managed_state(resolved_state):
        return resolved_state
    return None


def _result(
    *,
    status: str,
    root: Path,
    checked_at: str,
    captured_at: str | None,
    warnings: list[str] | None = None,
    source_changed_during_refresh: bool = False,
    capture_stats: dict[str, Any] | None = None,
) -> dict[str, object]:
    result: dict[str, object] = {
        "status": status,
        "snapshot": str(root),
        "snapshot_id": root.name,
        "checked_at": checked_at,
    }
    if captured_at is not None:
        result["captured_at"] = captured_at
    result["source_changed_during_refresh"] = source_changed_during_refresh
    if capture_stats is not None:
        result["capture_stats"] = capture_stats
    final_warnings = list(warnings or [])
    if source_changed_during_refresh:
        final_warnings.append(
            "Source files changed while refresh ran. This validated read view is as of "
            "captured_at; run sync again to check for later changes."
        )
    if final_warnings:
        result["warnings"] = final_warnings
    return result


def refresh_cache(
    source_library: Path, data_dir: Path, *, force: bool = False
) -> dict[str, object]:
    """Capture or reuse one immutable snapshot and atomically publish it as active."""

    requested_source = Path(source_library).expanduser()
    try:
        if requested_source.is_symlink():
            raise GoodnotesError("The source Library directory must not be a symlink.")
        source = requested_source.resolve(strict=True)
    except OSError as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="resolve_source_library", path=requested_source, kind="source"
            ) from exc
        raise GoodnotesError(
            f"Source Library directory does not exist: {requested_source}"
        ) from exc
    try:
        _require_directory(source, purpose="Source Library path")
    except (GoodnotesError, OSError) as exc:
        if _permission_denied(exc):
            raise FilesystemAccessError.from_exception(
                exc, operation="inspect_source_library", path=source, kind="source"
            ) from exc
        raise
    requested_data_dir = Path(data_dir).expanduser()
    try:
        state, snapshots = _prepare_state(requested_data_dir, source)
    except (GoodnotesError, OSError, RuntimeError) as exc:
        if not isinstance(exc, RuntimeError) and _os_cause(exc) is None:
            raise
        raise CacheAccessError.from_exception(
            exc,
            str(exc),
            operation="prepare_cache_directory",
            path=requested_data_dir,
            cache_dir=requested_data_dir,
            missing_is_uninitialized=True,
            writer=True,
        ) from exc

    with _file_lock(state / _SYNC_LOCK, exclusive=True):
        _validate_cache_marker(state, create=True)
        _validate_generated_snapshots(snapshots)
        with _file_lock(state / _VIEW_LOCK, exclusive=False):
            active = _load_active(state, snapshots)
        source_signature = _source_input_signature(source)

        # Refreshes serialize on .sync.lock. The shared view lock protects pointer resolution
        # while allowing existing readers to continue during the source capture.
        can_check_active_cache = (
            not force
            and active is not None
            and active[2] is not None
            and active[0].get("source_path") == str(source)
            and active[0].get("source_signature") == source_signature
            and active[0].get("cache_signature") is not None
        )
        active_cache_signature = _active_cache_signature(active) if can_check_active_cache else None

        if (
            not force
            and active is not None
            and active[2] is not None
            and active[0].get("source_path") == str(source)
            and active[0].get("source_signature") == source_signature
            and active[0].get("cache_signature") is not None
            and active[0].get("cache_signature") == active_cache_signature
        ):
            checked_at = _timestamp()
            with _file_lock(state / _VIEW_LOCK, exclusive=True):
                active = _load_active(state, snapshots)
                if (
                    active is not None
                    and active[2] is not None
                    and active[0].get("source_path") == str(source)
                    and active[0].get("source_signature") == source_signature
                    and active[0].get("cache_signature") is not None
                    and active[0].get("cache_signature") == active_cache_signature
                ):
                    data, root, _ = active
                    captured_at = data.get("captured_at")
                    data = {**data, "checked_at": checked_at}
                    data.setdefault("absolutePath", str(root))
                    data.setdefault("snapshot", str(root))
                    data.setdefault("snapshot_id", root.name)
                    cleanup_warnings: list[str] = []
                    try:
                        _clean_abandoned_staging(snapshots)
                    except (GoodnotesError, OSError) as exc:
                        cleanup_warnings.append(f"Could not clean abandoned staging: {exc}")
                    _write_json_atomic(state, "active.json", data)
                    cleanup_warnings.extend(_prune_snapshots(snapshots, root.name))
                    return _result(
                        status="unchanged",
                        root=root,
                        checked_at=checked_at,
                        captured_at=captured_at if isinstance(captured_at, str) else None,
                        warnings=cleanup_warnings,
                        capture_stats=_snapshot_capture_stats(
                            root,
                            unchanged=True,
                            marker_data=active[2].marker_data,
                        ),
                    )

        checked_at = _timestamp()
        captured_at = _timestamp()
        generation = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
        staging = snapshots / f".staging-{uuid4().hex}"
        destination = snapshots / generation
        if destination.exists() or destination.is_symlink():
            raise GoodnotesError(f"Refusing to overwrite a cache generation: {destination}")

        published = False
        try:
            previous_snapshot = (
                active[1]
                if not force
                and active is not None
                and active[2] is not None
                and active[0].get("source_path") == str(source)
                else None
            )
            if previous_snapshot is None:
                create_snapshot(source, staging)
            else:
                create_snapshot(source, staging, previous_snapshot=previous_snapshot)
            sandbox = Sandbox.load(staging)
            if destination.exists() or destination.is_symlink():
                raise GoodnotesError(f"Refusing to overwrite a cache generation: {destination}")
            os.replace(staging, destination)
            sandbox = Sandbox.load(destination)
            cache_signature = _input_signature(sandbox.library_dir, include_root=True)
            captured_at = _snapshot_captured_at(destination, sandbox.marker_data)
            try:
                signature_after = _source_input_signature(source)
            except _InventoryChanged:
                source_changed_during_refresh = True
            else:
                source_changed_during_refresh = signature_after != source_signature
            saved_signature = None if source_changed_during_refresh else source_signature
            pointer_data: dict[str, Any] = {
                "absolutePath": str(destination),
                "snapshot": str(destination),
                "snapshot_id": destination.name,
                "source_path": str(source),
                "cache_signature": cache_signature,
                "checked_at": checked_at,
                "captured_at": captured_at,
                "source_changed_during_refresh": source_changed_during_refresh,
            }
            if saved_signature is not None:
                pointer_data["source_signature"] = saved_signature

            with _file_lock(state / _VIEW_LOCK, exclusive=True):
                # Re-check the pointer immediately before replacing it. This also rejects an
                # unexpected symlink or malformed state introduced during capture.
                previous = _read_pointer(state, snapshots, allow_missing_snapshot=True)
                previous_name = previous[1].name if previous is not None else None
                cleanup_warnings = []
                try:
                    _clean_abandoned_staging(snapshots)
                except (GoodnotesError, OSError) as exc:
                    cleanup_warnings.append(f"Could not clean abandoned staging: {exc}")
                _write_json_atomic(state, "active.json", pointer_data)
                published = True
                cleanup_warnings.extend(
                    _prune_snapshots(snapshots, destination.name, previous_name)
                )
        except BaseException:
            if not published:
                try:
                    pointer = _read_pointer(state, snapshots)
                    published = pointer is not None and pointer[1] == destination
                except GoodnotesError:
                    pass
            if not published:
                if staging.exists() and not staging.is_symlink():
                    try:
                        _safe_remove_tree(staging)
                    except GoodnotesError:
                        # Keep unsafe candidates for inspection instead of following links.
                        pass
                if destination.exists() and not destination.is_symlink():
                    try:
                        _remove_generation(snapshots, destination)
                    except GoodnotesError:
                        pass
            raise

        return _result(
            status="ready",
            root=sandbox.root,
            checked_at=checked_at,
            captured_at=captured_at,
            warnings=cleanup_warnings,
            source_changed_during_refresh=source_changed_during_refresh,
            capture_stats=_snapshot_capture_stats(sandbox.root, marker_data=sandbox.marker_data),
        )


def _read_cache_state(requested: Path) -> Path:
    try:
        if requested.is_symlink():
            raise CacheAccessError(
                "cache_invalid",
                f"The CLI cache directory must not be a symlink: {requested}",
                operation="resolve_cache_directory",
                path=requested,
                cache_dir=requested,
            )
        _require_not_production(requested.absolute(), purpose="CLI cache storage")
        state = requested.resolve(strict=True)
        info = state.lstat()
        if requested.is_symlink():
            raise CacheAccessError(
                "cache_invalid",
                f"The CLI cache directory must not be a symlink: {requested}",
                operation="resolve_cache_directory",
                path=requested,
                cache_dir=requested,
            )
    except (OSError, RuntimeError) as exc:
        raise CacheAccessError.from_exception(
            exc,
            f"Could not resolve the CLI cache directory: {requested}",
            operation="resolve_cache_directory",
            path=requested,
            cache_dir=requested,
            missing_is_uninitialized=True,
        ) from exc
    _require_not_production(state, purpose="CLI cache storage")
    if not stat.S_ISDIR(info.st_mode):
        raise CacheAccessError(
            "cache_invalid",
            f"The CLI cache path is not a directory: {state}",
            operation="resolve_cache_directory",
            path=state,
            cache_dir=state,
        )
    return state


@contextmanager
def read_view(settings: Settings) -> Generator[Sandbox, None, None]:
    """Resolve one snapshot under a shared lock and keep it protected through the read."""

    settings.cache_dir = None
    explicit = getattr(settings, "sandbox", None)
    lock_state: Path | None = None
    if explicit is not None:
        try:
            lock_state = _managed_state_for_explicit(Path(explicit))
        except (GoodnotesError, OSError, RuntimeError) as exc:
            if not isinstance(exc, RuntimeError) and _os_cause(exc) is None:
                raise
            raise CacheAccessError.from_exception(
                exc,
                f"Could not inspect the cache for the explicit snapshot: {explicit}",
                operation="resolve_explicit_sandbox",
                path=Path(explicit),
            ) from exc
    else:
        configured = getattr(settings, "data_dir", None)
        state_requested = Path(configured).expanduser() if configured is not None else None
        if state_requested is None:
            # Import lazily so Settings remains a type-only dependency for this module.
            from .config import default_data_dir

            state_requested = default_data_dir().expanduser()
        lock_state = _read_cache_state(state_requested)
    if explicit is not None and lock_state is not None:
        lock_state = _read_cache_state(lock_state)

    if lock_state is None:
        sandbox = settings.load_sandbox()
        expected = getattr(settings, "expect_snapshot", None)
        if expected is not None and sandbox.root.name != expected:
            raise StaleSnapshotError(
                f"The active snapshot is {sandbox.root.name}, but {expected} was requested."
            )
        settings.snapshot_id = sandbox.root.name
        yield sandbox
        return

    view_lock = lock_state / _VIEW_LOCK
    settings.cache_dir = lock_state
    with _file_lock(view_lock, exclusive=False, create=False):
        snapshots = lock_state / "snapshots"
        try:
            sandbox = settings.load_sandbox()
        except CacheAccessError:
            raise
        except (GoodnotesError, OSError, RuntimeError) as exc:
            raise CacheAccessError.from_exception(
                exc,
                f"Could not resolve a valid managed cache snapshot: {exc}",
                operation="resolve_read_view",
                path=lock_state,
                cache_dir=lock_state,
            ) from exc
        if sandbox.root.parent != snapshots or (
            explicit is None and not _GENERATION.fullmatch(sandbox.root.name)
        ):
            raise CacheAccessError(
                "cache_invalid",
                "The resolved cache snapshot must be a direct generated child.",
                operation="validate_active_snapshot",
                path=sandbox.root,
                cache_dir=lock_state,
            )
        expected = getattr(settings, "expect_snapshot", None)
        if expected is not None and sandbox.root.name != expected:
            raise StaleSnapshotError(
                f"The active snapshot is {sandbox.root.name}, but {expected} was requested."
            )
        settings.snapshot_id = sandbox.root.name
        yield sandbox
