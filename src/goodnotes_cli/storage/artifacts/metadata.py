from __future__ import annotations

import math
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from goodnotes_cli.errors import GoodnotesError

from .records import (
    _DIGEST,
    _ENTRY_MARKER,
    _FILE_NAME,
    _KEY,
    _EntryRecord,
    _FileRecord,
)

if TYPE_CHECKING:
    from .blobs import ArtifactBlobs
    from .filesystem import ArtifactFilesystem
    from .io import ArtifactIO


class ArtifactMetadata:
    def __init__(self, fs: ArtifactFilesystem, io: ArtifactIO, blobs: ArtifactBlobs) -> None:
        self._fs, self._io, self._blobs = fs, io, blobs

    def _touch_entry(
        self,
        record: _EntryRecord,
        now: float,
        *,
        operation: str,
    ) -> os.stat_result:
        """Touch an entry marker through its verified directory descriptor."""

        entries_fd = self._fs._open_managed_directory("entries")
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
            self._fs._require_owner(entry_info, purpose="artifact entry")
            if (
                not stat.S_ISDIR(entry_info.st_mode)
                or self._fs._identity(entry_info) != record.directory_identity
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
            if (
                current_entry is None
                or self._fs._identity(current_entry) != record.directory_identity
            ):
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
        before = self._fs._stat_at(directory_fd, name)
        if before is None or not stat.S_ISREG(before.st_mode):
            raise GoodnotesError(f"Artifact metadata changed before {operation}: {display_path}")
        self._fs._require_owner(before, purpose="artifact entry marker")
        if before.st_nlink != 1 or stat.S_IMODE(before.st_mode) != 0o600:
            raise GoodnotesError(f"Artifact entry marker metadata is invalid: {display_path}")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        try:
            marker_fd = os.open(name, flags, dir_fd=directory_fd)
        except OSError as exc:
            raise GoodnotesError(f"Could not {operation}: {display_path}") from exc
        try:
            opened = os.fstat(marker_fd)
            if not stat.S_ISREG(opened.st_mode) or self._fs._identity(opened) != self._fs._identity(
                before
            ):
                raise GoodnotesError(
                    f"Artifact metadata changed before {operation}: {display_path}"
                )
            self._fs._require_owner(opened, purpose="artifact entry marker")
            try:
                os.utime(marker_fd, (now, now))
            except OSError as exc:
                raise GoodnotesError(f"Could not {operation}: {display_path}") from exc
            after = os.fstat(marker_fd)
            current = self._fs._stat_at(directory_fd, name)
            if (
                current is None
                or not stat.S_ISREG(current.st_mode)
                or self._fs._identity(after) != self._fs._identity(opened)
                or self._fs._identity(current) != self._fs._identity(opened)
            ):
                raise GoodnotesError(
                    f"Artifact metadata changed during {operation}: {display_path}"
                )
            return after
        finally:
            os.close(marker_fd)

    def _stat_at_fd_path(self, key: str) -> os.stat_result | None:
        entries_fd = self._fs._open_managed_directory("entries")
        try:
            return self._fs._stat_at(entries_fd, key)
        finally:
            os.close(entries_fd)

    def _read_entry(
        self,
        directory: Path,
        *,
        expected_key: str | None,
        blob_map: Mapping[str, os.stat_result] | None = None,
        verify_blobs: bool = False,
        verify_files: bool = False,
    ) -> _EntryRecord:
        info = self._fs._directory(directory, purpose="artifact entry")
        self._fs._require_owner(info, purpose="artifact entry")
        marker = directory / _ENTRY_MARKER
        data, marker_info = self._io._read_json_file(marker, purpose="artifact entry marker")
        self._fs._require_owner(marker_info, purpose="artifact entry marker")
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
            file_info = self._fs._regular_file(file_path, purpose="artifact entry file")
            self._fs._require_owner(file_info, purpose="artifact entry file")
            if file_info.st_size != size or stat.S_IMODE(file_info.st_mode) != 0o400:
                raise GoodnotesError(f"Artifact entry file metadata is invalid: {file_path}")
            if kind == "png":
                assert isinstance(digest, str)
                blob_path = self._fs.blobs / f"{digest}.png"
                if verify_blobs:
                    blob_info = self._blobs._verify_blob(digest, expected_size=size)
                elif blob_map is None:
                    blob_info = self._blobs._blob_info(digest, expected_size=size)
                else:
                    blob_info = blob_map.get(digest)
                    if blob_info is None or blob_info.st_size != size:
                        raise GoodnotesError(
                            f"Artifact PNG blob is missing or invalid: {blob_path}"
                        )
                if self._fs._identity(file_info) != self._fs._identity(blob_info):
                    raise GoodnotesError(
                        f"Artifact PNG is not linked to its content blob: {file_path}"
                    )
            else:
                if file_info.st_nlink != 1:
                    raise GoodnotesError(f"Artifact file must not be hardlinked: {file_path}")
                if verify_files:
                    actual_digest, actual_size, hashed = self._blobs._hash_file(file_path)
                    if actual_digest != digest or actual_size != size:
                        raise GoodnotesError(f"Artifact file failed its content check: {file_path}")
                    if self._fs._identity(hashed) != self._fs._identity(file_info):
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
            self._fs._identity(info),
        )

    def _verify_built_entry(self, directory: Path, key: str) -> None:
        self._read_entry(directory, expected_key=key)
