from __future__ import annotations

import json
import math
import os
import stat
import time
from collections import Counter
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.paths import (
    path_contains,
)

from .blobs import ArtifactBlobs
from .filesystem import ArtifactFilesystem, _owned_fd
from .io import ArtifactIO
from .metadata import ArtifactMetadata
from .records import (
    _ENTRY_MARKER,
    _KEY,
    _PUBLISH_NAME,
    _STAGE_NAME,
    _STAGING_MARKER,
    ArtifactCapacityError,
    CachedArtifact,
    _EntryRecord,
    _PreparedFile,
)
from .retention import ArtifactRetention


class ArtifactCache:
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
        self.ttl_seconds = float(ttl_seconds)
        self.grace_seconds = float(grace_seconds)
        self._clock = clock
        self._active_stages: dict[str, tuple[int, int]] = {}
        self._pinned_keys: set[str] = set()
        self._records: dict[str, _EntryRecord] = {}
        self._fs = ArtifactFilesystem(Path(root).expanduser(), Path(snapshot_root).expanduser())
        self._io = ArtifactIO(self._fs, int(max_bytes))
        self._fs._io = self._io
        self._blobs = ArtifactBlobs(self._fs, self._io)
        self._metadata = ArtifactMetadata(self._fs, self._io, self._blobs)
        self._retention = ArtifactRetention(
            self._fs,
            self._io,
            self._blobs,
            self._metadata,
            self._records,
            self._active_stages,
        )

    def __enter__(self) -> ArtifactCache:
        if self._fs._lock_fd is not None:
            raise RuntimeError("ArtifactCache context is already open")
        self._pinned_keys.clear()
        try:
            self._fs._prepare_root()
            self._fs._acquire_lock()
            self._fs._ensure_root_marker()
            self._fs._ensure_managed_directories()
            self._maintain()
        except BaseException as primary:
            self._fs._release_lock(primary_error=primary)
            raise
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        primary_error = exc if isinstance(exc, BaseException) else None
        try:
            try:
                self.renew_leases()
            except GoodnotesError:
                if exc_type is None:
                    raise
        except BaseException as renewal_error:
            primary_error = renewal_error
            raise
        finally:
            self._fs._release_lock(primary_error=primary_error)

    def destination(self, key: str) -> Path:
        """Return the path for a validated key, whether or not it exists yet."""

        self._require_open()
        self._validate_key(key)
        return self._fs.entries / key

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
            if key not in self._pinned_keys and not self._retention._entry_is_staged(record):
                self._remove_entry(record)
                self._cleanup_orphan_blobs(self._records)
                return None
        validated = self._metadata._read_entry(
            record.directory,
            expected_key=key,
            verify_blobs=True,
            verify_files=True,
        )
        if validated.directory_identity != record.directory_identity:
            raise GoodnotesError(
                f"Artifact entry changed while it was being used: {record.directory}"
            )
        after = self._metadata._touch_entry(
            validated, now, operation="update artifact cache use time"
        )
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
        stage = self._fs.staging_root / name
        stage_fd, identity = self._initialize_staging_directory(stage, "staging", token)
        os.close(stage_fd)
        self._active_stages[name] = identity
        try:
            yield stage
        finally:
            self._active_stages.pop(name, None)
            if stage.exists() or stage.is_symlink():
                current = self._fs._directory(stage, purpose="artifact staging directory")
                if (current.st_dev, current.st_ino) != identity:
                    raise GoodnotesError(f"Artifact staging directory changed: {stage}")
                self._io._verify_staging_marker(stage, kind="staging", token=token)
                self._fs._safe_remove_tree(stage, expected_identity=identity)
                self._cleanup_orphan_blobs(self._records)

    def _initialize_staging_directory(
        self, directory: Path, kind: str, token: str
    ) -> tuple[int, tuple[int, int]]:
        purpose = f"artifact {kind} directory"
        parent_fd = self._fs._open_directory_fd(directory.parent)
        child_fd: int | None = None
        identity: tuple[int, int] | None = None
        try:
            identity = self._fs._mkdir_owned(directory, purpose=purpose, exist_ok=False)
            child_fd = self._fs._open_directory_fd(directory)
            if self._fs._identity(os.fstat(child_fd)) != identity:
                raise GoodnotesError(f"{purpose.capitalize()} changed: {directory}")
            self._io._write_json_exclusive(
                directory / _STAGING_MARKER,
                self._io._staging_data(kind, token),
                mode=0o600,
                dir_fd=child_fd,
            )
            return child_fd, identity
        except BaseException:
            if child_fd is not None:
                os.close(child_fd)
            if identity is not None:
                current = self._fs._stat_at(parent_fd, directory.name)
                if current is not None and self._fs._identity(current) == identity:
                    self._fs._remove_empty_created(parent_fd, directory, identity)
            raise
        finally:
            os.close(parent_fd)

    def publish(self, key: str, stage: str | Path, result: dict[str, Any]) -> CachedArtifact:
        """Atomically publish a complete flat stage under a content-derived key."""

        self._require_open()
        self._validate_key(key)
        if not isinstance(result, dict):
            raise GoodnotesError("Artifact result metadata must be a JSON object.")
        serialized_result = self._io._json_bytes(result)
        stage_path = self._validate_active_stage(Path(stage).expanduser())
        self._prune_expired_records()
        existing = self.lookup(key)
        if existing is not None:
            return existing
        prepared = tuple(
            sorted(self._blobs._prepare_stage_files(stage_path), key=lambda item: item.name)
        )
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
        entry_marker_bytes = self._io._json_bytes(entry_data)
        if len(entry_marker_bytes) > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=len(entry_marker_bytes),
                max_bytes=self.max_bytes,
                cache_dir=self._fs.root,
            )

        self._make_room(prepared, len(entry_marker_bytes))

        token = uuid4().hex
        publish_name = f".publishing-{token}"
        publish_dir = self._fs.staging_root / publish_name
        publish_fd, publish_identity = self._initialize_staging_directory(
            publish_dir, "publishing", token
        )
        final_dir = self._fs.entries / key
        entry_fd: int | None = None
        try:
            entry_dir = publish_dir / "entry"
            self._fs._mkdir_owned(entry_dir, purpose="artifact publish entry", exist_ok=False)
            entry_fd = self._fs._open_directory_fd(entry_dir)
            for item in prepared:
                target = entry_dir / item.name
                if item.kind == "png":
                    assert item.digest is not None
                    blob = self._blobs._ensure_blob(item)
                    self._blobs._link_blob(blob, target)
                else:
                    self._blobs._copy_private_file(
                        item.path, target, expected_identity=item.source_identity
                    )
            # The entry marker is the publication record and is written last.
            self._io._write_json_exclusive(
                entry_dir / _ENTRY_MARKER, entry_data, mode=0o600, dir_fd=entry_fd
            )
            self._metadata._touch_marker(
                entry_fd,
                _ENTRY_MARKER,
                now,
                display_path=entry_dir / _ENTRY_MARKER,
                operation="set artifact entry use time",
            )
            self._metadata._verify_built_entry(entry_dir, key)
            entries_fd = self._fs._open_managed_directory("entries")
            try:
                current_entry = self._fs._stat_at(publish_fd, "entry")
                if current_entry is None or self._fs._identity(current_entry) != self._fs._identity(
                    os.fstat(entry_fd)
                ):
                    raise GoodnotesError(f"Artifact publish entry changed: {entry_dir}")
                if self._fs._stat_at(entries_fd, key) is not None:
                    raise GoodnotesError(f"Artifact cache entry appeared during publication: {key}")
                os.replace("entry", key, src_dir_fd=publish_fd, dst_dir_fd=entries_fd)
            finally:
                os.close(entries_fd)
            self._io._verify_staging_marker(publish_dir, kind="publishing", token=token)
            self._fs._safe_remove_tree(publish_dir, expected_identity=publish_identity)
        except BaseException:
            if publish_dir.exists() or publish_dir.is_symlink():
                current = self._fs._directory(publish_dir, purpose="artifact publish directory")
                if (current.st_dev, current.st_ino) != publish_identity:
                    raise GoodnotesError(
                        f"Artifact publish directory changed: {publish_dir}"
                    ) from None
                self._io._verify_staging_marker(publish_dir, kind="publishing", token=token)
                self._fs._safe_remove_tree(publish_dir, expected_identity=publish_identity)
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

        final_record = self._metadata._read_entry(final_dir, expected_key=key)
        self._records[key] = final_record
        self._pinned_keys.add(key)
        self._cleanup_orphan_blobs(self._records)
        return CachedArtifact(final_dir, json.loads(json.dumps(final_record.result)))

    def _require_open(self) -> None:
        if self._fs._lock_fd is None:
            raise RuntimeError("ArtifactCache must be used inside a context manager")
        self._fs._verify_managed_directories()

    def renew_leases(self) -> None:
        """Renew pinned results without releasing the session's artifact lock."""
        if self._fs._lock_fd is None or not self._pinned_keys:
            return
        self._fs._verify_managed_directories()
        now = self._now()
        for key in self._pinned_keys:
            record = self._records.get(key)
            if record is None:
                continue
            after = self._metadata._touch_entry(record, now, operation="renew artifact cache lease")
            self._records[key] = _EntryRecord(
                key,
                record.directory,
                record.result,
                record.files,
                record.marker_size,
                after.st_mtime,
                record.directory_identity,
            )

    @staticmethod
    def _validate_key(key: str) -> None:
        if not isinstance(key, str) or _KEY.fullmatch(key) is None:
            raise GoodnotesError("Artifact cache keys must be 64 lowercase hexadecimal characters.")

    def _validate_active_stage(self, stage: Path) -> Path:
        if stage.is_symlink():
            raise GoodnotesError("Artifact stage must not be a symlink.")
        try:
            resolved = stage.resolve(strict=True)
        except OSError as exc:
            raise GoodnotesError(f"Artifact stage does not exist: {stage}") from exc
        if (
            not path_contains(self._fs.staging_root, resolved)
            or resolved.parent != self._fs.staging_root
        ):
            raise GoodnotesError(
                "Artifact stage must be a direct child of this cache's staging root."
            )
        match = _STAGE_NAME.fullmatch(resolved.name)
        if match is None or self._active_stages.get(resolved.name) is None:
            raise GoodnotesError("Artifact stage must be active in this ArtifactCache context.")
        info = self._fs._directory(resolved, purpose="artifact staging directory")
        if self._fs._identity(info) != self._active_stages[resolved.name]:
            raise GoodnotesError("Artifact staging directory changed while it was active.")
        self._io._verify_staging_marker(resolved, kind="staging", token=match.group(1))
        return resolved

    @property
    def max_bytes(self) -> int:
        return self._io.max_bytes

    @max_bytes.setter
    def max_bytes(self, value: int) -> None:
        self._io.max_bytes = value

    @property
    def root(self) -> Path:
        return self._fs.root

    @property
    def entries(self) -> Path:
        return self._fs.entries

    @property
    def blobs(self) -> Path:
        return self._fs.blobs

    @property
    def staging_root(self) -> Path:
        return self._fs.staging_root

    def _maintain(self) -> None:
        self._require_open()
        self._blobs._reset_verification()
        self._cleanup_staging()
        self._records.clear()
        self._records.update(self._retention._scan_entries())
        self._cleanup_orphan_blobs(self._records)
        self._prune_expired_records()
        size = self._retention._storage_size()
        if size > self.max_bytes:
            self._make_room((), 0)
        size = self._retention._storage_size()
        if size > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=size,
                max_bytes=self.max_bytes,
                cache_dir=self._fs.root,
            )

    def _prune_expired_records(self) -> None:
        now = self._now()
        protected_digests = set(
            self._retention._active_stage_png_references(self._blobs._blob_inventory())
        )
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
        self._fs._verify_managed_directory(".staging")
        try:
            entries = list(os.scandir(self._fs.staging_root))
        except OSError as exc:
            raise GoodnotesError(
                f"Could not inspect artifact staging root: {self._fs.staging_root}"
            ) from exc
        for entry in entries:
            path = self._fs.staging_root / entry.name
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
            info = self._fs._directory(path, purpose="artifact staging directory")
            self._io._verify_staging_marker(path, kind=kind, token=token)
            self._fs._safe_remove_tree(path, expected_identity=self._fs._identity(info))

    def _make_room(
        self,
        prepared: tuple[_PreparedFile, ...],
        marker_size: int,
    ) -> None:
        required = self._retention._projected_size(prepared, marker_size)
        if required <= self.max_bytes:
            return
        standalone = self._retention._projected_size(prepared, marker_size, records={})
        if standalone > self.max_bytes:
            raise ArtifactCapacityError(
                required_bytes=standalone,
                max_bytes=self.max_bytes,
                cache_dir=self._fs.root,
            )
        protected_digests = set(
            self._retention._active_stage_png_references(self._blobs._blob_inventory())
        )
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
            required = self._retention._projected_size(prepared, marker_size)
            if required <= self.max_bytes:
                return
        raise ArtifactCapacityError(
            required_bytes=required,
            max_bytes=self.max_bytes,
            cache_dir=self._fs.root,
        )

    def _remove_entry(self, record: _EntryRecord) -> None:
        self._fs._verify_root_path()
        current = self._fs._directory(record.directory, purpose="artifact entry")
        if self._fs._identity(current) != record.directory_identity:
            raise GoodnotesError(f"Artifact entry changed before removal: {record.directory}")
        self._metadata._read_entry(
            record.directory,
            expected_key=record.key,
            verify_blobs=False,
            verify_files=False,
        )
        self._fs._safe_remove_tree(record.directory, expected_identity=record.directory_identity)
        self._records.pop(record.key, None)

    def _cleanup_orphan_blobs(self, records: Mapping[str, _EntryRecord]) -> None:
        references = Counter(
            file.digest
            for record in records.values()
            for file in record.files
            if file.kind == "png" and file.digest is not None
        )
        blob_map = self._blobs._blob_inventory()
        references.update(self._retention._active_stage_png_references(blob_map))
        directory_fd = self._fs._open_managed_directory("blobs")
        with _owned_fd(directory_fd):
            for digest, inventoried in blob_map.items():
                if references[digest]:
                    continue
                name = f"{digest}.png"
                path = self._fs.blobs / name
                info = self._blobs._blob_info(
                    digest,
                    expected_size=inventoried.st_size,
                    expected_identity=self._fs._identity(inventoried),
                    dir_fd=directory_fd,
                )
                if info.st_nlink != 1:
                    raise GoodnotesError(
                        f"Unreferenced artifact blob has unexpected hardlinks: {path}"
                    )
                try:
                    os.unlink(name, dir_fd=directory_fd)
                except OSError as exc:
                    raise GoodnotesError(f"Could not remove unused artifact blob: {path}") from exc
                self._blobs._forget_blob(digest, inventoried)
                self._blobs._forget_blob(digest, info)

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise GoodnotesError("Artifact cache clock returned an invalid time.")
        return value

    def _past_grace(self, last_used: float) -> bool:
        return self._now() - last_used >= self.grace_seconds
