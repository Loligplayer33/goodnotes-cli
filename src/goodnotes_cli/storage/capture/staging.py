"""Own one capture tree until its caller confirms publication."""

from __future__ import annotations

import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from ...errors import GoodnotesError
from ..paths import _remove_entry_at
from ..sandbox import SNAPSHOT_MARKER

_DIRECTORY_FLAGS = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _identity(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


class _CaptureStaging:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._parent_fd = -1
        self._root_fd = -1
        self._library_fd = -1
        self._parent_identity: tuple[int, int] | None = None
        self._root_identity: tuple[int, int] | None = None
        self._library_identity: tuple[int, int] | None = None
        self._marker_identity: tuple[int, int] | None = None
        self._temporary_marker: tuple[str, tuple[int, int]] | None = None
        self._released = False

    @property
    def library_path(self) -> Path:
        return self.path / "Library"

    def __enter__(self) -> _CaptureStaging:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            parent = self.path.parent.lstat()
            self._parent_identity = _identity(parent)
            self._parent_fd = os.open(self.path.parent, _DIRECTORY_FLAGS)
            if (
                not stat.S_ISDIR(parent.st_mode)
                or _identity(os.fstat(self._parent_fd)) != self._parent_identity
            ):
                raise GoodnotesError("Snapshot destination parent changed during creation.")
            os.mkdir(self.path.name, mode=0o700, dir_fd=self._parent_fd)
            root = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
            self._root_identity = _identity(root)
            self._root_fd = os.open(self.path.name, _DIRECTORY_FLAGS, dir_fd=self._parent_fd)
            self.validate()
            return self
        except BaseException as exc:
            self.__exit__(type(exc), exc, exc.__traceback__)
            if isinstance(exc, FileExistsError):
                raise GoodnotesError(
                    f"Refusing to overwrite an existing snapshot destination: {self.path}"
                ) from exc
            if isinstance(exc, OSError):
                raise GoodnotesError(f"Could not create snapshot destination: {self.path}") from exc
            raise

    def _validate_root(self, *, missing_ok: bool = False) -> bool:
        if self._parent_identity is None or self._root_identity is None:
            return False
        if (
            _identity(self.path.parent.lstat()) != self._parent_identity
            or _identity(os.fstat(self._parent_fd)) != self._parent_identity
        ):
            raise GoodnotesError("Snapshot destination parent changed during capture.")
        try:
            root = os.stat(self.path.name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            if missing_ok:
                return False
            raise
        if not stat.S_ISDIR(root.st_mode) or _identity(root) != self._root_identity:
            raise GoodnotesError("Snapshot staging directory changed during capture.")
        if self._root_fd >= 0 and _identity(os.fstat(self._root_fd)) != self._root_identity:
            raise GoodnotesError("Snapshot staging directory changed during capture.")
        return True

    def _validate_entry(
        self, name: str, expected: tuple[int, int] | None, *, missing_ok: bool = False
    ) -> None:
        try:
            current = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            if expected is None or missing_ok:
                return
            raise
        if expected is None or _identity(current) != expected:
            raise GoodnotesError(f"Snapshot staging entry changed during capture: {name}")

    def validate(self) -> None:
        self._validate_root()
        self._validate_entry("Library", self._library_identity)
        self._validate_entry(SNAPSHOT_MARKER, self._marker_identity)
        if (
            self._library_fd >= 0
            and _identity(os.fstat(self._library_fd)) != self._library_identity
        ):
            raise GoodnotesError("Snapshot staging Library changed during capture.")

    def create_library(self) -> Path:
        self.validate()
        os.mkdir("Library", mode=0o700, dir_fd=self._root_fd)
        info = os.stat("Library", dir_fd=self._root_fd, follow_symlinks=False)
        self._library_identity = _identity(info)
        self._library_fd = os.open("Library", _DIRECTORY_FLAGS, dir_fd=self._root_fd)
        self.validate()
        return self.library_path

    def _make_root_writable(self) -> None:
        info = os.fstat(self._root_fd)
        mode = info.st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if mode != info.st_mode:
            os.fchmod(self._root_fd, mode)

    def remove_library(self) -> None:
        self._validate_root()
        self._validate_entry("Library", self._library_identity, missing_ok=True)
        if self._library_identity is None:
            return
        self._make_root_writable()
        _remove_entry_at(
            self._root_fd,
            "Library",
            self.library_path,
            expected_identity=self._library_identity,
        )
        self._library_identity = None
        if self._library_fd >= 0:
            descriptor, self._library_fd = self._library_fd, -1
            os.close(descriptor)

    def _remove_temporary_marker(self) -> None:
        if self._temporary_marker is None:
            return
        name, identity = self._temporary_marker
        self._validate_root()
        _remove_entry_at(self._root_fd, name, self.path / name, expected_identity=identity)
        self._temporary_marker = None

    @contextmanager
    def marker_file(self) -> Generator[BinaryIO, None, None]:
        self.validate()
        name = f".snapshot-marker-{uuid4().hex}"
        descriptor = os.open(
            name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=self._root_fd
        )
        failed = False
        try:
            identity = _identity(os.fstat(descriptor))
            self._temporary_marker = name, identity
            stream = os.fdopen(descriptor, "wb")
            descriptor = -1
            try:
                yield stream
            except BaseException:
                try:
                    stream.close()
                except BaseException:
                    pass
                raise
            else:
                stream.close()
            self.validate()
            self._validate_entry(name, identity)
            os.replace(name, SNAPSHOT_MARKER, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd)
            # Interrupted calls have uncertain outcomes. Commit only returned calls.
            self._marker_identity = identity
            self._temporary_marker = None
        except BaseException:
            failed = True
            try:
                self._remove_temporary_marker()
            except BaseException:
                pass
            raise
        finally:
            if descriptor >= 0:
                try:
                    os.close(descriptor)
                except BaseException:
                    if not failed:
                        raise

    def rename(self, destination: Path) -> None:
        if destination.parent != self.path.parent:
            raise ValueError("Capture publication must stay in its original parent.")
        self.validate()
        try:
            os.stat(destination.name, dir_fd=self._parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise GoodnotesError(f"Refusing to overwrite a cache generation: {destination}")
        os.replace(
            self.path.name,
            destination.name,
            src_dir_fd=self._parent_fd,
            dst_dir_fd=self._parent_fd,
        )
        self.path = destination
        self.validate()

    def release(self) -> None:
        self._released = True

    def _remove(self) -> None:
        if not self._validate_root(missing_ok=True):
            return
        if self._root_fd >= 0:
            self.remove_library()
            self._make_root_writable()
            self._remove_temporary_marker()
            if self._marker_identity is not None:
                _remove_entry_at(
                    self._root_fd,
                    SNAPSHOT_MARKER,
                    self.path / SNAPSHOT_MARKER,
                    expected_identity=self._marker_identity,
                )
                self._marker_identity = None
        self._validate_root()
        # Unknown children are not ours. A nonempty root must remain untouched.
        os.rmdir(self.path.name, dir_fd=self._parent_fd)

    def __exit__(self, exc_type: object, exc: BaseException | None, traceback: object) -> None:
        cleanup_error: BaseException | None = None
        try:
            if not self._released:
                self._remove()
        except BaseException as failure:
            cleanup_error = failure
        finally:
            descriptors = self._library_fd, self._root_fd, self._parent_fd
            self._library_fd = self._root_fd = self._parent_fd = -1
            for descriptor in descriptors:
                if descriptor >= 0:
                    try:
                        os.close(descriptor)
                    except BaseException as failure:
                        cleanup_error = cleanup_error or failure
        if exc is None and cleanup_error is not None:
            raise cleanup_error
