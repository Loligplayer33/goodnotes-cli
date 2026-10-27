"""Capture inventory records and retry signals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ...errors import GoodnotesError

_DATABASE_NAMES = ("projection.sqlite", "fts.sqlite")
_NATIVE_STORE_NAMES = ("notes_main.db",)
_DATABASE_SIDECAR_SUFFIXES = ("-wal", "-shm", "-journal")


class _SourceChanged(Exception):
    """The source changed while it was being inspected or copied."""


class _InventoryChanged(_SourceChanged, GoodnotesError):
    """A directory inventory changed during one metadata scan."""


@dataclass(frozen=True)
class _Entry:
    relative: Path
    is_directory: bool
    stat_key: tuple[int, int, int, int, int, int]
    size: int = 0
    sha256: str | None = None
    copy_from: Path | None = None
    reused: bool = False
    source_digest_reused: bool = False
    projection_validated: bool = False
