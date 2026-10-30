"""Storage operations."""

from __future__ import annotations

import errno
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from goodnotes_cli.errors import FilesystemAccessError, GoodnotesError, _permission_denied

if TYPE_CHECKING:
    from goodnotes_cli.storage.sandbox import Sandbox


def _is_production_goodnotes_path(path: Path) -> bool:
    """Recognize macOS Goodnotes app containers from a path without opening it."""

    parts = [part.casefold() for part in path.parts]
    for index, part in enumerate(parts[:-1]):
        if part in {"containers", "group containers"} and "goodnotes" in parts[index + 1]:
            return True
        if part == "application support" and parts[index + 1] in {"goodnotes", "goodnotes 6"}:
            return True
    return False


def _resolved(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def trusted_macos_alias(path: Path) -> Path | None:
    """Return the fixed target of an identity-verified macOS system alias."""
    target = {Path("/tmp"): Path("/private/tmp"), Path("/var"): Path("/private/var")}.get(path)
    if sys.platform != "darwin" or target is None:
        return None
    try:
        if not stat.S_ISLNK(path.lstat().st_mode):
            return None
        alias_info, target_info = path.stat(), target.stat()
        if not stat.S_ISDIR(target_info.st_mode):
            return None
        if (alias_info.st_dev, alias_info.st_ino) == (target_info.st_dev, target_info.st_ino):
            return target
    except OSError:
        pass
    return None


def stat_key(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    """Return the shared identity and freshness signature for a filesystem entry."""

    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _lexically_contains(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
    except ValueError:
        return False
    return True


def _samefile(left: Path, right: Path) -> bool:
    try:
        return left.samefile(right)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return False
        raise


def path_contains(parent: str | Path, child: str | Path) -> bool:
    """Check path containment using resolved paths and existing directory identity.

    Lexical ancestry handles missing descendants. When spellings differ, an existing
    ancestor is compared by filesystem identity so case aliases on insensitive volumes
    are recognized without treating case variants as aliases on sensitive volumes.
    """

    resolved_parent = _resolved(parent)
    resolved_child = _resolved(child)
    if _lexically_contains(resolved_parent, resolved_child):
        return True

    try:
        parent_info = resolved_parent.stat()
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR}:
            return False
        raise
    if _samefile(resolved_parent, resolved_child):
        return True
    if not stat.S_ISDIR(parent_info.st_mode):
        return False

    for depth in range(1, len(resolved_child.parts) + 1):
        child_prefix = Path(*resolved_child.parts[:depth])
        if _samefile(resolved_parent, child_prefix):
            return True
    return False


def paths_overlap(left: str | Path, right: str | Path) -> bool:
    """Check whether two paths refer to overlapping filesystem locations."""

    return path_contains(left, right) or path_contains(right, left)


@dataclass(frozen=True)
class OutputProtection:
    """Known input roots excluded from generated outputs and mutable storage."""

    snapshot_root: Path
    source_root: Path | None = None

    def require_disjoint(self, path: Path, *, purpose: str) -> None:
        """Require an owned write location outside, and not above, protected roots."""

        self._require_outside(path, purpose=purpose, allow_ancestor=False)

    def require_scratch_parent(self, path: Path) -> None:
        """Allow an ancestor parent that will contain a fresh, separately checked child."""

        self._require_outside(path, purpose="The scratch directory", allow_ancestor=True)

    def _require_outside(self, path: Path, *, purpose: str, allow_ancestor: bool) -> None:
        for root, label in (
            (self.snapshot_root, "Goodnotes snapshot"),
            (self.source_root, "captured Goodnotes source"),
        ):
            if root is None:
                continue
            try:
                overlaps = (
                    path_contains(root, path) if allow_ancestor else paths_overlap(path, root)
                )
            except (OSError, RuntimeError) as exc:
                raise GoodnotesError(
                    f"Could not inspect the {label} for write authorization."
                ) from exc
            if overlaps:
                raise GoodnotesError(
                    f"{purpose} must be outside the {label}; paths must not overlap."
                )


def safe_remove_tree(path: str | Path) -> None:
    """Remove a path tree without following symlinks or changing their targets.

    Directory traversal is anchored to open descriptors and uses ``O_NOFOLLOW``.
    Only opened directory descriptors are made owner-writable; every non-directory
    entry, including a symlink, is unlinked as an entry in its containing directory.
    Filesystem errors are allowed to propagate so callers can preserve their existing
    source/cache permission classification.
    """

    target = Path(path).expanduser()
    if target.name in {"", ".", ".."}:
        raise GoodnotesError(f"Refusing to remove an unsafe filesystem root: {target}")

    try:
        requested_parent_info = target.parent.stat()
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(requested_parent_info.st_mode):
        raise GoodnotesError(f"Refusing to clean through a non-directory parent: {target.parent}")

    try:
        parent = target.parent.resolve(strict=True)
    except FileNotFoundError as exc:
        raise GoodnotesError(f"Filesystem parent changed during cleanup: {target.parent}") from exc

    expected_parent = parent.lstat()
    if not stat.S_ISDIR(expected_parent.st_mode):
        raise GoodnotesError(f"Refusing to clean through a non-directory parent: {parent}")
    if stat_key(expected_parent)[:2] != stat_key(requested_parent_info)[:2]:
        raise GoodnotesError(f"Filesystem parent changed during cleanup: {parent}")

    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, directory_flags)
    try:
        opened_parent = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(opened_parent.st_mode)
            or stat_key(opened_parent)[:2] != stat_key(expected_parent)[:2]
        ):
            raise GoodnotesError(f"Filesystem parent changed during cleanup: {parent}")
        _remove_entry_at(parent_fd, target.name, target)
    finally:
        os.close(parent_fd)


