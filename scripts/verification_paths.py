"""Read-only path selection for disposable verification work."""

from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path

from goodnotes_cli.safety import path_contains

_SNAPSHOT_MARKER = "goodnotes-snapshot.json"


def _resolve(path: Path) -> Path:
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("unsafe_temporary_root") from exc


def temporary_parent(repository: Path, protected_roots: Sequence[Path] = ()) -> Path:
    """Select an existing disposable-work parent without probing or writing it."""
    selected = next(
        (os.environ[name] for name in ("TMPDIR", "TEMP", "TMP") if os.environ.get(name)),
        None,
    )
    if selected:
        parent = _resolve(Path(selected))
    else:
        parent = next(
            (
                resolved
                for candidate in (Path("/tmp"), Path("/var/tmp"), Path("/usr/tmp"))
                if (resolved := _resolve(candidate)).is_dir()
            ),
            None,
        )
        if parent is None:
            raise ValueError("unsafe_temporary_root")

    if not parent.is_dir():
        raise ValueError("unsafe_temporary_root")
    repository_root = _resolve(repository)
    resolved_protected = tuple(_resolve(path) for path in protected_roots)
    try:
        inside_repository = path_contains(repository_root, parent)
        inside_protected = any(path_contains(path, parent) for path in resolved_protected)
        has_library_ancestor = any(
            (ancestor / _SNAPSHOT_MARKER).exists() or (ancestor / "Databases").is_dir()
            for ancestor in (parent, *parent.parents)
        )
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError("unsafe_temporary_root") from exc
    if inside_repository or inside_protected:
        raise ValueError("unsafe_temporary_root")
    if has_library_ancestor:
        raise ValueError("unsafe_temporary_root")
    return parent
