"""Small test helpers shared by subprocess and in-process CLI checks."""

from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from goodnotes_cli.cli import app


def invoke_in_process(
    arguments: list[str],
    tmp_path: Path,
    *,
    sandbox: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run ordinary parser/schema checks without starting a Python subprocess."""
    environment = {
        "GOODNOTES_DATA_DIR": str(data_dir or tmp_path / "state"),
        "GOODNOTES_SANDBOX": "",
    }
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
