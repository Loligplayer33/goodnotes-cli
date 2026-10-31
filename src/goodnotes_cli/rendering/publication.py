"""Atomic no-overwrite publication for generated files."""

from __future__ import annotations

import os
import tempfile
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
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


_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


@dataclass
class _BundleStage:
    path: Path
    parent_fd: int
    directory_fd: int
    parent_identity: tuple[int, int]
    directory_identity: tuple[int, int]
    publication_completed: bool = False


def _close_bundle_descriptors(descriptors: list[int], primary: BaseException | None) -> None:
    """Detach and attempt each release once, preserving an earlier operation failure."""
    failure = primary
    while descriptors:
        descriptor = descriptors.pop()
        try:
            os.close(descriptor)
        except BaseException as exc:
            failure = failure or exc
    if primary is None and failure is not None:
        raise failure


def _check_bundle_stage(stage: _BundleStage) -> None:
    if (
        _identity(os.fstat(stage.directory_fd)) != stage.directory_identity
        or _identity(os.stat(stage.path.name, dir_fd=stage.parent_fd, follow_symlinks=False))
        != stage.directory_identity
    ):
        raise GoodnotesError("The bundle staging directory changed before publication.")
    if _identity(stage.path.parent.stat()) != stage.parent_identity:
        raise GoodnotesError("The bundle output parent changed during staging or publication.")


@contextmanager
def bundle_stage(destination: Path) -> Generator[_BundleStage, None, None]:
    """Own one sibling stage and its pinned directories through explicit bundle creation."""
    parent_existed = destination.parent.exists()
    descriptors: list[int] = []
    parent_identity: tuple[int, int] | None = None
    temporary: Path | None = None
    stage_identity: tuple[int, int] | None = None
    stage: _BundleStage | None = None
    primary: BaseException | None = None
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent_fd = os.open(destination.parent, _DIRECTORY_FLAGS)
        descriptors.append(parent_fd)
        parent_identity = _identity(os.fstat(parent_fd))
        temporary = Path(tempfile.mkdtemp(prefix=".goodnotes-bundle-", dir=destination.parent))
        stage_identity = _identity(os.stat(temporary.name, dir_fd=parent_fd, follow_symlinks=False))
        source_fd = os.open(temporary.name, _DIRECTORY_FLAGS, dir_fd=parent_fd)
        descriptors.append(source_fd)
        stage = _BundleStage(temporary, parent_fd, source_fd, parent_identity, stage_identity)
        _check_bundle_stage(stage)
        yield stage
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            if temporary is not None and stage_identity is not None:
                try:
                    _remove_entry_at(
                        descriptors[0], temporary.name, temporary, expected_identity=stage_identity
                    )
                except (OSError, GoodnotesError):
                    pass
            if (
                primary is not None
                and not parent_existed
                and parent_identity is not None
                and (stage is None or not stage.publication_completed)
            ):
                try:
                    if _identity(destination.parent.lstat()) == parent_identity:
                        destination.parent.rmdir()
                except OSError:
                    pass
        except BaseException as cleanup:
            if primary is None:
                primary = cleanup
                raise
        finally:
            _close_bundle_descriptors(descriptors, primary)


def _rollback_bundle(
    stage: _BundleStage,
    destination: Path,
    directory_fd: int | None,
    directory_identity: tuple[int, int] | None,
    published: list[tuple[str, tuple[int, int]]],
) -> None:
    if directory_fd is not None:
        for name, identity in published:
            try:
                if _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) == identity:
                    os.unlink(name, dir_fd=directory_fd)
            except OSError:
                pass
    if directory_identity is not None:
        try:
            if (
                _identity(os.stat(destination.name, dir_fd=stage.parent_fd, follow_symlinks=False))
                == directory_identity
            ):
                os.rmdir(destination.name, dir_fd=stage.parent_fd)
        except OSError:
            pass


def publish_bundle(stage: _BundleStage, destination: Path) -> None:
    """Borrow the pinned stage and own only the reservation and confirmed output links."""
    directory_fd: int | None = None
    directory_identity: tuple[int, int] | None = None
    published: list[tuple[str, tuple[int, int]]] = []
    primary: BaseException | None = None
    try:
        _check_bundle_stage(stage)
        os.mkdir(destination.name, mode=0o700, dir_fd=stage.parent_fd)
        directory_identity = _identity(
            os.stat(destination.name, dir_fd=stage.parent_fd, follow_symlinks=False)
        )
        directory_fd = os.open(destination.name, _DIRECTORY_FLAGS, dir_fd=stage.parent_fd)
        if _identity(os.fstat(directory_fd)) != directory_identity:
            raise GoodnotesError("The bundle output directory changed during publication.")
        files = sorted(os.listdir(stage.directory_fd), key=lambda name: name == "text.json")
        for name in files:
            identity = _identity(os.stat(name, dir_fd=stage.directory_fd, follow_symlinks=False))
            os.link(
                name,
                name,
                src_dir_fd=stage.directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
            # Only calls that returned successfully establish rollback ownership.
            published.append((name, identity))
        if (
            _identity(os.stat(destination.name, dir_fd=stage.parent_fd, follow_symlinks=False))
            != directory_identity
        ):
            raise GoodnotesError("The bundle output directory changed during publication.")
        if _identity(destination.parent.stat()) != stage.parent_identity:
            raise GoodnotesError("The bundle output parent changed during publication.")
        for name, identity in published:
            if _identity(os.stat(name, dir_fd=directory_fd, follow_symlinks=False)) != identity:
                raise GoodnotesError("A bundle output file changed during publication.")
        stage.publication_completed = True
    except BaseException as exc:
        primary = exc
        try:
            _rollback_bundle(stage, destination, directory_fd, directory_identity, published)
        except BaseException:
            pass
        raise
    finally:
        _close_bundle_descriptors([] if directory_fd is None else [directory_fd], primary)
