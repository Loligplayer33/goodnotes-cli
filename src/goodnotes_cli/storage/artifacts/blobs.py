from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.paths import (
    stat_key,
)

from .filesystem import _note_cleanup_failure, _owned_fd
from .records import (
    _COPY_CHUNK,
    _DIGEST,
    _FILE_NAME,
    _STAGING_MARKER,
    ArtifactCapacityError,
    _PreparedFile,
)

if TYPE_CHECKING:
    from .filesystem import ArtifactFilesystem
    from .io import ArtifactIO


class ArtifactBlobs:
    def __init__(self, fs: ArtifactFilesystem, io: ArtifactIO) -> None:
        self._fs, self._io = fs, io
        self._verified_blob_stats: dict[str, tuple[int, int, int, int, int]] = {}
        self._blob_digests_by_stat: dict[tuple[int, int, int, int, int], str] = {}

    def _reset_verification(self) -> None:
        self._verified_blob_stats.clear()
        self._blob_digests_by_stat.clear()

    def _index_inventory(self, blobs: Mapping[str, os.stat_result]) -> None:
        self._blob_digests_by_stat = {
            self._fs._content_signature(info): digest for digest, info in blobs.items()
        }

    def _digest_for_file(self, info: os.stat_result) -> str | None:
        return self._blob_digests_by_stat.get(self._fs._content_signature(info))

    def _forget_blob(self, digest: str, info: os.stat_result) -> None:
        self._verified_blob_stats.pop(digest, None)
        self._blob_digests_by_stat.pop(self._fs._content_signature(info), None)

    def _prepare_stage_files(
        self,
        stage: Path,
    ) -> tuple[_PreparedFile, ...]:
        self._io._verify_staging_marker(
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
            info = self._fs._regular_file(path, purpose="staged artifact file")
            self._fs._require_owner(info, purpose="staged artifact file")
            if info.st_size > self._io.max_bytes:
                raise ArtifactCapacityError(
                    required_bytes=info.st_size,
                    max_bytes=self._io.max_bytes,
                    cache_dir=self._fs.root,
                )
            if child.name.endswith(".png"):
                signature = self._fs._content_signature(info)
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

        directory_fd = self._fs._open_managed_directory("blobs")
        with _owned_fd(directory_fd):
            try:
                items = list(os.scandir(directory_fd))
            except OSError as exc:
                raise GoodnotesError(f"Could not inspect artifact blobs: {self._fs.blobs}") from exc
            result: dict[str, os.stat_result] = {}
            for item in items:
                match = re.fullmatch(r"([0-9a-f]{64})\.png", item.name)
                if match is None:
                    raise GoodnotesError(f"Unexpected item in artifact blobs: {item.name}")
                path = self._fs.blobs / item.name
                if stat.S_ISLNK(item.stat(follow_symlinks=False).st_mode):
                    raise GoodnotesError(f"Artifact blob must not be a symlink: {path}")
                digest = match.group(1)
                result[digest] = self._blob_info(digest, dir_fd=directory_fd)
            return result

    def _verify_blob(self, digest: str, *, expected_size: int | None = None) -> os.stat_result:
        if _DIGEST.fullmatch(digest) is None:
            raise GoodnotesError("Artifact blob digest is invalid.")
        path = self._fs.blobs / f"{digest}.png"
        directory_fd = self._fs._open_managed_directory("blobs")
        try:
            info = self._blob_info(digest, expected_size=expected_size, dir_fd=directory_fd)
            actual_digest, size, after = self._hash_file(path, dir_fd=directory_fd)
            if actual_digest != digest or size != info.st_size or stat_key(after) != stat_key(info):
                raise GoodnotesError(f"Artifact PNG blob failed its content check: {path}")
            self._verified_blob_stats[digest] = self._fs._content_signature(after)
            self._blob_digests_by_stat[self._fs._content_signature(after)] = digest
            return after
        finally:
            os.close(directory_fd)

    def _verified_blob(self, digest: str, *, expected_size: int | None = None) -> os.stat_result:
        info = self._blob_info(digest, expected_size=expected_size)
        if self._verified_blob_stats.get(digest) != self._fs._content_signature(info):
            return self._verify_blob(digest, expected_size=expected_size)
        return info

    def _blob_info(
        self,
        digest: str,
        *,
        expected_size: int | None = None,
        expected_identity: tuple[int, int] | None = None,
        dir_fd: int | None = None,
    ) -> os.stat_result:
        if _DIGEST.fullmatch(digest) is None:
            raise GoodnotesError("Artifact blob digest is invalid.")
        path = self._fs.blobs / f"{digest}.png"
        owned_fd = None
        if dir_fd is None:
            owned_fd = self._fs._open_managed_directory("blobs")
            dir_fd = owned_fd
        try:
            info = self._fs._stat_at(dir_fd, path.name)
            if info is None or not stat.S_ISREG(info.st_mode):
                raise GoodnotesError(
                    f"Artifact PNG blob must be a regular, non-symlink file: {path}"
                )
        finally:
            if owned_fd is not None:
                os.close(owned_fd)
        self._fs._require_owner(info, purpose="artifact PNG blob")
        if expected_identity is not None and self._fs._identity(info) != expected_identity:
            raise GoodnotesError(f"Artifact PNG blob changed before removal: {path}")
        if stat.S_IMODE(info.st_mode) != 0o400 or (
            expected_size is not None and info.st_size != expected_size
        ):
            raise GoodnotesError(f"Artifact PNG blob metadata is invalid: {path}")
        self._blob_digests_by_stat[self._fs._content_signature(info)] = digest
        return info

    def _ensure_blob(self, item: _PreparedFile) -> Path:
        assert item.digest is not None
        target = self._fs.blobs / f"{item.digest}.png"
        directory_fd = self._fs._open_managed_directory("blobs")
        with _owned_fd(directory_fd):
            if self._fs._stat_at(directory_fd, target.name) is not None:
                self._verified_blob(item.digest, expected_size=item.size)
                return target

            # Copy into a private staging file, then atomically create the
            # canonical name relative to the verified blobs directory.
            temp = item.path.parent / f".blob-{uuid4().hex}.tmp"
            temp_parent_fd = self._fs._open_directory_fd(temp.parent)
            with _owned_fd(temp_parent_fd):
                temp_identity: tuple[int, int] | None = None
                primary_error: BaseException | None = None
                try:
                    try:
                        temp_identity = self._copy_private_file(
                            item.path,
                            temp,
                            expected_identity=item.source_identity,
                            destination_parent_fd=temp_parent_fd,
                        )
                        temp_fd = os.open(
                            temp.name,
                            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                            dir_fd=temp_parent_fd,
                        )
                        with _owned_fd(temp_fd):
                            temp_info = os.fstat(temp_fd)
                            if (
                                not stat.S_ISREG(temp_info.st_mode)
                                or self._fs._identity(temp_info) != temp_identity
                            ):
                                raise GoodnotesError(f"Temporary artifact blob is unsafe: {temp}")
                            os.fchmod(temp_fd, 0o400)
                        os.link(
                            temp.name,
                            target.name,
                            src_dir_fd=temp_parent_fd,
                            dst_dir_fd=directory_fd,
                            follow_symlinks=False,
                        )
                        published = self._fs._stat_at(directory_fd, target.name)
                        if published is None or self._fs._identity(published) != temp_identity:
                            raise GoodnotesError(
                                f"Temporary artifact blob changed during publication: {temp}"
                            )
                    except FileExistsError:
                        self._verified_blob(item.digest, expected_size=item.size)
                    except OSError as exc:
                        raise GoodnotesError(
                            f"Could not publish artifact PNG blob: {target}"
                        ) from exc
                except BaseException as primary:
                    primary_error = primary
                    raise
                finally:
                    try:
                        if temp_identity is not None:
                            current = self._fs._stat_at(temp_parent_fd, temp.name)
                            if current is not None:
                                if self._fs._identity(current) != temp_identity:
                                    raise GoodnotesError(f"Temporary artifact blob changed: {temp}")
                                os.unlink(temp.name, dir_fd=temp_parent_fd)
                    except BaseException as cleanup:
                        if primary_error is not None:
                            _note_cleanup_failure(
                                primary_error, cleanup, operation="temporary blob cleanup"
                            )
                        elif isinstance(cleanup, OSError):
                            raise GoodnotesError(
                                f"Could not remove temporary artifact blob: {temp}"
                            ) from cleanup
                        else:
                            raise
        blob_info = self._blob_info(item.digest, expected_size=item.size)
        self._verified_blob_stats[item.digest] = self._fs._content_signature(blob_info)
        self._blob_digests_by_stat[self._fs._content_signature(blob_info)] = item.digest
        return target

    def _link_blob(self, source: Path, destination: Path) -> None:
        with ExitStack() as resources:
            source_fd = self._fs._open_directory_fd(source.parent)
            resources.enter_context(_owned_fd(source_fd))
            destination_fd = self._fs._open_directory_fd(destination.parent)
            resources.enter_context(_owned_fd(destination_fd))
            try:
                source_info = self._fs._stat_at(source_fd, source.name)
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
                raise GoodnotesError(
                    f"Could not link generated artifact image: {destination}"
                ) from exc

    def _copy_private_file(
        self,
        source: Path,
        destination: Path,
        *,
        expected_identity: tuple[int, int, int, int, int, int] | None,
        destination_parent_fd: int | None = None,
    ) -> tuple[int, int]:
        with ExitStack() as resources:
            source_parent_fd = self._fs._open_directory_fd(source.parent)
            resources.enter_context(_owned_fd(source_parent_fd))
            destination_parent_fd = (
                self._fs._open_directory_fd(destination.parent)
                if destination_parent_fd is None
                else os.dup(destination_parent_fd)
            )
            resources.enter_context(_owned_fd(destination_parent_fd))
            before = self._fs._stat_at(source_parent_fd, source.name)
            if before is None or not stat.S_ISREG(before.st_mode):
                raise GoodnotesError(f"Artifact staging file must be regular: {source}")
            if expected_identity is not None and stat_key(before) != expected_identity:
                raise GoodnotesError(f"Artifact staging file changed before copy: {source}")
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
            try:
                source_fd = os.open(source.name, flags, dir_fd=source_parent_fd)
            except OSError as exc:
                raise GoodnotesError(f"Could not open artifact staging file: {source}") from exc
            resources.enter_context(_owned_fd(source_fd))
            created_identity: tuple[int, int] | None = None
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
                with _owned_fd(target_fd):
                    created_identity = self._fs._identity(os.fstat(target_fd))
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
                    current = self._fs._stat_at(source_parent_fd, source.name)
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
                    return self._fs._identity(os.fstat(target_fd))
            except BaseException as primary:
                if created_identity is not None:
                    try:
                        current = self._fs._stat_at(destination_parent_fd, destination.name)
                        if current is not None and self._fs._identity(current) == created_identity:
                            try:
                                os.unlink(destination.name, dir_fd=destination_parent_fd)
                            except FileNotFoundError:
                                pass
                    except BaseException as cleanup:
                        _note_cleanup_failure(primary, cleanup, operation="private copy rollback")
                raise

    def _hash_file(
        self, path: Path, *, dir_fd: int | None = None
    ) -> tuple[str, int, os.stat_result]:
        if dir_fd is None:
            self._fs._verify_managed_parent(path)
            before = self._fs._regular_file(path, purpose="artifact file")
        else:
            before = self._fs._stat_at(dir_fd, path.name)
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
                current = self._fs._regular_file(path, purpose="artifact file")
            else:
                current = self._fs._stat_at(dir_fd, path.name)
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