def _remove_entry_at(
    parent_fd: int,
    name: str,
    display_path: Path,
    *,
    expected_identity: tuple[int, int] | None = None,
) -> None:
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return

    if expected_identity is not None and stat_key(before)[:2] != expected_identity:
        raise GoodnotesError(f"Filesystem entry changed during cleanup: {display_path}")
    if not stat.S_ISDIR(before.st_mode):
        os.unlink(name, dir_fd=parent_fd)
        return

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(name, flags, dir_fd=parent_fd)
    try:
        opened = os.fstat(directory_fd)
        if not stat.S_ISDIR(opened.st_mode) or stat_key(opened)[:2] != stat_key(before)[:2]:
            raise GoodnotesError(f"Filesystem entry changed during cleanup: {display_path}")

        writable_mode = opened.st_mode | stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR
        if writable_mode != opened.st_mode:
            os.fchmod(directory_fd, writable_mode)

        for child_name in os.listdir(directory_fd):
            _remove_entry_at(directory_fd, child_name, display_path / child_name)

        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_key(current)[:2] != stat_key(opened)[:2]:
            raise GoodnotesError(f"Filesystem entry changed during cleanup: {display_path}")
    finally:
        os.close(directory_fd)

    os.rmdir(name, dir_fd=parent_fd)


def resolve_confined_file(root: str | Path, candidate: str | Path) -> Path | None:
    """Resolve an existing regular file below ``root``.

    A missing candidate returns ``None``. An existing path that resolves outside the
    root, or to something other than a regular file, raises ``GoodnotesError``. OS
    errors such as permission failures propagate unchanged for caller classification.
    ``candidate`` may be absolute or relative to ``root``.
    """

    resolved_root = Path(root).expanduser().resolve(strict=True)
    raw_candidate = Path(candidate).expanduser()
    requested = raw_candidate if raw_candidate.is_absolute() else resolved_root / raw_candidate
    prospective = requested.resolve(strict=False)
    if not path_contains(resolved_root, prospective):
        raise GoodnotesError("The requested file resolves outside its allowed directory.")

    try:
        resolved = requested.resolve(strict=True)
    except FileNotFoundError:
        return None
    if not path_contains(resolved_root, resolved):
        raise GoodnotesError("The requested file resolves outside its allowed directory.")

    info = resolved.stat()
    if not stat.S_ISREG(info.st_mode):
        raise GoodnotesError("The requested path is not a regular file.")
    return resolved


def _require_not_production(path: Path, *, purpose: str) -> None:
    if _is_production_goodnotes_path(path):
        raise GoodnotesError(f"Refusing to use a production Goodnotes path for {purpose}: {path}")


def safe_output(
    path: str | Path,
    sandbox: Sandbox,
    *,
    inputs: tuple[str | Path, ...] | list[str | Path] = (),
) -> Path:
    """Validate a new output path, including paths reached through symlinked parents."""

    try:
        return _safe_output(path, sandbox, inputs=inputs)
    except (OSError, GoodnotesError) as exc:
        if not _permission_denied(exc):
            raise
        raise FilesystemAccessError.from_exception(
            exc, operation="validate_output", path=path, kind="output"
        ) from exc


def _safe_output(
    path: str | Path,
    sandbox: Sandbox,
    *,
    inputs: tuple[str | Path, ...] | list[str | Path] = (),
) -> Path:

    requested = Path(path).expanduser()
    resolved = _resolved(requested)
    for candidate in (requested.absolute(), resolved):
        _require_not_production(candidate, purpose="an output")
    sandbox.output_protection().require_disjoint(resolved, purpose="Output paths")
    for input_path in inputs:
        resolved_input = _resolved(input_path)
        if paths_overlap(resolved, resolved_input):
            raise GoodnotesError("Output paths must not overlap any input path.")

    if requested.exists() or requested.is_symlink():
        raise GoodnotesError(f"Refusing to overwrite an existing output: {requested}")
    if resolved.parent.exists() and not resolved.parent.is_dir():
        raise GoodnotesError(f"Output parent is not a directory: {resolved.parent}")
    return resolved
