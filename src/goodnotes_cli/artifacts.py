"""Bounded, private storage for regenerable CLI artifacts.

The cache keeps generated PNGs as content-addressed blobs and stores hardlinks
from artifact entries. Its byte limit counts the unique payload and entry-marker
inodes; the fixed ownership, lock, and staging-control files are excluded.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import re
import stat
import time
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .errors import GoodnotesError
from .safety import (
    _remove_entry_at,
    _require_not_production,
    path_contains,
    paths_overlap,
    stat_key,
)

_ROOT_MARKER = ".goodnotes-artifacts.json"
_LOCK_NAME = ".artifacts.lock"
_ENTRY_MARKER = ".artifact.json"
_STAGING_MARKER = ".artifact-staging.json"
_ROOT_OWNER = {"format_version": 1, "owner": "goodnotes-cli-artifacts"}
_OWNER_UID = os.getuid() if hasattr(os, "getuid") else None
_KEY = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGE_NAME = re.compile(r"^\.staging-([0-9a-f]{32})$")
_PUBLISH_NAME = re.compile(r"^\.publishing-([0-9a-f]{32})$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_COPY_CHUNK = 1024 * 1024


class ArtifactCapacityError(GoodnotesError):
    """A regenerable artifact cannot fit without evicting a protected entry."""

    code = "artifact_cache_capacity"

    def __init__(self, *, required_bytes: int, max_bytes: int, cache_dir: Path) -> None:
        super().__init__(
            "The generated artifact could not fit without removing a protected cache entry. "
            f"The projected use is {required_bytes} bytes; the cache limit is {max_bytes} bytes."
        )
        self.details = {
            "operation": "publish_artifact",
            "path": str(cache_dir),
            "required_bytes": required_bytes,
            "max_bytes": max_bytes,
            "remedy": (
                "Use --output to save a persistent result outside the bounded artifact cache, "
                "or retry after the five-minute protected period."
            ),
        }


@dataclass(frozen=True)
class CachedArtifact:
    """A validated immutable cache entry."""

    directory: Path
    result: dict[str, Any]


@dataclass(frozen=True)
class _FileRecord:
    name: str
    size: int
    kind: str
    digest: str | None = None


@dataclass(frozen=True)
class _EntryRecord:
    key: str
    directory: Path
    result: dict[str, Any]
    files: tuple[_FileRecord, ...]
    marker_size: int
    last_used: float
    directory_identity: tuple[int, int]


@dataclass(frozen=True)
class _PreparedFile:
    name: str
    path: Path
    size: int
    kind: str
    digest: str | None = None
    source_identity: tuple[int, int, int, int, int, int] | None = None


def artifact_key(value: Any) -> str:
    """Hash a JSON value using stable, compact canonical JSON encoding."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise GoodnotesError("Artifact cache keys must be JSON-serializable values.") from exc
    return hashlib.sha256(encoded).hexdigest()


