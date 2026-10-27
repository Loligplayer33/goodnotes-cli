from __future__ import annotations

import errno
import fcntl
import os
import stat
from pathlib import Path
from typing import TYPE_CHECKING

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.paths import (
    _remove_entry_at,
    _require_not_production,
    paths_overlap,
    stat_key,
    trusted_macos_alias,
)

from .records import (
    _LOCK_NAME,
    _OWNER_UID,
    _ROOT_MARKER,
    _ROOT_OWNER,
)

if TYPE_CHECKING:
    from .io import ArtifactIO


class ArtifactFilesystem:
    def __init__(self, root: Path, snapshot_root: Path) -> None:
        self._requested_root = root
        self._requested_snapshot_root = snapshot_root
        self.root = root
        self.entries = root / "entries"
        self.blobs = root / "blobs"
        self.staging_root = root / ".staging"
        self._root_fd: int | None = None
        self._root_identity: tuple[int, int] | None = None
        self._managed_directory_identities: dict[str, tuple[int, int]] = {}
        self._lock_fd: int | None = None
        self._io: ArtifactIO

    def _prepare_root(self) -> None:
        raw_root = self._requested_root
        requested_snapshot = self._requested_snapshot_root
        if not raw_root.is_absolute():
            raw_root = Path(os.path.abspath(raw_root))
        if not requested_snapshot.is_absolute():
            requested_snapshot = Path(os.path.abspath(requested_snapshot))
        raw_root = self._normalize_system_alias(raw_root)
        requested_snapshot = self._normalize_system_alias(requested_snapshot)
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
                    value, marker_info = self._io._read_json_file(
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
                try:
                    info = os.fstat(child)
                    if not stat.S_ISDIR(info.st_mode):
                        raise GoodnotesError(
                            f"Artifact cache path component is not a directory: {part}"
                        )
                except BaseException:
                    os.close(child)
                    raise
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
            self._io._write_json_exclusive(marker, _ROOT_OWNER, mode=0o600, dir_fd=root_fd)
        value, info = self._io._read_json_file(
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
    def _normalize_system_alias(path: Path) -> Path:
        # The alias may be an ancestor, never the selected root itself.
        if len(path.parts) > 2:
            target = trusted_macos_alias(Path(path.anchor) / path.parts[1])
            if target is not None:
                return target.joinpath(*path.parts[2:])
        return path

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
                try:
                    info = os.fstat(child)
                    if not stat.S_ISDIR(info.st_mode):
                        raise GoodnotesError(f"Artifact path component is not a directory: {path}")
                    expected = self._managed_directory_identities.get(part)
                    if expected is not None and self._identity(info) != expected:
                        raise GoodnotesError(
                            f"Managed artifact directory changed: {self.root / part}"
                        )
                    self._require_owner(info, purpose="artifact directory")
                    if info.st_mode & 0o077:
                        raise GoodnotesError(f"Artifact directory is not private: {path}")
                except BaseException:
                    os.close(child)
                    raise
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

    def _remove_empty_created(self, parent_fd: int, path: Path, identity: tuple[int, int]) -> None:
        current = self._stat_at(parent_fd, path.name)
        if current is None:
            return
        if self._identity(current) != identity:
            raise GoodnotesError(f"Filesystem entry changed during cleanup: {path}")
        try:
            os.rmdir(path.name, dir_fd=parent_fd)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise

    def _safe_remove_tree(
        self, path: Path, *, expected_identity: tuple[int, int] | None = None
    ) -> None:
        parent_fd = self._open_directory_fd(path.parent)
        try:
            _remove_entry_at(parent_fd, path.name, path, expected_identity=expected_identity)
        finally:
            os.close(parent_fd)

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

    def _mkdir_owned(self, path: Path, *, purpose: str, exist_ok: bool) -> tuple[int, int]:
        parent_fd = self._open_directory_fd(path.parent)
        child_fd: int | None = None
        created_identity: tuple[int, int] | None = None
        try:
            try:
                os.mkdir(path.name, mode=0o700, dir_fd=parent_fd)
                created = True
            except FileExistsError:
                if not exist_ok:
                    raise
                created = False
            flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
            # Capture the created directory before later initialization can fail.
            if created:
                created_info = self._stat_at(parent_fd, path.name)
                if created_info is None or not stat.S_ISDIR(created_info.st_mode):
                    raise GoodnotesError(
                        f"{purpose.capitalize()} changed while being created: {path}"
                    )
                self._require_owner(created_info, purpose=purpose)
                created_identity = self._identity(created_info)
            child_fd = os.open(path.name, flags, dir_fd=parent_fd)
            info = os.fstat(child_fd)
            current = self._stat_at(parent_fd, path.name)
            if (
                not stat.S_ISDIR(info.st_mode)
                or current is None
                or self._identity(current) != self._identity(info)
                or (created_identity is not None and self._identity(info) != created_identity)
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
            return self._identity(info)
        except BaseException as exc:
            if created_identity is not None:
                current = self._stat_at(parent_fd, path.name)
                if current is not None and self._identity(current) == created_identity:
                    # Before initialization writes a marker the directory must be
                    # empty. rmdir leaves unknown contents intact and fails closed.
                    os.rmdir(path.name, dir_fd=parent_fd)
            if isinstance(exc, OSError):
                raise GoodnotesError(f"Could not create {purpose}: {path}") from exc
            raise
        finally:
            if child_fd is not None:
                os.close(child_fd)
            os.close(parent_fd)
