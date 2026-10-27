"""Bounded reads of regular snapshot and cache metadata."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from .paths import stat_key

# Snapshot markers contain a file inventory and can be substantially larger than pointers.
SNAPSHOT_MARKER_LIMIT = 64 * 1024 * 1024
CACHE_METADATA_LIMIT = 1024 * 1024


def read_json(path: Path, *, limit: int) -> Any:
    """Read one stable, regular file without following the final component."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_size > limit:
        raise ValueError("Metadata is not a bounded regular file.")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if not stat.S_ISREG(opened.st_mode) or stat_key(before) != stat_key(opened):
            raise ValueError("Metadata changed while opening.")
        with os.fdopen(fd, "rb") as stream:
            fd = -1
            raw = stream.read(limit + 1)
            after_fd = os.fstat(stream.fileno())
        after_path = path.lstat()
        if (
            len(raw) > limit
            or stat_key(opened) != stat_key(after_fd)
            or stat_key(opened) != stat_key(after_path)
        ):
            raise ValueError("Metadata changed while reading or exceeds its limit.")
        return json.loads(raw)
    finally:
        if fd >= 0:
            os.close(fd)
