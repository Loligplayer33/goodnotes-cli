"""Manage the CLI's immutable snapshot cache and read-view locks."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from .errors import GoodnotesError, StaleSnapshotError
from .safety import (
    SNAPSHOT_MARKER,
    Sandbox,
    _overlaps,
    _require_not_production,
)
from .snapshot import create_snapshot

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
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect cache input: {path}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise GoodnotesError(f"Symlinks are not allowed in cache paths: {path}")
    return info


def _require_directory(path: Path, *, purpose: str) -> None:
    info = _lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise GoodnotesError(f"{purpose} is not a directory: {path}")


def _prepare_state(data_dir: Path, source: Path | None = None) -> tuple[Path, Path]:
    requested = Path(data_dir).expanduser()
    if requested.is_symlink():
        raise GoodnotesError("The CLI cache directory must not be a symlink.")
    state = requested.resolve(strict=False)
    _require_not_production(state, purpose="CLI cache storage")
    if source is not None and _overlaps(state, source):
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
    if source is not None and _overlaps(state, source):
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


@contextmanager
def _file_lock(path: Path, *, exclusive: bool) -> Iterator[None]:
    """Lock a persistent CLI-owned file without following a link."""

    if path.is_symlink():
        raise GoodnotesError(f"The cache lock must not be a symlink: {path}")
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise GoodnotesError(f"Could not open cache lock: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise GoodnotesError(f"The cache lock is not a regular file: {path}")
        try:
            current = path.lstat()
        except OSError as exc:
            raise GoodnotesError(f"Could not verify cache lock: {path}") from exc
        if stat.S_ISLNK(current.st_mode) or (current.st_dev, current.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ):
            raise GoodnotesError(f"The cache lock changed while it was being opened: {path}")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        except OSError as exc:
            raise GoodnotesError(f"Could not lock the CLI cache: {path}") from exc
    except BaseException:
        os.close(descriptor)
        raise
    try:
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
    value = data.get("absolutePath", data.get("snapshot"))
    return value if isinstance(value, str) else None


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
    if not root.is_absolute() or root.parent != snapshots or not _GENERATION.fullmatch(root.name):
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

    if source.is_symlink():
        raise GoodnotesError("The source Library directory must not be a symlink.")
    _require_directory(source, purpose="Source Library path")
    database_dir = source / "Databases"
    attachments_dir = source / "Attachments"
    _require_directory(database_dir, purpose="Source database path")
    _require_directory(attachments_dir, purpose="Source attachments path")

    entries: list[tuple[str, tuple[int, int, int, int, int, int]]] = []
    paths_to_recheck: list[tuple[Path, tuple[int, int, int, int, int, int]]] = []
    if include_root:
        root_key = _stat_key(_lstat(source))
        entries.append((".", root_key))
        paths_to_recheck.append((source, root_key))

    def add(path: Path, relative: Path, *, directory: bool) -> os.stat_result:
        info = _lstat(path)
        expected_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if not expected_type:
            kind = "directory" if directory else "regular file"
            raise GoodnotesError(f"Snapshot input is not a {kind}: {path}")
        key = _stat_key(info)
        entries.append((relative.as_posix(), key))
        paths_to_recheck.append((path, key))
        return info

    add(database_dir, Path("Databases"), directory=True)
    add(attachments_dir, Path("Attachments"), directory=True)

    try:
        database_children = sorted(os.scandir(database_dir), key=lambda entry: entry.name)
    except OSError as exc:
        raise GoodnotesError(
            f"Could not inspect source database directory: {database_dir}"
        ) from exc
    names = {entry.name for entry in database_children}
    for database_name in _DATABASES:
        database_path = database_dir / database_name
        if database_name in names:
            add(database_path, Path("Databases") / database_name, directory=False)
        sidecars = sorted(name for name in names if name.startswith(database_name + "-"))
        if sidecars and database_name not in names:
            raise GoodnotesError(f"SQLite sidecars exist without their database: {database_path}")
        for sidecar_name in sidecars:
            if not any(sidecar_name == database_name + suffix for suffix in _DATABASE_SIDECARS):
                raise GoodnotesError(f"Unsupported SQLite sidecar: {database_dir / sidecar_name}")
            add(
                database_dir / sidecar_name,
                Path("Databases") / sidecar_name,
                directory=False,
            )
    if "projection.sqlite" not in names:
        raise GoodnotesError(
            f"Required projection database is missing: {database_dir / 'projection.sqlite'}"
        )

    def scan(directory: Path, relative: Path) -> None:
        before = add(directory, relative, directory=True)
        try:
            children = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as exc:
            raise GoodnotesError(
                f"Could not inspect source attachment directory: {directory}"
            ) from exc
        for child in children:
            child_path = directory / child.name
            child_relative = relative / child.name
            info = _lstat(child_path)
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
        after = _lstat(directory)
        if _stat_key(before) != _stat_key(after):
            raise GoodnotesError(f"Source directory changed while it was inspected: {directory}")

    scan(attachments_dir, Path("Attachments"))
    for path, expected_key in paths_to_recheck:
        if _stat_key(_lstat(path)) != expected_key:
            raise GoodnotesError(f"Snapshot input changed while it was fingerprinted: {path}")
    entries.sort(key=lambda entry: entry[0])
    encoded = json.dumps(entries, separators=(",", ":"), ensure_ascii=True).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_captured_at(root: Path) -> str:
    marker = root / SNAPSHOT_MARKER
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GoodnotesError(f"Could not read snapshot capture time: {marker}") from exc
    value = data.get("created_at_utc") if isinstance(data, dict) else None
    if not isinstance(value, str):
        raise GoodnotesError(f"Snapshot marker has no capture time: {marker}")
    return value


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
    """Remove a CLI-owned tree without traversing symbolic links or special files."""

    if path.is_symlink():
        raise GoodnotesError(f"Refusing to remove a symlinked cache directory: {path}")
    try:
        root_info = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect cache directory: {path}") from exc
    if not stat.S_ISDIR(root_info.st_mode):
        raise GoodnotesError(f"Refusing to remove a non-directory cache path: {path}")

    _assert_safe_tree(path)

    def make_writable_and_remove(directory: Path) -> None:
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect generated snapshot: {directory}") from exc
        for child in children:
            child_path = directory / child.name
            info = _lstat(child_path)
            if stat.S_ISDIR(info.st_mode):
                child_path.chmod(0o700)
                make_writable_and_remove(child_path)
                child_path.rmdir()
            elif stat.S_ISREG(info.st_mode):
                child_path.unlink()
            else:
                raise GoodnotesError(f"Refusing to remove a special cache file: {child_path}")

    path.chmod(0o700)
    make_writable_and_remove(path)
    path.rmdir()


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
    resolved = requested.resolve(strict=False)
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
    if requested_source.is_symlink():
        raise GoodnotesError("The source Library directory must not be a symlink.")
    try:
        source = requested_source.resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(
            f"Source Library directory does not exist: {requested_source}"
        ) from exc
    _require_directory(source, purpose="Source Library path")
    state, snapshots = _prepare_state(Path(data_dir), source)

    with _file_lock(state / _SYNC_LOCK, exclusive=True):
        _validate_cache_marker(state, create=True)
        _validate_generated_snapshots(snapshots)
        with _file_lock(state / _VIEW_LOCK, exclusive=False):
            active = _load_active(state, snapshots)
        source_signature = _input_signature(source)

        # Refreshes serialize on .sync.lock. The shared view lock protects pointer resolution
        # while allowing existing readers to continue during the source capture.
        active_cache_signature = None if force else _active_cache_signature(active)

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
            create_snapshot(source, staging)
            sandbox = Sandbox.load(staging)
            if destination.exists() or destination.is_symlink():
                raise GoodnotesError(f"Refusing to overwrite a cache generation: {destination}")
            os.replace(staging, destination)
            sandbox = Sandbox.load(destination)
            cache_signature = _input_signature(sandbox.library_dir, include_root=True)
            captured_at = _snapshot_captured_at(destination)
            signature_after = _input_signature(source)
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
        )


@contextmanager
def read_view(settings: Settings) -> Iterator[Sandbox]:
    """Resolve one snapshot under a shared lock and keep it protected through the read."""

    explicit = getattr(settings, "sandbox", None)
    lock_state: Path | None = None
    if explicit is not None:
        lock_state = _managed_state_for_explicit(Path(explicit))
    else:
        configured = getattr(settings, "data_dir", None)
        state_requested = Path(configured).expanduser() if configured is not None else None
        if state_requested is None:
            # Import lazily so Settings remains a type-only dependency for this module.
            from .config import default_data_dir

            state_requested = default_data_dir().expanduser()
        if state_requested.is_symlink():
            raise GoodnotesError("The CLI cache directory must not be a symlink.")
        _require_not_production(state_requested.resolve(strict=False), purpose="CLI cache storage")
        if not state_requested.exists():
            raise GoodnotesError("No local library. Run 'goodnotes sync' first.")
        state = state_requested.resolve(strict=True)
        _require_not_production(state, purpose="CLI cache storage")
        _require_directory(state, purpose="CLI cache directory")
        lock_state = state

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
    if not lock_state.exists() or not lock_state.is_dir():
        raise GoodnotesError(f"CLI cache directory does not exist: {lock_state}")
    with _file_lock(view_lock, exclusive=False):
        if (lock_state / "active.json").is_symlink():
            raise GoodnotesError("The active library pointer must not be a symlink.")
        snapshots = lock_state / "snapshots"
        if snapshots.is_symlink():
            raise GoodnotesError("The snapshots directory must not be a symlink.")
        sandbox = settings.load_sandbox()
        if sandbox.root.parent != snapshots or (
            explicit is None and not _GENERATION.fullmatch(sandbox.root.name)
        ):
            raise GoodnotesError("The resolved cache snapshot must be a direct generated child.")
        expected = getattr(settings, "expect_snapshot", None)
        if expected is not None and sandbox.root.name != expected:
            raise StaleSnapshotError(
                f"The active snapshot is {sandbox.root.name}, but {expected} was requested."
            )
        settings.snapshot_id = sandbox.root.name
        yield sandbox
