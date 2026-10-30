"""Native preparation errors stay native at every visual command boundary."""

from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.native import store as native
from tests.support.cli import _tree_manifest, invoke_in_process
from tests.support.library import _create_snapshot


@pytest.mark.parametrize("verbose", [False, True])
@pytest.mark.parametrize(
    "command", ["render", "render-output", "bundle", "bundle-output", "export"]
)
@pytest.mark.parametrize("phase", ["CURRENT", "OPTIONS-000001", "MANIFEST-000001", "inventory"])
def test_native_preparation_failure_is_json_and_not_an_output_write_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    verbose: bool,
    command: str,
    phase: str,
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    source = sandbox.library_dir / "Databases" / "notes_main.db"
    source.mkdir()
    (source / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (source / "MANIFEST-000001").write_bytes(b"generated manifest")
    (source / "OPTIONS-000001").write_bytes(b"comparator=leveldb.BytewiseComparator\n")
    before = _tree_manifest(sandbox.root)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    target = source if phase == "inventory" else source / phase
    failure = PermissionError(errno.EACCES, "generated native read denied", str(target))
    original_open, original_iterdir = os.open, Path.iterdir

    def open_file(path: Any, *args: Any, **kwargs: Any) -> int:
        if path == target:
            raise failure
        return original_open(path, *args, **kwargs)

    def inventory(path: Path) -> Any:
        if phase == "inventory" and path == source:
            raise failure
        return original_iterdir(path)

    monkeypatch.setattr(native.os, "open", open_file)
    monkeypatch.setattr(Path, "iterdir", inventory)
    monkeypatch.setattr(native.tempfile, "gettempdir", lambda: str(scratch))
    monkeypatch.setattr(
        native, "NativeRuntime", lambda: pytest.fail("failure reached native engine")
    )
    output = tmp_path / "output" / ("export.pdf" if command == "export" else "result")
    if command.startswith("render"):
        arguments = ["render", "doc-hostile", "--page", "3"]
        if command == "render-output":
            output = output.with_suffix(".png")
            arguments += ["--output", str(output)]
    elif command.startswith("bundle"):
        arguments = ["bundle", "doc-hostile", "--pages", "3"]
        if command == "bundle-output":
            arguments += ["--output", str(output)]
    else:
        arguments = ["export", "doc-hostile", "--pages", "3", "--output", str(output)]
    result = invoke_in_process(arguments, tmp_path, sandbox=sandbox.root, verbose=verbose)
    assert result.returncode == 1, result.stderr
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "native_data_unavailable"
    assert error["details"]["operation"] == (
        "clone_native_store" if phase == "MANIFEST-000001" else "inspect_native_store"
    )
    assert error["details"]["path"] == str(target)
    assert "copied native store" in error["details"]["remedy"]
    assert "Traceback" not in result.stderr
    assert not output.exists()
    assert not list(scratch.iterdir())
    assert not list(tmp_path.rglob(".goodnotes-bundle-*"))
    assert _tree_manifest(sandbox.root) == before
