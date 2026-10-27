"""Storage operations."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from uuid import uuid4

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.storage.cache.state import _GENERATION
from goodnotes_cli.storage.inventory import _lstat
from goodnotes_cli.storage.metadata import SNAPSHOT_MARKER_LIMIT, read_json
from goodnotes_cli.storage.paths import safe_remove_tree as _safe_remove_tree
from goodnotes_cli.storage.sandbox import SNAPSHOT_MARKER, Sandbox

_STAGING = re.compile(r"^\.staging-[0-9a-f]{32}$")
_SIDECAR_KEY = re.compile(r"^[0-9a-f]{24}$")


def _assert_safe_tree(path: Path) -> None:
    pending = [path]
    while pending:
        directory = pending.pop()
        try:
            children = list(os.scandir(directory))
        except OSError as exc:
            raise GoodnotesError(f"Could not inspect generated snapshot: {directory}") from exc
        for child in children:
            child_path = directory / child.name
            info = _lstat(child_path)
            if stat.S_ISDIR(info.st_mode):
                pending.append(child_path)
            elif not stat.S_ISREG(info.st_mode):
                raise GoodnotesError(f"Refusing to remove a special cache file: {child_path}")


def _remove_generation(snapshots: Path, root: Path) -> None:
    _assert_safe_tree(root)
    _remove_registration_sidecar(root)
    tombstone = snapshots / f".staging-{uuid4().hex}"
    if tombstone.exists() or tombstone.is_symlink():
        raise GoodnotesError(f"Refusing to overwrite a staging snapshot: {tombstone}")
    try:
        os.rename(root, tombstone)
    except OSError as exc:
        raise GoodnotesError(f"Could not stage generated snapshot cleanup: {root}") from exc
    _safe_remove_tree(tombstone)


def _clean_abandoned_staging(snapshots: Path) -> None:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect snapshots directory: {snapshots}") from exc
    for entry in entries:
        if not _STAGING.fullmatch(entry.name):
            continue
        staging = snapshots / entry.name
        if staging.is_symlink():
            raise GoodnotesError(f"A staging snapshot must not be a symlink: {staging}")
        if not staging.is_dir():
            raise GoodnotesError(f"A staging snapshot must be a directory: {staging}")
        _safe_remove_tree(staging)


def _validate_generated_snapshots(snapshots: Path) -> None:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        raise GoodnotesError(f"Could not inspect snapshots directory: {snapshots}") from exc
    for entry in entries:
        if not _GENERATION.fullmatch(entry.name):
            continue
        root = snapshots / entry.name
        if root.is_symlink():
            raise GoodnotesError(f"A generated snapshot must not be a symlink: {root}")


def _has_valid_marker(root: Path) -> bool:
    marker = root / SNAPSHOT_MARKER
    if marker.is_symlink():
        raise GoodnotesError(f"A generated snapshot marker must not be a symlink: {marker}")
    if not marker.exists():
        return False
    try:
        if not stat.S_ISREG(marker.lstat().st_mode):
            return False
        value = read_json(marker, limit=SNAPSHOT_MARKER_LIMIT)
    except (OSError, ValueError):
        return False
    return isinstance(value, dict) and value.get("format_version") == 1


def _remove_registration_sidecar(root: Path) -> None:
    key = hashlib.sha256(os.fsencode(root.resolve(strict=True))).hexdigest()[:24]
    sidecar_parent = root.parent / ".goodnotes-cli"
    sidecar = sidecar_parent / key
    if sidecar_parent.is_symlink() or sidecar.is_symlink():
        raise GoodnotesError("The annotated-PDF registration sidecar must not be a symlink.")
    if not _SIDECAR_KEY.fullmatch(sidecar.name):
        return
    if sidecar.exists():
        if not sidecar.is_dir():
            raise GoodnotesError(
                f"The annotated-PDF registration sidecar is not a directory: {sidecar}"
            )
        _safe_remove_tree(sidecar)
    try:
        sidecar_parent.rmdir()
    except OSError:
        pass


def _prune_snapshots(
    snapshots: Path, active_name: str, previous_active_name: str | None = None
) -> list[str]:
    try:
        entries = list(os.scandir(snapshots))
    except OSError as exc:
        return [f"Could not inspect snapshots directory {snapshots}: {exc}"]
    warnings: list[str] = []
    for entry in entries:
        if not _GENERATION.fullmatch(entry.name):
            continue
        root = snapshots / entry.name
        try:
            if root.is_symlink():
                raise GoodnotesError(f"A generated snapshot must not be a symlink: {root}")
            if not root.is_dir() or entry.name == active_name:
                continue
            if entry.name != previous_active_name:
                if not _has_valid_marker(root):
                    continue
                # A valid marker and snapshot structure prove CLI ownership before pruning.
                Sandbox.load(root)
            _remove_generation(snapshots, root)
        except (GoodnotesError, OSError) as exc:
            warnings.append(f"Could not prune generated snapshot {entry.name}: {exc}")
    return warnings
