"""A cache-local library selection is explicit, bounded, and read-only."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from goodnotes_cli.config import Settings
from goodnotes_cli.errors import CacheAccessError
from goodnotes_cli.storage.cache.lifecycle import refresh_cache
from tests.support.cli import _tree_manifest, invoke, successful
from tests.support.library import _create_snapshot


def test_cache_selection_precedence_and_snapshot_isolation(tmp_path: Path) -> None:
    selection = tmp_path / "library-root.json"
    selection.write_text(json.dumps({"root_folder_id": "personal-root"}))
    before = selection.stat()
    assert Settings(cache_dir=tmp_path).load_library_root() == "personal-root"
    assert selection.stat().st_mtime_ns == before.st_mtime_ns
    assert (
        Settings(cache_dir=tmp_path, library_root="other-root").load_library_root() == "other-root"
    )
    assert Settings(cache_dir=tmp_path, library_root="all").load_library_root() is None
    assert Settings(cache_dir=tmp_path, sandbox=tmp_path / "copy").load_library_root() is None
    assert Settings(cache_dir=tmp_path / "missing").load_library_root() is None


@pytest.mark.parametrize(
    "data", ["{", "[]", "{}", '{"root_folder_id": ""}', '{"root_folder_id": null}', "x" * 4097]
)
def test_invalid_selection_never_silently_restores_the_combined_library(
    tmp_path: Path, data: str
) -> None:
    (tmp_path / "library-root.json").write_text(data)
    with pytest.raises(CacheAccessError) as error:
        Settings(cache_dir=tmp_path).load_library_root()
    assert "--library-root all" in error.value.details["remedy"]


def test_excessively_nested_selection_has_a_public_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GOODNOTES_LIBRARY_ROOT", raising=False)
    source = _create_snapshot(tmp_path / "source").root / "Library"
    state = tmp_path / "state"
    captured = refresh_cache(source, state)
    snapshot_path = captured["snapshot"]
    assert isinstance(snapshot_path, str)
    snapshot = Path(snapshot_path)
    selection = state / "library-root.json"
    selection.write_text('{"root_folder_id":' + "[" * 1200 + "0" + "]" * 1200 + "}")
    assert selection.stat().st_size == 2420
    before_source = _tree_manifest(source)
    before_snapshot = _tree_manifest(snapshot)
    before_selection = selection.read_bytes()

    result = invoke(["ls"], tmp_path, data_dir=state)

    assert result.returncode == 1
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cache_invalid"
    assert error["details"]["operation"] == "read_library_selection"
    assert error["details"]["path"] == str(selection)
    assert "--library-root all" in error["details"]["remedy"]
    assert "Traceback" not in result.stderr
    assert successful(invoke(["--library-root", "all", "ls"], tmp_path, data_dir=state))["items"]
    assert successful(invoke(["ls"], tmp_path, sandbox=snapshot, data_dir=state))["items"]
    assert _tree_manifest(source) == before_source
    assert _tree_manifest(snapshot) == before_snapshot
    assert selection.read_bytes() == before_selection


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_special_selection_files_fail_without_hanging_or_following_links(
    tmp_path: Path, kind: str
) -> None:
    selection = tmp_path / "library-root.json"
    target = tmp_path / "target.json"
    target.write_text('{"root_folder_id":"personal-root"}')
    if kind == "symlink":
        selection.symlink_to(target)
    elif kind == "directory":
        selection.mkdir()
    else:
        os.mkfifo(selection)
    script = """
from pathlib import Path
from goodnotes_cli.config import Settings
from goodnotes_cli.errors import GoodnotesError
import sys
try:
    Settings(cache_dir=Path(sys.argv[1])).load_library_root()
except GoodnotesError:
    sys.exit(0)
sys.exit(1)
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stderr
    assert target.read_text() == '{"root_folder_id":"personal-root"}'
