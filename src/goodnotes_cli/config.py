"""Resolve the active local read view without touching the Goodnotes application."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import CacheAccessError
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
        pointer = (self.data_dir or default_data_dir()) / "active.json"

        def invalid_pointer() -> CacheAccessError:
            return CacheAccessError(
                "cache_invalid",
                f"The active library pointer is invalid: {pointer}",
                operation="read_active_pointer",
                path=pointer,
                cache_dir=pointer.parent,
            )

        try:
            info = pointer.lstat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("not a regular file")
            contents = pointer.read_text(encoding="utf-8")
        except OSError as exc:
            raise CacheAccessError.from_exception(
                exc,
                f"Could not read the active library pointer: {pointer}",
                operation="read_active_pointer",
                path=pointer,
                cache_dir=pointer.parent,
                missing_is_uninitialized=True,
            ) from exc
        except ValueError as exc:
            raise invalid_pointer() from exc
        try:
            data = json.loads(contents)
        except ValueError as exc:
            raise invalid_pointer() from exc
        if not isinstance(data, dict) or not isinstance(data.get("snapshot"), str):
            raise invalid_pointer()
        path = data["snapshot"]
        return Sandbox.load(path)
