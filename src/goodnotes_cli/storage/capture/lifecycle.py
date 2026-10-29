"""Own the snapshot capture transaction."""

from __future__ import annotations

import json
import os
import plistlib
import stat
import time
from collections.abc import Generator, Iterable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ... import build_info
from ...errors import FilesystemAccessError, GoodnotesError, _permission_denied
from .. import metadata
from ..paths import _is_production_goodnotes_path
from ..paths import (
    paths_overlap as _paths_overlap,
)
from ..sandbox import SNAPSHOT_MARKER
from .copy import _copy_snapshot_tree, _verify_copy
from .inventory import _collect_manifest, _file_entry, _plan_capture, _sqlite_family, _stat_key
from .sqlite import (
    _PROJECTION_VALIDATION_ID,
    _consolidate_copied_databases,
    _read_projection_library_format,
    _validate_projection_database,
)
from .staging import _CaptureStaging
from .types import _DATABASE_NAMES, _Entry, _SourceChanged

_GOODNOTES_BUNDLE_IDENTIFIER = "com.goodnotesapp.x"


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
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
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

    from ...config import default_library

    try:
        default_source = default_library().resolve(strict=False)
        resolved_source = source.resolve(strict=False)
    except (OSError, RuntimeError):
        return None
    if resolved_source != default_source:
        return None
    return _read_installed_goodnotes_info()


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
    staging: _CaptureStaging,
    entries: Iterable[_Entry],
    *,
    source: Path | None = None,
    source_entries: Iterable[_Entry] = (),
    capture_stats: dict[str, Any] | None = None,
    projection_library_format: dict[str, int] | None = None,
) -> None:
    staging.validate()
    destination = staging.path
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
    with staging.marker_file() as stream:
        encoder = json.JSONEncoder(separators=(",", ":"), sort_keys=True)
        size = 0
        for chunk in encoder.iterencode(marker_data):
            encoded = chunk.encode("utf-8")
            size += len(encoded)
            if size > metadata.SNAPSHOT_MARKER_LIMIT:
                raise GoodnotesError(
                    "Snapshot inventory exceeds the supported snapshot marker size "
                    f"({metadata.SNAPSHOT_MARKER_LIMIT} bytes)."
                )
            stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


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

    with _capture_snapshot(
        source_library, destination, max_attempts=max_attempts, previous_snapshot=previous_snapshot
    ) as staging:
        staging.release()
        return staging.path


@contextmanager
def _capture_snapshot(
    source_library: str | Path,
    destination: str | Path,
    *,
    max_attempts: int = 3,
    previous_snapshot: Path | None = None,
) -> Generator[_CaptureStaging, None, None]:
    """Retain created-tree ownership until the caller confirms publication."""

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

    with _CaptureStaging(target) as staging:
        started = time.monotonic()
        for attempt in range(max_attempts):
            try:
                manifest_before = _collect_manifest(source, hash_contents=False)
                planned, source_files = _plan_capture(source, manifest_before, previous_snapshot)
                library_destination = staging.create_library()
                copy_metrics = _copy_snapshot_tree(source, library_destination, planned)
                staging.validate()
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
                    staging,
                    final_manifest,
                    source=source,
                    source_entries=source_files,
                    capture_stats=capture_stats,
                    projection_library_format=projection_library_format,
                )
                staging.validate()
                _make_read_only(target, final_manifest)
                staging.validate()
                break
            except _SourceChanged as exc:
                try:
                    staging.remove_library()
                except (GoodnotesError, OSError):
                    raise GoodnotesError(
                        "Source changed during snapshot capture and staging could not be "
                        "safely reset."
                    ) from exc
                if attempt + 1 >= max_attempts:
                    raise GoodnotesError(
                        f"Source kept changing after {max_attempts} snapshot attempts."
                    ) from exc
        yield staging