class ArtifactCache:
    """Serialize cache operations and manage private volatile artifact entries.

    The persistent lock is held for the context's lifetime. Staging directories
    are private, flat, and removed on every context exit; abandoned ones are
    recovered the next time the cache is opened.
    """

    def __init__(
        self,
        root: str | Path,
        snapshot_root: str | Path,
        *,
        max_bytes: int = 100_000_000,
        ttl_seconds: float = 86_400,
        grace_seconds: float = 300,
        clock: Any = time.time,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if ttl_seconds < 0 or grace_seconds < 0:
            raise ValueError("ttl_seconds and grace_seconds must not be negative")
        self._requested_root = Path(root).expanduser()
        self._requested_snapshot_root = Path(snapshot_root).expanduser()
        self.max_bytes = int(max_bytes)
        self.ttl_seconds = float(ttl_seconds)
        self.grace_seconds = float(grace_seconds)
        self._clock = clock
        self.root = self._requested_root
        self.entries = self.root / "entries"
        self.blobs = self.root / "blobs"
        self.staging_root = self.root / ".staging"
        self._root_fd: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self._managed_directory_identities: dict[str, tuple[int, int]] = {}
        self._lock_fd: int | None = None
        self._active_stages: dict[str, tuple[int, int]] = {}
        self._pinned_keys: set[str] = set()
        self._records: dict[str, _EntryRecord] = {}
        self._verified_blob_stats: dict[str, tuple[int, int, int, int, int]] = {}
        self._blob_digests_by_stat: dict[tuple[int, int, int, int, int], str] = {}

    def __enter__(self) -> ArtifactCache:
        if self._lock_fd is not None:
            raise RuntimeError("ArtifactCache context is already open")
        self._pinned_keys.clear()
        try:
            self._prepare_root()
            self._acquire_lock()
            self._ensure_root_marker()
            self._ensure_managed_directories()
            self._maintain()
        except BaseException:
            self._release_lock()
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            self._renew_leases()
        except GoodnotesError:
            if exc_type is None:
                raise
        finally:
            self._release_lock()

    def destination(self, key: str) -> Path:
        """Return the path for a validated key, whether or not it exists yet."""

        self._require_open()
        self._validate_key(key)
        return self.entries / key

    def lookup(self, key: str) -> CachedArtifact | None:
        """Return a validated entry and extend its lease, or return ``None``."""

        self._require_open()
        self._validate_key(key)
        record = self._records.get(key)
        if record is None:
            return None
        now = self._now()
        age = now - record.last_used
        if age >= self.ttl_seconds and age >= self.grace_seconds:
            if key not in self._pinned_keys and not self._entry_is_staged(record):
                self._remove_entry(record)
                self._cleanup_orphan_blobs(self._records)
                return None
        validated = self._read_entry(
            record.directory,
            expected_key=key,
            verify_blobs=True,
            verify_files=True,
        )
        if validated.directory_identity != record.directory_identity:
            raise GoodnotesError(
                f"Artifact entry changed while it was being used: {record.directory}"
            )
        after = self._touch_entry(validated, now, operation="update artifact cache use time")
        self._records[key] = _EntryRecord(
            key,
            validated.directory,
            validated.result,
            validated.files,
            validated.marker_size,
            after.st_mtime,
            validated.directory_identity,
        )
        self._pinned_keys.add(key)
        return CachedArtifact(validated.directory, json.loads(json.dumps(validated.result)))

    @contextmanager
    def staging(self) -> Generator[Path, None, None]:
        """Yield a new private flat staging directory, removing it on exit."""

        self._require_open()
        self._prune_expired_records()
        token = uuid4().hex
        name = f".staging-{token}"
        stage = self.staging_root / name
        self._mkdir_owned(stage, purpose="artifact staging directory", exist_ok=False)
        stage_fd = self._open_directory_fd(stage)
        try:
            self._write_json_exclusive(
                stage / _STAGING_MARKER,
                self._staging_data("staging", token),
                mode=0o600,
                dir_fd=stage_fd,
            )
        finally:
            os.close(stage_fd)
        opened = self._directory(stage, purpose="artifact staging directory")
        identity = (opened.st_dev, opened.st_ino)
        self._active_stages[name] = identity
        try:
            yield stage
        finally:
            self._active_stages.pop(name, None)
            if stage.exists() or stage.is_symlink():
                current = self._directory(stage, purpose="artifact staging directory")
                if (current.st_dev, current.st_ino) != identity:
                    raise GoodnotesError(f"Artifact staging directory changed: {stage}")
                self._verify_staging_marker(stage, kind="staging", token=token)
                self._safe_remove_tree(stage)
                self._cleanup_orphan_blobs(self._records)

    def publish(self, key: str, stage: str | Path, result: dict[str, Any]) -> CachedArtifact:
        """Atomically publish a complete flat stage under a content-derived key."""

        self._require_open()
        self._validate_key(key)
        if not isinstance(result, dict):
            raise GoodnotesError("Artifact result metadata must be a JSON object.")
        serialized_result = self._json_bytes(result)
        stage_path = self._validate_active_stage(Path(stage).expanduser())
        self._prune_expired_records()
        existing = self.lookup(key)
        if existing is not None:
            return existing
        prepared = tuple(sorted(self._prepare_stage_files(stage_path), key=lambda item: item.name))
        if not prepared:
            raise GoodnotesError("An artifact cache entry must contain at least one file.")

        now = self._now()
        files_data = [
            {
                "name": item.name,
                "kind": item.kind,
                "size": item.size,
                "sha256": item.digest,
            }
            for item in prepared
        ]
        entry_data = {
            "format_version": 1,
            "key": key,
            "created_at": now,
            "result": json.loads(serialized_result),
            "files": files_data,
        }
        entry_marker_bytes = self._json_bytes(entry_data)
        if len(entry_marker_bytes) > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=len(entry_marker_bytes),
                max_bytes=self.max_bytes,
                cache_dir=self.root,
            )

        self._make_room(prepared, len(entry_marker_bytes))

        token = uuid4().hex
        publish_name = f".publishing-{token}"
        publish_dir = self.staging_root / publish_name
        self._mkdir_owned(publish_dir, purpose="artifact publish directory", exist_ok=False)
        publish_fd = self._open_directory_fd(publish_dir)
        try:
            self._write_json_exclusive(
                publish_dir / _STAGING_MARKER,
                self._staging_data("publishing", token),
                mode=0o600,
                dir_fd=publish_fd,
            )
            info = self._directory(publish_dir, purpose="artifact publish directory")
        except BaseException:
            os.close(publish_fd)
            raise
        final_dir = self.entries / key
        entry_fd: int | None = None
        try:
            entry_dir = publish_dir / "entry"
            self._mkdir_owned(entry_dir, purpose="artifact publish entry", exist_ok=False)
            entry_fd = self._open_directory_fd(entry_dir)
            for item in prepared:
                target = entry_dir / item.name
                if item.kind == "png":
                    assert item.digest is not None
                    blob = self._ensure_blob(item)
                    self._link_blob(blob, target)
                else:
                    self._copy_private_file(
                        item.path, target, expected_identity=item.source_identity
                    )
            # The entry marker is the publication record and is written last.
            self._write_json_exclusive(
                entry_dir / _ENTRY_MARKER, entry_data, mode=0o600, dir_fd=entry_fd
            )
            self._touch_marker(
                entry_fd,
                _ENTRY_MARKER,
                now,
                display_path=entry_dir / _ENTRY_MARKER,
                operation="set artifact entry use time",
            )
            self._verify_built_entry(entry_dir, key)
            entries_fd = self._open_managed_directory("entries")
            try:
                current_entry = self._stat_at(publish_fd, "entry")
                if current_entry is None or self._identity(current_entry) != self._identity(
                    os.fstat(entry_fd)
                ):
                    raise GoodnotesError(f"Artifact publish entry changed: {entry_dir}")
                if self._stat_at(entries_fd, key) is not None:
                    raise GoodnotesError(f"Artifact cache entry appeared during publication: {key}")
                os.replace("entry", key, src_dir_fd=publish_fd, dst_dir_fd=entries_fd)
            finally:
                os.close(entries_fd)
            self._verify_staging_marker(publish_dir, kind="publishing", token=token)
            self._safe_remove_tree(publish_dir)
        except BaseException:
            if publish_dir.exists() or publish_dir.is_symlink():
                current = self._directory(publish_dir, purpose="artifact publish directory")
                if (current.st_dev, current.st_ino) != (info.st_dev, info.st_ino):
                    raise GoodnotesError(
                        f"Artifact publish directory changed: {publish_dir}"
                    ) from None
                self._verify_staging_marker(publish_dir, kind="publishing", token=token)
                self._safe_remove_tree(publish_dir)
            try:
                self._cleanup_orphan_blobs(self._records)
            except GoodnotesError:
                # Preserve the publishing failure; any unsafe residue will fail
                # closed the next time the cache is opened.
                pass
            raise
        finally:
            if entry_fd is not None:
                os.close(entry_fd)
            os.close(publish_fd)

        final_record = self._read_entry(final_dir, expected_key=key)
        self._records[key] = final_record
        self._pinned_keys.add(key)
        self._cleanup_orphan_blobs(self._records)
        return CachedArtifact(final_dir, json.loads(json.dumps(final_record.result)))

    def _require_open(self) -> None:
        if self._lock_fd is None:
            raise RuntimeError("ArtifactCache must be used inside a context manager")
        self._verify_managed_directories()

    def _renew_leases(self) -> None:
        if self._lock_fd is None or not self._pinned_keys:
            return
        self._verify_managed_directories()
        now = self._now()
        for key in self._pinned_keys:
            record = self._records.get(key)
            if record is None:
                continue
            after = self._touch_entry(record, now, operation="renew artifact cache lease")
            self._records[key] = _EntryRecord(
                key,
                record.directory,
                record.result,
                record.files,
                record.marker_size,
                after.st_mtime,
                record.directory_identity,
            )

    def _touch_entry(
        self,
        record: _EntryRecord,
        now: float,
        *,
        operation: str,
    ) -> os.stat_result:
        """Touch an entry marker through its verified directory descriptor."""

        entries_fd = self._open_managed_directory("entries")
        try:
            try:
                entry_fd = os.open(
                    record.key,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=entries_fd,
                )
            except OSError as exc:
                raise GoodnotesError(
                    f"Artifact entry changed before {operation}: {record.directory}"
                ) from exc
        finally:
            os.close(entries_fd)
        try:
            entry_info = os.fstat(entry_fd)
            self._require_owner(entry_info, purpose="artifact entry")
            if (
                not stat.S_ISDIR(entry_info.st_mode)
                or self._identity(entry_info) != record.directory_identity
                or entry_info.st_mode & 0o077
            ):
                raise GoodnotesError(
                    f"Artifact entry changed before {operation}: {record.directory}"
                )
            after = self._touch_marker(
                entry_fd,
                _ENTRY_MARKER,
                now,
                display_path=record.directory / _ENTRY_MARKER,
                operation=operation,
            )
            current_entry = self._stat_at_fd_path(record.key)
            if current_entry is None or self._identity(current_entry) != record.directory_identity:
                raise GoodnotesError(
                    f"Artifact entry changed during {operation}: {record.directory}"
                )
            return after
        finally:
            os.close(entry_fd)

    def _touch_marker(
        self,
        directory_fd: int,
        name: str,
        now: float,
        *,
        display_path: Path,
        operation: str,
    ) -> os.stat_result:
        before = self._stat_at(directory_fd, name)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise GoodnotesError(f"Artifact metadata changed before {operation}: {display_path}")
        self._require_owner(before, purpose="artifact entry marker")
        if before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600:
            raise GoodnotesError(f"Artifact entry marker metadata is invalid: {display_path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            marker_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise GoodnotesError(f"Could not {operation}: {display_path}") from exc
        try:
            opened = os.fstat(marker_fd)
            if not stat.S_ISREG(opened.st_mode) or self._identity(opened) != self._identity(before):
                raise GoodnotesError(
                    f"Artifact metadata changed before {operation}: {display_path}"
                )
            self._require_owner(opened, purpose="artifact entry marker")
            try:
                os.utime(marker_fd, (now, now))
            except OSError as exc:
                raise GoodnotesError(f"Could not {operation}: {display_path}") from exc
            after = os.fstat(marker_fd)
            current = self._stat_at(directory_fd, name)
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or self._identity(after) != self._identity(opened)
                or self._identity(current) != self._identity(opened)
            ):
                raise GoodnotesError(
                    f"Artifact metadata changed during {operation}: {display_path}"
                )
            return after
        finally:
            os.close(marker_fd)

    def _stat_at_fd_path(self, key: str) -> os.stat_result | None:
        entries_fd = self._open_managed_directory("entries")
        try:
            return self._stat_at(entries_fd, key)
        finally:
            os.close(entries_fd)

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise GoodnotesError("Artifact cache clock returned an invalid time.")
        return value

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise GoodnotesError("Artifact cache keys must be 64 lowercase hexadecimal characters.")

    def _prepare_root(self) -> None:
        raw_root = self._requested_root
        requested_snapshot = self._requested_snapshot_root
        if not raw_root.is_absolute():
            raw_root = Path(os.path.abspath(raw_root))
        if not requested_snapshot.is_absolute():
            requested_snapshot = Path(os.path.abspath(requested_snapshot))
        self._reject_symlink_aliases(raw_root, purpose="artifact cache")
        self._reject_symlink_aliases(requested_snapshot, purpose="snapshot")
        try:
            snapshot_info = requested_snapshot.lstat()
        except OSError as exc:
            raise GoodnotesError(
                f"Could not inspect selected snapshot root: {requested_snapshot}"
            ) from exc
        if stat.S_ISLNK(snapshot_info.st_mode) or not stat.S_ISDIR(snapshot_info.st_mode):
            raise GoodnotesError("The selected snapshot root must be a real directory.")
        snapshot = requested_snapshot.resolve(strict=True)
        try:
            root = raw_root.resolve(strict=False)
        except OSError as exc:
            raise GoodnotesError(f"Could not resolve artifact cache root: {raw_root}") from exc
        _require_not_production(root, purpose="artifact cache storage")
        if paths_overlap(root, snapshot):
            raise GoodnotesError(
                "Artifact cache storage and the selected snapshot must not overlap."
            )
        try:
            root_fd = self._open_directory_tree(raw_root)
        except OSError as exc:
            raise GoodnotesError(f"Could not open artifact cache root safely: {raw_root}") from exc
        self._root_fd = root_fd
        try:
            info = os.fstat(root_fd)
            if not stat.S_ISDIR(info.st_mode):
                raise GoodnotesError("The artifact cache root must be a real directory.")
            self._require_owner(info, purpose="artifact cache root")
            try:
                resolved = raw_root.resolve(strict=True)
            except OSError as exc:
                raise GoodnotesError(f"Could not resolve artifact cache root: {raw_root}") from exc
            if resolved != root:
                raise GoodnotesError("Artifact cache root changed while it was being prepared.")
            self.root = resolved
            self.entries = resolved / "entries"
            self.blobs = resolved / "blobs"
            self.staging_root = resolved / ".staging"
            self._root_identity = self._identity(info)
            self._verify_root_path()
            if paths_overlap(resolved, snapshot):
                raise GoodnotesError(
                    "Artifact cache storage and the selected snapshot must not overlap."
                )
            _require_not_production(resolved, purpose="artifact cache storage")

            marker_info = self._stat_at(root_fd, _ROOT_MARKER)
            if marker_info is not None:
                if not stat.S_ISREG(marker_info.st_mode):
                    raise GoodnotesError(
                        "The artifact cache ownership marker must be a regular file."
                    )
                self._require_owner(marker_info, purpose="artifact cache ownership marker")
                if self._stat_at(root_fd, _LOCK_NAME) is None:
                    value, marker_info = self._read_json_file(
                        self.root / _ROOT_MARKER,
                        purpose="artifact cache ownership marker",
                        dir_fd=root_fd,
                    )
                    if (
                        stat.S_IMODE(marker_info.st_mode) != 0o600
                        or marker_info.st_nlink != 1
                        or value != _ROOT_OWNER
                    ):
                        raise GoodnotesError("The artifact cache ownership marker is invalid.")
            else:
                try:
                    existing = {entry.name for entry in os.scandir(root_fd)}
                except OSError as exc:
                    raise GoodnotesError(
                        f"Could not inspect artifact cache root: {self.root}"
                    ) from exc
                # Another initializer may have published the ownership marker
                # after our first absence check. Defer its contents until its
                # persistent lock is acquired.
                marker_after_scan = self._stat_at(root_fd, _ROOT_MARKER)
                if marker_after_scan is not None:
                    if not stat.S_ISREG(marker_after_scan.st_mode):
                        raise GoodnotesError(
                            "The artifact cache ownership marker must be a regular file."
                        )
                    self._require_owner(
                        marker_after_scan, purpose="artifact cache ownership marker"
                    )
                elif existing - {_LOCK_NAME}:
                    raise GoodnotesError(
                        "Refusing to adopt a non-empty unmarked artifact cache root."
                    )

        except BaseException:
            os.close(root_fd)
            self._root_fd = None
            self._root_identity = None
            raise

    @staticmethod
    def _open_directory_tree(path: Path) -> int:
        """Open/create each component without following a symlink ancestor."""

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path.anchor, flags)
        try:
            for part in path.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    os.close(child)
                    raise GoodnotesError(
                        f"Artifact cache path component is not a directory: {part}"
                    )
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _stat_at(directory_fd: int, name: str) -> os.stat_result | None:
        try:
            return os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def _verify_root_path(self) -> None:
        if self._root_fd is None or self._root_identity is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        self._reject_symlink_aliases(self.root, purpose="artifact cache")
        try:
            current = self.root.lstat()
            opened = os.fstat(self._root_fd)
        except OSError as exc:
            raise GoodnotesError("Artifact cache root changed while in use.") from exc
        if (
            not stat.S_ISDIR(current.st_mode)
            or self._identity(current) != self._root_identity
            or self._identity(opened) != self._root_identity
        ):
            raise GoodnotesError("Artifact cache root changed while in use.")

    def _ensure_root_marker(self) -> None:
        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        marker = self.root / _ROOT_MARKER
        marker_info = self._stat_at(root_fd, _ROOT_MARKER)
        if marker_info is not None and stat.S_ISLNK(marker_info.st_mode):
            raise GoodnotesError("The artifact cache ownership marker must not be a symlink.")
        if marker_info is None:
            try:
                existing = {entry.name for entry in os.scandir(root_fd)}
            except OSError as exc:
                raise GoodnotesError(f"Could not inspect artifact cache root: {self.root}") from exc
            if existing - {_LOCK_NAME}:
                raise GoodnotesError("Refusing to adopt a non-empty unmarked artifact cache root.")
            self._protect_root()
            self._write_json_exclusive(marker, _ROOT_OWNER, mode=0o600, dir_fd=root_fd)
        value, info = self._read_json_file(
            marker, purpose="artifact cache ownership marker", dir_fd=root_fd
        )
        self._require_owner(info, purpose="artifact cache ownership marker")
        if stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1 or value != _ROOT_OWNER:
            raise GoodnotesError("The artifact cache ownership marker is invalid.")
        self._protect_root()
        if self._lock_fd is None:
            raise RuntimeError("Artifact cache lock is not open")
        os.fchmod(self._lock_fd, 0o600)

    def _protect_root(self) -> None:
        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        try:
            os.fchmod(root_fd, 0o700)
        except OSError as exc:
            raise GoodnotesError(f"Could not protect artifact cache root: {self.root}") from exc
        self._verify_root_path()

    @staticmethod
    def _reject_symlink_aliases(path: Path, *, purpose: str) -> None:
        """Reject symlinks in the requested path or any parent component."""

        try:
            canonical = path.resolve(strict=False)
        except OSError as exc:
            raise GoodnotesError(f"Could not resolve {purpose} path: {path}") from exc
        if canonical != path:
            raise GoodnotesError(f"The {purpose} path or one of its parents must not be a symlink.")

    def _acquire_lock(self) -> None:
        path = self.root / _LOCK_NAME
        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        try:
            before = self._stat_at(root_fd, _LOCK_NAME)
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact cache lock: {path}") from exc
        if before is not None:
            if not stat.S_ISREG(before.st_mode):
                raise GoodnotesError("The artifact cache lock must be a regular file.")
            self._require_owner(before, purpose="artifact cache lock")
            if before.st_nlink != 1:
                raise GoodnotesError("The artifact cache lock must not be hardlinked.")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if before is None:
            try:
                descriptor = os.open(
                    _LOCK_NAME,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR | nofollow,
                    0o600,
                    dir_fd=root_fd,
                )
            except FileExistsError:
                before = self._stat_at(root_fd, _LOCK_NAME)
                if before is None or not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise GoodnotesError("The artifact cache lock is invalid.") from None
                self._require_owner(before, purpose="artifact cache lock")
                descriptor = os.open(_LOCK_NAME, os.O_RDWR | nofollow, dir_fd=root_fd)
            except OSError as exc:
                raise GoodnotesError(f"Could not create artifact cache lock: {path}") from exc
        else:
            try:
                descriptor = os.open(_LOCK_NAME, os.O_RDWR | nofollow, dir_fd=root_fd)
            except OSError as exc:
                raise GoodnotesError(f"Could not open artifact cache lock: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            current = self._stat_at(root_fd, _LOCK_NAME)
            if (
                current is None
                or not stat.S_ISREG(opened.st_mode)
                or self._identity(opened) != self._identity(current)
                or (before is not None and self._identity(before) != self._identity(opened))
                or opened.st_nlink != 1
            ):
                raise GoodnotesError("Artifact cache lock changed while it was being opened.")
            self._require_owner(opened, purpose="artifact cache lock")
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX)
            except OSError as exc:
                raise GoodnotesError(f"Could not acquire artifact cache lock: {path}") from exc
            current = self._stat_at(root_fd, _LOCK_NAME)
            if current is None or self._identity(current) != self._identity(opened):
                raise GoodnotesError("Artifact cache lock changed while it was being acquired.")
            self._verify_root_path()
            self._lock_fd = descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _release_lock(self) -> None:
        descriptor, self._lock_fd = self._lock_fd, None
        if descriptor is not None:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
        root_fd, self._root_fd = self._root_fd, None
        self._root_identity = None
        self._managed_directory_identities.clear()
        if root_fd is not None:
            os.close(root_fd)

    def _ensure_managed_directories(self) -> None:
        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        known = {_ROOT_MARKER, _LOCK_NAME, "entries", "blobs", ".staging"}
        try:
            names = {entry.name for entry in os.scandir(root_fd)}
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact cache root: {self.root}") from exc
        unknown = names - known
        if unknown:
            raise GoodnotesError(f"Unexpected item in artifact cache root: {sorted(unknown)[0]}")
        for name, purpose in (
            ("entries", "artifact entries directory"),
            ("blobs", "artifact blobs directory"),
            (".staging", "artifact staging root"),
        ):
            path = self.root / name
            before = self._stat_at(root_fd, name)
            if before is None:
                try:
                    os.mkdir(name, mode=0o700, dir_fd=root_fd)
                except OSError as exc:
                    raise GoodnotesError(f"Could not create {purpose}: {path}") from exc
                before = self._stat_at(root_fd, name)
            if before is None or not stat.S_ISDIR(before.st_mode):
                raise GoodnotesError(f"{purpose.capitalize()} must be a real directory: {path}")
            try:
                directory_fd = os.open(
                    name,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=root_fd,
                )
            except OSError as exc:
                raise GoodnotesError(f"Could not open {purpose}: {path}") from exc
            try:
                opened = os.fstat(directory_fd)
                if not stat.S_ISDIR(opened.st_mode) or self._identity(opened) != self._identity(
                    before
                ):
                    raise GoodnotesError(f"{purpose.capitalize()} changed while opening: {path}")
                self._require_owner(opened, purpose=purpose)
                os.fchmod(directory_fd, 0o700)
                if os.fstat(directory_fd).st_mode & 0o077:
                    raise GoodnotesError(f"Could not protect {purpose}: {path}")
                self._managed_directory_identities[name] = self._identity(opened)
            finally:
                os.close(directory_fd)

    def _verify_managed_directory(self, name: str) -> None:
        expected = self._managed_directory_identities.get(name)
        if expected is None:
            raise RuntimeError(f"Managed artifact directory is not initialized: {name}")
        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        current = self._stat_at(root_fd, name)
        if (
            current is None
            or not stat.S_ISDIR(current.st_mode)
            or self._identity(current) != expected
        ):
            raise GoodnotesError(f"Managed artifact directory changed: {self.root / name}")

    def _open_managed_directory(self, name: str) -> int:
        if name not in self._managed_directory_identities:
            raise RuntimeError(f"Managed artifact directory is not initialized: {name}")
        return self._open_directory_fd(self.root / name)

    def _open_directory_fd(self, path: Path) -> int:
        """Open a cache directory by walking from the pinned root descriptor."""

        self._verify_root_path()
        root_fd = self._root_fd
        if root_fd is None:
            raise RuntimeError("Artifact cache root descriptor is not open")
        try:
            relative = path.relative_to(self.root)
        except ValueError as exc:
            raise GoodnotesError(f"Artifact directory is outside the cache root: {path}") from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise GoodnotesError(f"Artifact directory path is invalid: {path}")

        descriptor = os.dup(root_fd)
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            for part in relative.parts:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except OSError as exc:
                    raise GoodnotesError(
                        f"Could not safely open artifact directory: {path}"
                    ) from exc
                info = os.fstat(child)
                if not stat.S_ISDIR(info.st_mode):
                    os.close(child)
                    raise GoodnotesError(f"Artifact path component is not a directory: {path}")
                expected = self._managed_directory_identities.get(part)
                if expected is not None and self._identity(info) != expected:
                    os.close(child)
                    raise GoodnotesError(f"Managed artifact directory changed: {self.root / part}")
                self._require_owner(info, purpose="artifact directory")
                if info.st_mode & 0o077:
                    os.close(child)
                    raise GoodnotesError(f"Artifact directory is not private: {path}")
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _verify_managed_directories(self) -> None:
        for name in ("entries", "blobs", ".staging"):
            self._verify_managed_directory(name)

    def _verify_managed_parent(self, path: Path) -> None:
        for name in ("entries", "blobs", ".staging"):
            base = self.root / name
            if path == base or path.is_relative_to(base):
                self._verify_managed_directory(name)
                return

    def _maintain(self) -> None:
        self._require_open()
        self._verified_blob_stats.clear()
        self._blob_digests_by_stat.clear()
        self._cleanup_staging()
        self._records = self._scan_entries()
        self._cleanup_orphan_blobs(self._records)
        self._prune_expired_records()
        size = self._storage_size()
        if size > self.max_bytes:
            self._make_room((), 0)
        size = self._storage_size()
        if size > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=size,
                max_bytes=self.max_bytes,
                cache_dir=self.root,
            )

    def _prune_expired_records(self) -> None:
        now = self._now()
        protected_digests = set(self._active_stage_png_references(self._blob_inventory()))
        expired = [
            record
            for record in self._records.values()
            if now - record.last_used >= self.ttl_seconds
            and now - record.last_used >= self.grace_seconds
            and record.key not in self._pinned_keys
            and not any(
                item.kind == "png" and item.digest in protected_digests for item in record.files
            )
        ]
        for record in sorted(expired, key=lambda item: item.last_used):
            self._remove_entry(record)
        if expired:
            self._cleanup_orphan_blobs(self._records)

    def _cleanup_staging(self) -> None:
        self._verify_managed_directory(".staging")
        try:
            entries = list(os.scandir(self.staging_root))
        except OSError as exc:
            raise GoodnotesError(
                f"Could not inspect artifact staging root: {self.staging_root}"
            ) from exc
        for entry in entries:
            path = self.staging_root / entry.name
            if entry.name in self._active_stages:
                continue
            stage_match = _STAGE_NAME.fullmatch(entry.name)
            publish_match = _PUBLISH_NAME.fullmatch(entry.name)
            if stage_match is None and publish_match is None:
                raise GoodnotesError(f"Unexpected item in artifact staging root: {entry.name}")
            if stage_match is not None:
                token, kind = stage_match.group(1), "staging"
            else:
                assert publish_match is not None
                token, kind = publish_match.group(1), "publishing"
            if stat.S_ISLNK(entry.stat(follow_symlinks=False).st_mode):
                raise GoodnotesError(f"Artifact staging path must not be a symlink: {path}")
            self._directory(path, purpose="artifact staging directory")
            self._verify_staging_marker(path, kind=kind, token=token)
            self._safe_remove_tree(path)

    def _scan_entries(self) -> dict[str, _EntryRecord]:
        self._verify_managed_directory("entries")
        try:
            items = list(os.scandir(self.entries))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact entries: {self.entries}") from exc
        records: dict[str, _EntryRecord] = {}
        png_references: Counter[str] = Counter()
        blob_map = self._blob_inventory()
        for item in items:
            if _KEY.fullmatch(item.name) is None:
                raise GoodnotesError(f"Unexpected item in artifact entries: {item.name}")
            path = self.entries / item.name
            if stat.S_ISLNK(item.stat(follow_symlinks=False).st_mode):
                raise GoodnotesError(f"Artifact entry must not be a symlink: {path}")
            record = self._read_entry(
                path,
                expected_key=item.name,
                blob_map=blob_map,
                verify_blobs=False,
                verify_files=False,
            )
            records[item.name] = record
            for file in record.files:
                if file.kind == "png":
                    assert file.digest is not None
                    png_references[file.digest] += 1

        stage_references = self._active_stage_png_references(blob_map)
        for digest, info in blob_map.items():
            expected_links = 1 + png_references[digest] + stage_references[digest]
            if info.st_nlink != expected_links:
                raise GoodnotesError(
                    f"Artifact blob has unexpected hardlinks: {self.blobs / (digest + '.png')}"
                )
        missing = set(png_references) - set(blob_map)
        if missing:
            raise GoodnotesError("An artifact entry refers to a missing generated PNG blob.")
        self._blob_digests_by_stat = {
            self._content_signature(info): digest for digest, info in blob_map.items()
        }
        return records

    def _active_stage_png_references(self, blob_map: Mapping[str, os.stat_result]) -> Counter[str]:
        references: Counter[str] = Counter()
        for name, identity in self._active_stages.items():
            stage = self.staging_root / name
            info = self._directory(stage, purpose="artifact staging directory")
            if self._identity(info) != identity:
                raise GoodnotesError(f"Artifact staging directory changed: {stage}")
            self._verify_staging_marker(stage, kind="staging", token=name.removeprefix(".staging-"))
            for child in os.scandir(stage):
                if child.name == _STAGING_MARKER:
                    continue
                if _FILE_NAME.fullmatch(child.name) is None:
                    raise GoodnotesError(f"Invalid staged artifact filename: {child.name}")
                path = stage / child.name
                file_info = self._regular_file(path, purpose="staged artifact file")
                self._require_owner(file_info, purpose="staged artifact file")
                if child.name.endswith(".png") and file_info.st_nlink > 1:
                    signature = self._content_signature(file_info)
                    digest = self._blob_digests_by_stat.get(signature)
                    if digest is None:
                        raise GoodnotesError(
                            "A staged PNG hardlink must refer to a verified artifact cache blob."
                        )
                    blob_info = blob_map.get(digest)
                    if blob_info is None or self._identity(file_info) != self._identity(blob_info):
                        raise GoodnotesError(
                            "A staged PNG hardlink must refer to a verified artifact cache blob."
                        )
                    self._verified_blob(digest)
                    references[digest] += 1
                elif file_info.st_nlink != 1:
                    raise GoodnotesError(f"Staged non-image files must not be hardlinked: {path}")
        return references

    def _entry_is_staged(self, record: _EntryRecord) -> bool:
        protected_digests = self._active_stage_png_references(self._blob_inventory())
        return any(item.kind == "png" and item.digest in protected_digests for item in record.files)

    def _read_entry(
        self,
        directory: Path,
        *,
        expected_key: str | None,
        blob_map: Mapping[str, os.stat_result] | None = None,
        verify_blobs: bool = False,
        verify_files: bool = False,
    ) -> _EntryRecord:
        info = self._directory(directory, purpose="artifact entry")
        self._require_owner(info, purpose="artifact entry")
        marker = directory / _ENTRY_MARKER
        data, marker_info = self._read_json_file(marker, purpose="artifact entry marker")
        self._require_owner(marker_info, purpose="artifact entry marker")
        if marker_info.st_nlink != 1 or stat.S_IMODE(marker_info.st_mode) != 0o600:
            raise GoodnotesError(f"Artifact entry marker must not be hardlinked: {marker}")
        if (
            not isinstance(data, dict)
            or set(data) != {"format_version", "key", "created_at", "result", "files"}
            or data.get("format_version") != 1
            or not isinstance(data.get("key"), str)
            or _KEY.fullmatch(data["key"]) is None
            or (expected_key is not None and data.get("key") != expected_key)
            or not isinstance(data.get("result"), dict)
            or not isinstance(data.get("files"), list)
        ):
            raise GoodnotesError(f"Artifact entry marker is invalid: {marker}")
        created_at = data.get("created_at")
        if (
            isinstance(created_at, bool)
            or not isinstance(created_at, (int, float))
            or not math.isfinite(float(created_at))
        ):
            raise GoodnotesError(f"Artifact entry creation time is invalid: {marker}")

        files: list[_FileRecord] = []
        listed: set[str] = {_ENTRY_MARKER}
        for item in data["files"]:
            if not isinstance(item, dict):
                raise GoodnotesError(f"Artifact entry file record is invalid: {marker}")
            name, kind, size = item.get("name"), item.get("kind"), item.get("size")
            if (
                not isinstance(name, str)
                or _FILE_NAME.fullmatch(name) is None
                or name == _ENTRY_MARKER
                or name in listed
                or not isinstance(kind, str)
                or kind not in {"file", "png"}
                or isinstance(size, bool)
                or not isinstance(size, int)
                or size < 0
            ):
                raise GoodnotesError(f"Artifact entry file record is invalid: {marker}")
            digest = item.get("sha256")
            if (
                set(item) != {"name", "kind", "size", "sha256"}
                or not isinstance(digest, str)
                or _DIGEST.fullmatch(digest) is None
            ):
                raise GoodnotesError(f"Artifact file digest is invalid: {marker}")
            file_path = directory / name
            file_info = self._regular_file(file_path, purpose="artifact entry file")
            self._require_owner(file_info, purpose="artifact entry file")
            if file_info.st_size != size or stat.S_IMODE(file_info.st_mode) != 0o400:
                raise GoodnotesError(f"Artifact entry file metadata is invalid: {file_path}")
            if kind == "png":
                assert isinstance(digest, str)
                blob_path = self.blobs / f"{digest}.png"
                if verify_blobs:
                    blob_info = self._verify_blob(digest, expected_size=size)
                elif blob_map is None:
                    blob_info = self._blob_info(digest, expected_size=size)
                else:
                    blob_info = blob_map.get(digest)
                    if blob_info is None or blob_info.st_size != size:
                        raise GoodnotesError(
                            f"Artifact PNG blob is missing or invalid: {blob_path}"
                        )
                if self._identity(file_info) != self._identity(blob_info):
                    raise GoodnotesError(
                        f"Artifact PNG is not linked to its content blob: {file_path}"
                    )
            else:
                if file_info.st_nlink != 1:
                    raise GoodnotesError(f"Artifact file must not be hardlinked: {file_path}")
                if verify_files:
                    actual_digest, actual_size, hashed = self._hash_file(file_path)
                    if actual_digest != digest or actual_size != size:
                        raise GoodnotesError(f"Artifact file failed its content check: {file_path}")
                    if self._identity(hashed) != self._identity(file_info):
                        raise GoodnotesError(
                            f"Artifact file changed while being checked: {file_path}"
                        )
            files.append(_FileRecord(name, size, kind, digest))
            listed.add(name)
        try:
            children = {child.name for child in os.scandir(directory)}
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact entry: {directory}") from exc
        if children != listed:
            raise GoodnotesError(f"Artifact entry contains unexpected files: {directory}")
        last_used = marker_info.st_mtime
        if not math.isfinite(last_used):
            raise GoodnotesError(f"Artifact entry use time is invalid: {marker}")
        return _EntryRecord(
            data["key"],
            directory,
            data["result"],
            tuple(files),
            marker_info.st_size,
            last_used,
            self._identity(info),
        )

    def _prepare_stage_files(
        self,
        stage: Path,
    ) -> tuple[_PreparedFile, ...]:
        self._verify_staging_marker(
            stage, kind="staging", token=stage.name.removeprefix(".staging-")
        )
        prepared: list[_PreparedFile] = []
        try:
            children = list(os.scandir(stage))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect artifact staging directory: {stage}") from exc
        for child in children:
            if child.name == _STAGING_MARKER:
                continue
            if _FILE_NAME.fullmatch(child.name) is None:
                raise GoodnotesError(f"Invalid staged artifact filename: {child.name}")
            path = stage / child.name
            info = self._regular_file(path, purpose="staged artifact file")
            self._require_owner(info, purpose="staged artifact file")
            if info.st_size > self.max_bytes:
                raise ArtifactCapacityError(
                    required_bytes=info.st_size,
                    max_bytes=self.max_bytes,
                    cache_dir=self.root,
                )
            if child.name.endswith(".png"):
                signature = self._content_signature(info)
                if info.st_nlink > 1:
                    digest = self._blob_digests_by_stat.get(signature)
                    if digest is None:
                        raise GoodnotesError(
                            "A staged PNG hardlink must refer to an artifact cache blob."
                        )
                    after = self._verified_blob(digest, expected_size=info.st_size)
                    size = after.st_size
                else:
                    digest, size, after = self._hash_file(path)
                if size != info.st_size:
                    raise GoodnotesError(f"Staged artifact changed while hashing: {path}")
                prepared.append(
                    _PreparedFile(
                        child.name,
                        path,
                        size,
                        "png",
                        digest,
                        stat_key(after),
                    )
                )
            else:
                if info.st_nlink != 1:
                    raise GoodnotesError(f"Staged files must not be hardlinked: {path}")
                digest, size, after = self._hash_file(path)
                prepared.append(
                    _PreparedFile(child.name, path, size, "file", digest, stat_key(after))
                )
        return tuple(prepared)

    def _blob_inventory(self) -> dict[str, os.stat_result]:
        """Read safe blob metadata without hashing content that no caller uses."""

        directory_fd = self._open_managed_directory("blobs")
        try:
            items = list(os.scandir(directory_fd))
        except OSError as exc:
            os.close(directory_fd)
            raise GoodnotesError(f"Could not inspect artifact blobs: {self.blobs}") from exc
        try:
            result: dict[str, os.stat_result] = {}
            for item in items:
                match = re.fullmatch(r"([0-9a-f]{64})\.png", item.name)
                if match is None:
                    raise GoodnotesError(f"Unexpected item in artifact blobs: {item.name}")
                path = self.blobs / item.name
                if stat.S_ISLNK(item.stat(follow_symlinks=False).st_mode):
                    raise GoodnotesError(f"Artifact blob must not be a symlink: {path}")
                digest = match.group(1)
                result[digest] = self._blob_info(digest, dir_fd=directory_fd)
            return result
        finally:
            os.close(directory_fd)

    def _make_room(
        self,
        prepared: tuple[_PreparedFile, ...],
        marker_size: int,
    ) -> None:
        required = self._projected_size(prepared, marker_size)
        if required <= self.max_bytes:
            return
        standalone = self._projected_size(prepared, marker_size, records={})
        if standalone > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=standalone,
                max_bytes=self.max_bytes,
                cache_dir=self.root,
            )
        protected_digests = set(self._active_stage_png_references(self._blob_inventory()))
        candidates = sorted(
            (
                record
                for record in self._records.values()
                if self._past_grace(record.last_used)
                and record.key not in self._pinned_keys
                and not any(
                    file.kind == "png" and file.digest in protected_digests for file in record.files
                )
            ),
            key=lambda record: record.last_used,
        )
        for record in candidates:
            self._remove_entry(record)
            self._cleanup_orphan_blobs(self._records)
            required = self._projected_size(prepared, marker_size)
            if required <= self.max_bytes:
                return
        raise ArtifactCapacityError(
            required_bytes=required,
            max_bytes=self.max_bytes,
            cache_dir=self.root,
        )

    def _projected_size(
        self,
        prepared: tuple[_PreparedFile, ...],
        marker_size: int,
        *,
        records: Mapping[str, _EntryRecord] | None = None,
        new_marker: bool = True,
    ) -> int:
        metadata_size = marker_size if new_marker else 0
        payload_size = 0
        png_digests: set[str] = set()
        blob_sizes: dict[str, int] = {}
        for record in (self._records if records is None else records).values():
            metadata_size += record.marker_size
            for item in record.files:
                if item.kind == "png":
                    assert item.digest is not None
                    png_digests.add(item.digest)
                    blob_sizes[item.digest] = item.size
                else:
                    payload_size += item.size
        new_png_sizes: dict[str, int] = {}
        for item in prepared:
            if item.kind == "png":
                assert item.digest is not None
                png_digests.add(item.digest)
                new_png_sizes[item.digest] = item.size
            else:
                payload_size += item.size
        for digest in png_digests:
            if digest in blob_sizes:
                continue
            if digest not in new_png_sizes:
                # A PNG used by a remaining entry must already have a canonical blob.
                raise GoodnotesError("An artifact entry refers to an unavailable PNG blob.")
            blob_sizes[digest] = new_png_sizes[digest]
        return metadata_size + payload_size + sum(blob_sizes.values())

    def _storage_size(self) -> int:
        return self._projected_size((), 0, new_marker=False)

    def _safe_remove_tree(self, path: Path) -> None:
        parent_fd = self._open_directory_fd(path.parent)
        try:
            _remove_entry_at(parent_fd, path.name, path)
        finally:
            os.close(parent_fd)

    def _remove_entry(self, record: _EntryRecord) -> None:
        self._verify_root_path()
        current = self._directory(record.directory, purpose="artifact entry")
        if self._identity(current) != record.directory_identity:
            raise GoodnotesError(f"Artifact entry changed before removal: {record.directory}")
        self._read_entry(
            record.directory,
            expected_key=record.key,
            verify_blobs=False,
            verify_files=False,
        )
        self._safe_remove_tree(record.directory)
        self._records.pop(record.key, None)

    def _cleanup_orphan_blobs(self, records: Mapping[str, _EntryRecord]) -> None:
        references = Counter(
            file.digest
            for record in records.values()
            for file in record.files
            if file.kind == "png" and file.digest is not None
        )
        blob_map = self._blob_inventory()
        references.update(self._active_stage_png_references(blob_map))
        directory_fd = self._open_managed_directory("blobs")
        try:
            for item in os.scandir(directory_fd):
                match = re.fullmatch(r"([0-9a-f]{64})\.png", item.name)
                if match is None:
                    raise GoodnotesError(f"Unexpected item in artifact blobs: {item.name}")
                digest = match.group(1)
                path = self.blobs / item.name
                info = blob_map[digest]
                if references[digest]:
                    continue
                if info.st_nlink != 1:
                    raise GoodnotesError(
                        f"Unreferenced artifact blob has unexpected hardlinks: {path}"
                    )
                try:
                    os.unlink(item.name, dir_fd=directory_fd)
                except OSError as exc:
                    raise GoodnotesError(f"Could not remove unused artifact blob: {path}") from exc
                self._verified_blob_stats.pop(digest, None)
                self._blob_digests_by_stat.pop(self._content_signature(info), None)
        finally:
            os.close(directory_fd)

    def _past_grace(self, last_used: float) -> bool:
        return self._now() - last_used >= self.grace_seconds

    def _verify_blob(self, digest: str, *, expected_size: int | None = None) -> os.stat_result:
        if _DIGEST.fullmatch(digest) is None:
            raise GoodnotesError("Artifact blob digest is invalid.")
        path = self.blobs / f"{digest}.png"
        directory_fd = self._open_managed_directory("blobs")
        try:
            info = self._blob_info(digest, expected_size=expected_size, dir_fd=directory_fd)
            actual_digest, size, after = self._hash_file(path, dir_fd=directory_fd)
            if actual_digest != digest or size != info.st_size or stat_key(after) != stat_key(info):
                raise GoodnotesError(f"Artifact PNG blob failed its content check: {path}")
            self._verified_blob_stats[digest] = self._content_signature(after)
            self._blob_digests_by_stat[self._content_signature(after)] = digest
            return after
        finally:
            os.close(directory_fd)

    def _verified_blob(self, digest: str, *, expected_size: int | None = None) -> os.stat_result:
        info = self._blob_info(digest, expected_size=expected_size)
        if self._verified_blob_stats.get(digest) != self._content_signature(info):
            return self._verify_blob(digest, expected_size=expected_size)
        return info

    def _blob_info(
        self,
        digest: str,
        *,
        expected_size: int | None = None,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        if _DIGEST.fullmatch(digest) is None:
            raise GoodnotesError("Artifact blob digest is invalid.")
        path = self.blobs / f"{digest}.png"
        owned_fd = None
        if dir_fd is None:
            owned_fd = self._open_managed_directory("blobs")
            dir_fd = owned_fd
        try:
            info = self._stat_at(dir_fd, path.name)
            if info is None or not stat.S_ISREG(info.st_mode):
                raise GoodnotesError(
                    f"Artifact PNG blob must be a regular, non-symlink file: {path}"
                )
        finally:
            if owned_fd is not None:
                os.close(owned_fd)
        self._require_owner(info, purpose="artifact PNG blob")
        if stat.S_IMODE(info.st_mode) != 0o400 or (
            expected_size is not None and info.st_size != expected_size
        ):
            raise GoodnotesError(f"Artifact PNG blob metadata is invalid: {path}")
        self._blob_digests_by_stat[self._content_signature(info)] = digest
        return info

    def _ensure_blob(self, item: _PreparedFile) -> Path:
        assert item.digest is not None
        target = self.blobs / f"{item.digest}.png"
        directory_fd = self._open_managed_directory("blobs")
        try:
            if self._stat_at(directory_fd, target.name) is not None:
                self._verified_blob(item.digest, expected_size=item.size)
                return target

            # Copy into a private staging file, then atomically create the
            # canonical name relative to the verified blobs directory.
            temp = item.path.parent / f".blob-{uuid4().hex}.tmp"
            self._copy_private_file(item.path, temp, expected_identity=item.source_identity)
            temp_parent_fd = self._open_directory_fd(temp.parent)
            try:
                temp_fd = os.open(
                    temp.name,
                    os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                    dir_fd=temp_parent_fd,
                )
                try:
                    if not stat.S_ISREG(os.fstat(temp_fd).st_mode):
                        raise GoodnotesError(f"Temporary artifact blob is unsafe: {temp}")
                    os.fchmod(temp_fd, 0o400)
                finally:
                    os.close(temp_fd)
                os.link(
                    temp.name,
                    target.name,
                    src_dir_fd=temp_parent_fd,
                    dst_dir_fd=directory_fd,
                    follow_symlinks=False,
                )
            except FileExistsError:
                self._verified_blob(item.digest, expected_size=item.size)
            except OSError as exc:
                raise GoodnotesError(f"Could not publish artifact PNG blob: {target}") from exc
            finally:
                try:
                    os.unlink(temp.name, dir_fd=temp_parent_fd)
                except FileNotFoundError:
                    pass
                except OSError as exc:
                    raise GoodnotesError(
                        f"Could not remove temporary artifact blob: {temp}"
                    ) from exc
                os.close(temp_parent_fd)
        finally:
            os.close(directory_fd)
        blob_info = self._blob_info(item.digest, expected_size=item.size)
        self._verified_blob_stats[item.digest] = self._content_signature(blob_info)
        self._blob_digests_by_stat[self._content_signature(blob_info)] = item.digest
        return target

    def _link_blob(self, source: Path, destination: Path) -> None:
        source_fd = self._open_directory_fd(source.parent)
        try:
            destination_fd = self._open_directory_fd(destination.parent)
        except BaseException:
            os.close(source_fd)
            raise
        try:
            source_info = self._stat_at(source_fd, source.name)
            if source_info is None or not stat.S_ISREG(source_info.st_mode):
                raise GoodnotesError(f"Generated artifact image is unsafe: {source}")
            os.link(
                source.name,
                destination.name,
                src_dir_fd=source_fd,
                dst_dir_fd=destination_fd,
                follow_symlinks=False,
            )
        except OSError as exc:
            raise GoodnotesError(f"Could not link generated artifact image: {destination}") from exc
        finally:
            os.close(source_fd)
            os.close(destination_fd)

    def _copy_private_file(
        self,
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int, int, int, int, int] | None,
    ) -> None:
        source_parent_fd = self._open_directory_fd(source.parent)
        try:
            destination_parent_fd = self._open_directory_fd(destination.parent)
        except BaseException:
            os.close(source_parent_fd)
            raise
        before = self._stat_at(source_parent_fd, source.name)
        if before is None or not stat.S_ISREG(before.st_mode):
            os.close(source_parent_fd)
            os.close(destination_parent_fd)
            raise GoodnotesError(f"Artifact staging file must be regular: {source}")
        if expected_identity is not None and stat_key(before) != expected_identity:
            os.close(source_parent_fd)
            os.close(destination_parent_fd)
            raise GoodnotesError(f"Artifact staging file changed before copy: {source}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            source_fd = os.open(source.name, flags, dir_fd=source_parent_fd)
        except OSError as exc:
            os.close(source_parent_fd)
            os.close(destination_parent_fd)
            raise GoodnotesError(f"Could not open artifact staging file: {source}") from exc
        target_fd: int | None = None
        created_target = False
        try:
            opened = os.fstat(source_fd)
            if not stat.S_ISREG(opened.st_mode) or stat_key(opened) != stat_key(before):
                raise GoodnotesError(f"Artifact staging file changed while opening: {source}")
            target_fd = os.open(
                destination.name,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=destination_parent_fd,
            )
            created_target = True
            total = 0
            while True:
                chunk = os.read(source_fd, _COPY_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(target_fd, view)
                    view = view[written:]
            after = os.fstat(source_fd)
            current = self._stat_at(source_parent_fd, source.name)
            if (
                stat_key(after) != stat_key(opened)
                or current is None
                or not stat.S_ISREG(current.st_mode)
                or stat_key(current) != stat_key(opened)
                or total != opened.st_size
            ):
                raise GoodnotesError(f"Artifact staging file changed during copy: {source}")
            os.fchmod(target_fd, 0o400)
            os.fsync(target_fd)
        except BaseException:
            if target_fd is not None:
                os.close(target_fd)
                target_fd = None
            if created_target:
                try:
                    os.unlink(destination.name, dir_fd=destination_parent_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            if target_fd is not None:
                os.close(target_fd)
            os.close(source_fd)
            os.close(source_parent_fd)
            os.close(destination_parent_fd)

    def _hash_file(
        self, path: Path, *, dir_fd: int | None = None
    ) -> tuple[str, int, os.stat_result]:
        if dir_fd is None:
            self._verify_managed_parent(path)
            before = self._regular_file(path, purpose="artifact file")
        else:
            before = self._stat_at(dir_fd, path.name)
            if before is None or not stat.S_ISREG(before.st_mode):
                raise GoodnotesError(f"Artifact file must be a regular, non-symlink file: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = (
                os.open(path, flags) if dir_fd is None else os.open(path.name, flags, dir_fd=dir_fd)
            )
        except OSError as exc:
            raise GoodnotesError(f"Could not read artifact file: {path}") from exc
        digest = hashlib.sha256()
        total = 0
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or stat_key(opened) != stat_key(before):
                raise GoodnotesError(f"Artifact file changed while opening: {path}")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                for chunk in iter(lambda: stream.read(_COPY_CHUNK), b""):
                    total += len(chunk)
                    digest.update(chunk)
            after = os.fstat(descriptor)
            if dir_fd is None:
                current = self._regular_file(path, purpose="artifact file")
            else:
                current = self._stat_at(dir_fd, path.name)
                if current is None or not stat.S_ISREG(current.st_mode):
                    raise GoodnotesError(f"Artifact file changed while hashing: {path}")
            if (
                stat_key(after) != stat_key(opened)
                or stat_key(current) != stat_key(opened)
                or total != opened.st_size
            ):
                raise GoodnotesError(f"Artifact file changed while hashing: {path}")
            return digest.hexdigest(), total, after
        finally:
            os.close(descriptor)

    def _verify_built_entry(self, directory: Path, key: str) -> None:
        self._read_entry(directory, expected_key=key)

    def _validate_active_stage(self, stage: Path) -> Path:
        if stage.is_symlink():
            raise GoodnotesError("Artifact stage must not be a symlink.")
        try:
            resolved = stage.resolve(strict=True)
        except OSError as exc:
            raise GoodnotesError(f"Artifact stage does not exist: {stage}") from exc
        if not path_contains(self.staging_root, resolved) or resolved.parent != self.staging_root:
            raise GoodnotesError(
                "Artifact stage must be a direct child of this cache's staging root."
            )
        match = _STAGE_NAME.fullmatch(resolved.name)
        if match is None or self._active_stages.get(resolved.name) is None:
            raise GoodnotesError("Artifact stage must be active in this ArtifactCache context.")
        info = self._directory(resolved, purpose="artifact staging directory")
        if self._identity(info) != self._active_stages[resolved.name]:
            raise GoodnotesError("Artifact staging directory changed while it was active.")
        self._verify_staging_marker(resolved, kind="staging", token=match.group(1))
        return resolved

    def _verify_staging_marker(self, directory: Path, *, kind: str, token: str) -> None:
        marker = directory / _STAGING_MARKER
        data, info = self._read_json_file(marker, purpose="artifact staging marker")
        self._require_owner(info, purpose="artifact staging marker")
        if info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600:
            raise GoodnotesError(f"Artifact staging marker must not be hardlinked: {marker}")
        expected = self._staging_data(kind, token)
        if data != expected:
            raise GoodnotesError(f"Artifact staging marker is invalid: {marker}")

    def _staging_data(self, kind: str, token: str) -> dict[str, Any]:
        return {
            "format_version": 1,
            "owner": "goodnotes-cli-artifacts",
            "kind": kind,
            "token": token,
        }

    def _read_json_file(
        self,
        path: Path,
        *,
        purpose: str,
        dir_fd: int | None = None,
    ) -> tuple[Any, os.stat_result]:
        if dir_fd is None:
            info = self._regular_file(path, purpose=purpose)
        else:
            try:
                info = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
            except OSError as exc:
                raise GoodnotesError(f"Could not inspect {purpose}: {path}") from exc
            if not stat.S_ISREG(info.st_mode):
                raise GoodnotesError(
                    f"{purpose.capitalize()} must be a regular, non-symlink file: {path}"
                )
        max_metadata_bytes = max(self.max_bytes, 1_000_000)
        if info.st_size > max_metadata_bytes:
            raise GoodnotesError(f"{purpose.capitalize()} is too large: {path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            descriptor = (
                os.open(path, flags) if dir_fd is None else os.open(path.name, flags, dir_fd=dir_fd)
            )
        except OSError as exc:
            raise GoodnotesError(f"Could not read {purpose}: {path}") from exc
        try:
            opened = os.fstat(descriptor)
            if self._identity(info) != self._identity(opened):
                raise GoodnotesError(f"{purpose.capitalize()} changed while opening: {path}")
            data = bytearray()
            while True:
                chunk = os.read(descriptor, min(_COPY_CHUNK, max_metadata_bytes + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > max_metadata_bytes:
                    raise GoodnotesError(f"{purpose.capitalize()} is too large: {path}")
            after = os.fstat(descriptor)
            if dir_fd is None:
                current = self._regular_file(path, purpose=purpose)
            else:
                current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise GoodnotesError(f"{purpose.capitalize()} changed while being read: {path}")
            if self._identity(after) != self._identity(opened) or self._identity(
                current
            ) != self._identity(opened):
                raise GoodnotesError(f"{purpose.capitalize()} changed while being read: {path}")
            try:
                return json.loads(data.decode("utf-8")), after
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise GoodnotesError(f"{purpose.capitalize()} is invalid: {path}") from exc
        finally:
            os.close(descriptor)

    def _write_json_exclusive(
        self,
        path: Path,
        value: Any,
        *,
        mode: int,
        dir_fd: int | None = None,
    ) -> bytes:
        if dir_fd is None:
            self._verify_managed_parent(path)
        else:
            self._verify_root_path()
        encoded = self._json_bytes(value)
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = (
                os.open(path, flags, mode)
                if dir_fd is None
                else os.open(path.name, flags, mode, dir_fd=dir_fd)
            )
        except OSError as exc:
            raise GoodnotesError(f"Could not create artifact metadata file: {path}") from exc
        try:
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except BaseException:
            os.close(descriptor)
            try:
                if dir_fd is None:
                    path.unlink()
                else:
                    os.unlink(path.name, dir_fd=dir_fd)
            except OSError:
                pass
            raise
        os.close(descriptor)
        return encoded

    @staticmethod
    def _json_bytes(value: Any) -> bytes:
        try:
            return json.dumps(
                value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError) as exc:
            raise GoodnotesError(
                "Artifact metadata must contain JSON-serializable values."
            ) from exc

    def _regular_file(self, path: Path, *, purpose: str) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect {purpose}: {path}") from exc
        if not stat.S_ISREG(info.st_mode):
            raise GoodnotesError(
                f"{purpose.capitalize()} must be a regular, non-symlink file: {path}"
            )
        return info

    def _directory(self, path: Path, *, purpose: str, private: bool = True) -> os.stat_result:
        try:
            info = path.lstat()
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect {purpose}: {path}") from exc
        if not stat.S_ISDIR(info.st_mode):
            raise GoodnotesError(f"{purpose.capitalize()} must be a real directory: {path}")
        self._require_owner(info, purpose=purpose)
        if private and info.st_mode & 0o077:
            raise GoodnotesError(
                f"{purpose.capitalize()} must be private to the current user: {path}"
            )
        return info

    @staticmethod
    def _require_owner(info: os.stat_result, *, purpose: str) -> None:
        if _OWNER_UID is not None and info.st_uid != _OWNER_UID:
            raise GoodnotesError(f"{purpose.capitalize()} is not owned by the current user.")

    @staticmethod
    def _identity(info: os.stat_result) -> tuple[int, int]:
        return stat_key(info)[:2]

    @staticmethod
    def _content_signature(info: os.stat_result) -> tuple[int, int, int, int, int]:
        # Creating or removing a hardlink changes ctime but does not change content.
        return stat_key(info)[:5]

    def _mkdir_owned(self, path: Path, *, purpose: str, exist_ok: bool) -> None:
        parent_fd = self._open_directory_fd(path.parent)
        child_fd: int | None = None
        try:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
            except FileExistsError:
                if not exist_ok:
                    raise
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            child_fd = os.open(path.name, flags, dir_fd=parent_fd)
        except OSError as exc:
            os.close(parent_fd)
            raise GoodnotesError(f"Could not create {purpose}: {path}") from exc
        try:
            info = os.fstat(child_fd)
            current = self._stat_at(parent_fd, path.name)
            if (
                not stat.S_ISDIR(info.st_mode)
                or current is None
                or self._identity(current) != self._identity(info)
            ):
                raise GoodnotesError(f"{purpose.capitalize()} changed while being created: {path}")
            self._require_owner(info, purpose=purpose)
            if info.st_mode & 0o077:
                raise GoodnotesError(
                    f"{purpose.capitalize()} must be private to the current user: {path}"
                )
            os.fchmod(child_fd, 0o700)
            # Reopen the path from the root after creation. If an ancestor was
            # replaced during mkdir, fail without following the replacement.
            visible_parent = self._open_directory_fd(path.parent)
            try:
                if self._identity(os.fstat(visible_parent)) != self._identity(os.fstat(parent_fd)):
                    raise GoodnotesError(f"{purpose.capitalize()} parent changed: {path.parent}")
            finally:
                os.close(visible_parent)
        finally:
            if child_fd is not None:
                os.close(child_fd)
            os.close(parent_fd)
