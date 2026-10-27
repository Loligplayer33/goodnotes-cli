from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING, Any

from goodnotes_cli.errors import GoodnotesError

from .records import (
    _COPY_CHUNK,
    _STAGING_MARKER,
)

if TYPE_CHECKING:
    from .filesystem import ArtifactFilesystem


class ArtifactIO:
    def __init__(self, fs: ArtifactFilesystem, max_bytes: int) -> None:
        self._fs = fs
        self.max_bytes = max_bytes

    def _verify_staging_marker(self, directory: Path, *, kind: str, token: str) -> None:
        marker = directory / _STAGING_MARKER
        data, info = self._read_json_file(marker, purpose="artifact staging marker")
        self._fs._require_owner(info, purpose="artifact staging marker")
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
            info = self._fs._regular_file(path, purpose=purpose)
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
            if self._fs._identity(info) != self._fs._identity(opened):
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
                current = self._fs._regular_file(path, purpose=purpose)
            else:
                current = os.stat(path.name, dir_fd=dir_fd, follow_symlinks=False)
                if not stat.S_ISREG(current.st_mode):
                    raise GoodnotesError(f"{purpose.capitalize()} changed while being read: {path}")
            if self._fs._identity(after) != self._fs._identity(opened) or self._fs._identity(
                current
            ) != self._fs._identity(opened):
                raise GoodnotesError(f"{purpose.capitalize()} changed while being read: {path}")
            try:
                return json.loads(data.decode("utf-8")), after
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
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
            self._fs._verify_managed_parent(path)
        else:
            self._fs._verify_root_path()
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
        created_identity = None
        try:
            created_identity = self._fs._identity(os.fstat(descriptor))
            view = memoryview(encoded)
            while view:
                written = os.write(descriptor, view)
                view = view[written:]
            os.fchmod(descriptor, mode)
            os.fsync(descriptor)
        except BaseException:
            try:
                if created_identity is None:
                    created_identity = self._fs._identity(os.fstat(descriptor))
                current = path.lstat() if dir_fd is None else self._fs._stat_at(dir_fd, path.name)
                if current is not None and self._fs._identity(current) == created_identity:
                    if dir_fd is None:
                        path.unlink()
                    else:
                        os.unlink(path.name, dir_fd=dir_fd)
            except BaseException:
                pass
            raise
        finally:
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
