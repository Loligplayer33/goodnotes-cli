"""Atomic no-overwrite publication for generated files."""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

from ..errors import FilesystemAccessError, GoodnotesError
from ..storage.paths import _remove_entry_at


def _publish_new_file(
    destination: Path,
    writer: Callable[[str], object],
) -> None:
    """Write to a sibling temporary file, then create the destination without overwriting."""
    temporary = destination.with_name(
        f".{destination.stem}.tmp-{os.getpid()}-{uuid.uuid4().hex}{destination.suffix}"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer(str(temporary))
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise GoodnotesError("The document renderer did not create an output file.")
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise GoodnotesError(f"The output file already exists: {destination}") from exc
    except GoodnotesError:
        raise
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="write_document_output", path=destination, kind="output"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def publish_bundle(
    temporary: Path,
    destination: Path,
    *,
    expected_source_identity: tuple[int, int] | None = None,
) -> None:
    """Publish into the reserved directory and remove only entries we created on failure."""
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_parent_fd = os.open(temporary.parent, flags)
    source_fd = -1
    parent_fd = -1
    directory_fd = -1
    source_identity: tuple[int, int] | None = None
    directory_identity: tuple[int, int] | None = None
    published: list[tuple[str, tuple[int, int]]] = []
    reserved = False
    try:
        source_fd = os.open(temporary.name, flags | nofollow, dir_fd=source_parent_fd)
        source_identity = _identity(os.fstat(source_fd))
        if (
            expected_source_identity is not None and source_identity != expected_source_identity
        ) or _identity(
            os.stat(temporary.name, dir_fd=source_parent_fd, follow_symlinks=False)
        ) != source_identity:
            raise GoodnotesError("The bundle staging directory changed before publication.")
        parent_fd = os.open(destination.parent, flags)
        parent_identity = _identity(os.fstat(parent_fd))
        os.mkdir(destination.name, mode=0o700, dir_fd=parent_fd)
        reserved = True
        directory_identity = _identity(
            os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False)
        )
        directory_fd = os.open(destination.name, flags | nofollow, dir_fd=parent_fd)
        if _identity(os.fstat(directory_fd)) != directory_identity:
            raise GoodnotesError("The bundle output directory changed during publication.")
        files = sorted(os.listdir(source_fd), key=lambda name: name == "text.json")
        for name in files:
            identity = _identity(os.stat(name, dir_fd=source_fd, follow_symlinks=False))
            os.link(
                name, name, src_dir_fd=source_fd, dst_dir_fd=directory_fd, follow_symlinks=False
            )
            # An interrupted syscall has an unknown outcome. Only confirmed links
            # belong to rollback, even if an uncertain entry matches the source inode.
            published.append((name, identity))
        if (
            _identity(os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False))
            != directory_identity
        ):
            raise GoodnotesError("The bundle output directory changed during publication.")
        if _identity(destination.parent.stat()) != parent_identity:
            raise GoodnotesError("The bundle output parent changed during publication.")
        for name, identity in published:
            if _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) != identity:
                raise GoodnotesError("A bundle output file changed during publication.")
    except BaseException:
        if directory_fd >= 0:
            for name, identity in published:
                try:
                    if (
                        _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False))
                        == identity
                    ):
                        os.unlink(name, dir_fd=directory_fd)
                except OSError:
                    pass
        if directory_identity is not None and parent_fd >= 0:
            try:
                if (
                    _identity(os.stat(destination.name, dir_fd=parent_fd, follow_symlinks=False))
                    == directory_identity
                ):
                    os.rmdir(destination.name, dir_fd=parent_fd)
            except OSError:
                pass
        raise
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)
        if parent_fd >= 0:
            os.close(parent_fd)
        try:
            if reserved and source_identity is not None:
                try:
                    _remove_entry_at(
                        source_parent_fd,
                        temporary.name,
                        temporary,
                        expected_identity=source_identity,
                    )
                except (OSError, GoodnotesError):
                    pass
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            os.close(source_parent_fd)
