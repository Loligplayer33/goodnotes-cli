"""Shared CLI helpers for generated tests."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

from typer.testing import CliRunner

from goodnotes_cli.cli import app
from scripts.verification.environment import isolated_environment


def invoke_in_process(
    arguments: list[str],
    tmp_path: Path,
    *,
    sandbox: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ordinary parser/schema checks without starting a Python subprocess."""
    child_environment = isolated_environment(
        {"GOODNOTES_DATA_DIR": str(data_dir or tmp_path / "state")}
    )
    environment: dict[str, str | None] = {
        key: None for key in os.environ if key not in child_environment
    }
    environment.update(child_environment)
    prefix = ["--sandbox", str(sandbox)] if sandbox is not None else []
    if verbose:
        prefix.append("--verbose")
    result = CliRunner().invoke(app, [*prefix, *arguments], env=environment)
    return subprocess.CompletedProcess(
        ["goodnotes", *prefix, *arguments],
        result.exit_code,
        result.stdout,
        result.stderr or "",
    )


def invoke(
    arguments: list[str],
    tmp_path: Path,
    *,
    sandbox: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Exercise the full response contract unless a test requests concise defaults."""
    environment = isolated_environment({"GOODNOTES_DATA_DIR": str(data_dir or tmp_path / "state")})
    prefix = ["--sandbox", str(sandbox)] if sandbox is not None else []
    if verbose:
        prefix.append("--verbose")
    return subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", *prefix, *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def successful(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


def _tree_manifest(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        info = path.lstat()
        is_file = stat.S_ISREG(info.st_mode)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if is_file else None
        entries.append(
            (
                path.relative_to(root).as_posix() if path != root else ".",
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                digest,
            )
        )
    return tuple(entries)
