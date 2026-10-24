"""Resolve the active local read view without touching the Goodnotes application."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .errors import GoodnotesError
from .safety import Sandbox


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

    def load_sandbox(self) -> Sandbox:
        if self.sandbox is not None:
            return Sandbox.load(self.sandbox)
        pointer = (self.data_dir or default_data_dir()) / "active.json"
        try:
            data = json.loads(pointer.read_text())
            path = data["snapshot"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise GoodnotesError("No local library. Run 'goodnotes sync' first.") from exc
        if not isinstance(path, str):
            raise GoodnotesError("Invalid active library pointer. Run 'goodnotes sync' again.")
        return Sandbox.load(path)
