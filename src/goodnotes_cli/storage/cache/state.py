"""Storage operations."""

from __future__ import annotations

import json
import os
import re
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from goodnotes_cli.errors import CacheAccessError, GoodnotesError
from goodnotes_cli.storage.inventory import _input_signature, _require_directory
from goodnotes_cli.storage.metadata import CACHE_METADATA_LIMIT, SNAPSHOT_MARKER_LIMIT, read_json
from goodnotes_cli.storage.paths import _require_not_production, path_contains
from goodnotes_cli.storage.sandbox import SNAPSHOT_MARKER, Sandbox

_CACHE_MARKER = ".goodnotes-cli-cache.json"


class PointerEntryError(GoodnotesError):
    """The active pointer entry itself cannot be read or validated."""


_GENERATION = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{8}$")


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
        value = read_json(marker, limit=CACHE_METADATA_LIMIT)
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
        raise PointerEntryError("The active library pointer must not be a symlink.")
    if not pointer.exists():
        return None
    try:
        pointer_info = pointer.lstat()
        if not stat.S_ISREG(pointer_info.st_mode):
            raise PointerEntryError("The active library pointer must be a regular file.")
        data = read_json(pointer, limit=CACHE_METADATA_LIMIT)
    except GoodnotesError:
        raise
    except (OSError, ValueError) as exc:
        raise PointerEntryError("The active library pointer is invalid.") from exc
    if not isinstance(data, dict):
        raise PointerEntryError("The active library pointer is invalid.")
    raw_path = _active_path(data)
    if raw_path is None:
        raise PointerEntryError("The active library pointer is invalid.")
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


def _timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _snapshot_captured_at(root: Path, marker_data: dict[str, Any] | None = None) -> str:
    marker = root / SNAPSHOT_MARKER
    if marker_data is None:
        try:
            marker_data = read_json(marker, limit=SNAPSHOT_MARKER_LIMIT)
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
            marker_data = read_json(marker, limit=SNAPSHOT_MARKER_LIMIT)
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
