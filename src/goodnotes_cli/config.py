"""Resolve the active local read view without touching the Goodnotes application."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CacheAccessError, GoodnotesError, _permission_denied
from .safety import Sandbox

if TYPE_CHECKING:
    from .output import Projection


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
        from .cache import _read_pointer

        try:
            resolved = _read_pointer(state, state / "snapshots")
        except (GoodnotesError, OSError) as exc:
            message = str(exc).casefold()
            pointer_entry_error = "active library pointer" in message and "snapshot" not in message
            pointer_read_denied = _permission_denied(exc) and "active library pointer" in message
            if self.cache_dir is not None and not (pointer_entry_error or pointer_read_denied):
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
