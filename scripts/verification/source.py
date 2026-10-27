"""Git and content provenance for focused verification runs."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]

_IGNORED_PARTS = {
    ".cache",
    ".git",
    ".hypothesis",
    ".local",
    ".mypy_cache",
    ".private-corpus",
    ".pytest_cache",
    ".ruff_cache",
    ".ty_cache",
    ".venv",
    "__pycache__",
    "caches",
    "privatecorpus",
    "private-corpus",
    "private_corpus",
}

_UNTRACKED_SUFFIXES = {".cfg", ".ini", ".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}

_UNTRACKED_NAMES = {".gitignore", "Makefile", "justfile"}

_HEX_SHA = re.compile(r"\A[0-9a-f]{40,64}\Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _run_git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=False, timeout=15
    )
    if result.returncode:
        raise RuntimeError("git_unavailable")
    return result.stdout


def _hash_path(hasher: Any, root: Path, relative: bytes, marker: bytes) -> None:
    name_parts = relative.split(b"/")
    if any(os.fsdecode(part) in _IGNORED_PARTS for part in name_parts):
        return
    path = root / os.fsdecode(relative)
    hasher.update(len(relative).to_bytes(8, "big"))
    hasher.update(relative)
    hasher.update(marker)
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            hasher.update(b"L")
            hasher.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            hasher.update(b"F")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
        else:
            hasher.update(b"O")
    except FileNotFoundError:
        hasher.update(b"MISSING")
    except OSError as exc:
        raise RuntimeError("source_unreadable") from exc


def _source_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    commit = _run_git(root, "rev-parse", "--verify", "HEAD").decode("ascii", "strict").strip()
    if not _HEX_SHA.fullmatch(commit):
        raise RuntimeError("git_revision_unavailable")
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    tracked = _run_git(root, "ls-files", "--cached", "-z")
    tracked_entries = tracked.split(b"\0")
    tracked_paths = set(tracked_entries) - {b""}
    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    hasher = hashlib.sha256()
    for relative in sorted(set(tracked_entries + untracked.split(b"\0")) - {b""}):
        is_tracked = relative in tracked_paths
        if not is_tracked:
            basename = os.fsdecode(relative.rsplit(b"/", 1)[-1])
            if (
                Path(basename).suffix.lower() not in _UNTRACKED_SUFFIXES
                and basename not in _UNTRACKED_NAMES
            ):
                continue
        _hash_path(hasher, root, relative, b"T" if is_tracked else b"U")
    return {
        "git_commit": commit,
        "dirty": bool(status.strip()),
        "status_fingerprint": hashlib.sha256(status).hexdigest(),
        "content_fingerprint": hasher.hexdigest(),
    }
