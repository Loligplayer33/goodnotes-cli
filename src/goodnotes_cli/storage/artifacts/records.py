from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from goodnotes_cli.errors import GoodnotesError

_ROOT_MARKER = ".goodnotes-artifacts.json"
_LOCK_NAME = ".artifacts.lock"
_ENTRY_MARKER = ".artifact.json"
_STAGING_MARKER = ".artifact-staging.json"
_ROOT_OWNER = {"format_version": 1, "owner": "goodnotes-cli-artifacts"}
_OWNER_UID = os.getuid() if hasattr(os, "getuid") else None
_KEY = re.compile(r"^[0-9a-f]{64}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_STAGE_NAME = re.compile(r"^\.staging-([0-9a-f]{32})$")
_PUBLISH_NAME = re.compile(r"^\.publishing-([0-9a-f]{32})$")
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_COPY_CHUNK = 1024 * 1024


class ArtifactCapacityError(GoodnotesError):
    """A regenerable artifact cannot fit without evicting a protected entry."""

    code = "artifact_cache_capacity"

    def __init__(self, *, required_bytes: int, max_bytes: int, cache_dir: Path) -> None:
        super().__init__(
            "The generated artifact could not fit without removing a protected cache entry. "
            f"The projected use is {required_bytes} bytes; the cache limit is {max_bytes} bytes."
        )
        self.details = {
            "operation": "publish_artifact",
            "path": str(cache_dir),
            "required_bytes": required_bytes,
            "max_bytes": max_bytes,
            "remedy": (
                "Use --output to save a persistent result outside the bounded artifact cache, "
                "or retry after the five-minute protected period."
            ),
        }


@dataclass(frozen=True)
class CachedArtifact:
    """A validated immutable cache entry."""

    directory: Path
    result: dict[str, Any]


@dataclass(frozen=True)
class _FileRecord:
    name: str
    size: int
    kind: str
    digest: str | None = None


@dataclass(frozen=True)
class _EntryRecord:
    key: str
    directory: Path
    result: dict[str, Any]
    files: tuple[_FileRecord, ...]
    marker_size: int
    last_used: float
    directory_identity: tuple[int, int]


@dataclass(frozen=True)
class _PreparedFile:
    name: str
    path: Path
    size: int
    kind: str
    digest: str | None = None
    source_identity: tuple[int, int, int, int, int, int] | None = None


def artifact_key(value: Any) -> str:
    """Hash a JSON value using stable, compact canonical JSON encoding."""

    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise GoodnotesError("Artifact cache keys must be JSON-serializable values.") from exc
    return hashlib.sha256(encoded).hexdigest()
