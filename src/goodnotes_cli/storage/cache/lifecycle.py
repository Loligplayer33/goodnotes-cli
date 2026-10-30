"""Storage operations."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from goodnotes_cli.errors import (
    CacheAccessError,
    FilesystemAccessError,
    GoodnotesError,
    StaleSnapshotError,
    _os_cause,
    _permission_denied,
)
from goodnotes_cli.storage.capture.lifecycle import _capture_snapshot
from goodnotes_cli.storage.capture.types import _InventoryChanged
from goodnotes_cli.storage.paths import _require_not_production, paths_overlap
from goodnotes_cli.storage.sandbox import Sandbox

if TYPE_CHECKING:
    from goodnotes_cli.config import Settings

from goodnotes_cli.storage.cache.maintenance import (
    _clean_abandoned_staging,
    _prune_snapshots,
    _validate_generated_snapshots,
)
from goodnotes_cli.storage.cache.state import (
    _GENERATION,
    _active_cache_signature,
    _load_active,
    _managed_state_for_explicit,
    _read_cache_state,
    _read_pointer,
    _snapshot_capture_stats,
    _snapshot_captured_at,
    _timestamp,
    _validate_cache_marker,
    _write_json_atomic,
)
from goodnotes_cli.storage.inventory import (
    _input_signature,
    _require_directory,
    _source_input_signature,
)
from goodnotes_cli.storage.locks import _SYNC_LOCK, _VIEW_LOCK, _file_lock


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

        return _capture_and_publish(
            source, state, snapshots, force=force, active=active, source_signature=source_signature
        )


def _capture_and_publish(
    source: Path,
    state: Path,
    snapshots: Path,
    *,
    force: bool,
    active: tuple[dict[str, Any], Path, Sandbox | None] | None,
    source_signature: str,
) -> dict[str, object]:
    """Capture one generation under the caller's sync lock and retain it through publication."""

    checked_at = _timestamp()
    captured_at = _timestamp()
    generation = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid4().hex[:8]
    staging_path = snapshots / f".staging-{uuid4().hex}"
    destination = snapshots / generation
    if destination.exists() or destination.is_symlink():
        raise GoodnotesError(f"Refusing to overwrite a cache generation: {destination}")
    previous_snapshot = (
        active[1]
        if not force
        and active is not None
        and active[2] is not None
        and active[0].get("source_path") == str(source)
        else None
    )
    with _capture_snapshot(source, staging_path, previous_snapshot=previous_snapshot) as staging:
        published = False
        try:
            Sandbox.load(staging.path)
            staging.rename(destination)
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
                # The pointer check and owner release share this publication lifetime.
                previous = _read_pointer(state, snapshots, allow_missing_snapshot=True)
                previous_name = previous[1].name if previous is not None else None
                cleanup_warnings = []
                try:
                    _clean_abandoned_staging(snapshots)
                except (GoodnotesError, OSError) as exc:
                    cleanup_warnings.append(f"Could not clean abandoned staging: {exc}")
                _write_json_atomic(state, "active.json", pointer_data)
                published = True
                staging.release()
                cleanup_warnings.extend(
                    _prune_snapshots(snapshots, destination.name, previous_name)
                )
        except BaseException:
            if not published:
                try:
                    pointer = _read_pointer(state, snapshots)
                except BaseException:
                    # Publication is uncertain. Preserve the possibly active generation.
                    staging.release()
                else:
                    if pointer is not None and pointer[1] == destination:
                        staging.release()
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
            from goodnotes_cli.config import default_data_dir

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
