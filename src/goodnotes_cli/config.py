"""Resolve the active local read view without touching the Goodnotes application."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CacheAccessError, GoodnotesError, _permission_denied
from .storage.sandbox import Sandbox

if TYPE_CHECKING:
    from .presentation.contracts import Projection


def default_data_dir() -> Path:
    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return Path(os.environ.get("GOODNOTES_DATA_DIR", cache / "goodnotes-cli")).expanduser()


def default_library() -> Path:
    return Path.home() / "Library/Containers/com.goodnotesapp.x/Data/Library"


@dataclass
class Settings:
    sandbox: Path | None = None
    data_dir: Path | None = None
    text: bool = False
    fields: str | None = None
    expect_snapshot: str | None = None
    snapshot_id: str | None = None
    projection: Projection | None = None
    cache_dir: Path | None = None
    brief: bool = False
    library_root: str | None = None

    def load_library_root(self) -> str | None:
        """Read an explicit library namespace without changing the cache or snapshot."""
        selected = self.library_root
        if selected is None and self.sandbox is None and self.cache_dir is not None:
            path = self.cache_dir / "library-root.json"
            try:
                before = path.lstat()
                descriptor = os.open(
                    path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
                )
            except FileNotFoundError:
                return None
            except OSError as exc:
                raise CacheAccessError.from_exception(
                    exc,
                    "Could not read the library root selection.",
                    operation="read_library_selection",
                    path=path,
                    cache_dir=self.cache_dir,
                ) from exc
            try:
                with os.fdopen(descriptor, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(before.st_mode)
                        or not stat.S_ISREG(info.st_mode)
                        or (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino)
                        or info.st_size > 4096
                    ):
                        raise ValueError("Invalid selection file.")
                    raw = stream.read(4097)
                    after = path.lstat()
                    final = os.fstat(stream.fileno())

                    def identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
                        return (
                            value.st_dev,
                            value.st_ino,
                            value.st_size,
                            value.st_mtime_ns,
                            value.st_ctime_ns,
                        )

                    if identity(before) != identity(info) or any(
                        identity(value) != identity(info) for value in (after, final)
                    ):
                        raise ValueError("Selection file changed while reading.")
                    if len(raw) > 4096:
                        raise ValueError("Selection file is too large.")
                    data = json.loads(raw)
                if not isinstance(data, dict) or set(data) != {"root_folder_id"}:
                    raise ValueError("Invalid selection fields.")
                selected = data["root_folder_id"]
                if not isinstance(selected, str) or not selected.strip():
                    raise ValueError("Invalid root ID.")
            except (OSError, ValueError, TypeError, RecursionError) as exc:
                raise CacheAccessError(
                    "cache_invalid",
                    "The library root selection is invalid.",
                    operation="read_library_selection",
                    path=path,
                    cache_dir=self.cache_dir,
                ) from exc
        if selected is not None and (not isinstance(selected, str) or not selected.strip()):
            raise GoodnotesError("Library root selection must be a nonempty root ID or all.")
        return None if selected == "all" else selected

    def load_sandbox(self) -> Sandbox:
        if self.sandbox is not None:
            return Sandbox.load(self.sandbox)
        state = Path(self.data_dir or default_data_dir()).expanduser().resolve(strict=False)
        pointer = state / "active.json"

        def invalid_pointer() -> CacheAccessError:
            return CacheAccessError(
                "cache_invalid",
                f"The active library pointer is invalid: {pointer}",
                operation="read_active_pointer",
                path=pointer,
                cache_dir=pointer.parent,
            )

        # Use the cache's canonical pointer checks so direct settings loads and
        # locked read-view loads apply the same generation and path rules.
        from .storage.cache.state import PointerEntryError, _read_pointer

        try:
            resolved = _read_pointer(state, state / "snapshots")
        except (GoodnotesError, OSError) as exc:
            if self.cache_dir is not None and not isinstance(exc, PointerEntryError):
                # `read_view` owns the shared lock and adds its established
                # operation/path context around pointer and snapshot failures.
                raise
            if _permission_denied(exc):
                raise CacheAccessError.from_exception(
                    exc,
                    f"Could not read the active library pointer: {pointer}",
                    operation="read_active_pointer",
                    path=pointer,
                    cache_dir=pointer.parent,
                    missing_is_uninitialized=True,
                ) from exc
            raise invalid_pointer() from exc
        if resolved is None:
            missing = FileNotFoundError(2, "Active library pointer is missing", str(pointer))
            if self.cache_dir is not None:
                raise GoodnotesError(
                    f"The active library pointer is missing: {pointer}"
                ) from missing
            raise CacheAccessError.from_exception(
                missing,
                f"Could not read the active library pointer: {pointer}",
                operation="read_active_pointer",
                path=pointer,
                cache_dir=pointer.parent,
                missing_is_uninitialized=True,
            ) from missing
        _, path = resolved
        return Sandbox.load(path)
