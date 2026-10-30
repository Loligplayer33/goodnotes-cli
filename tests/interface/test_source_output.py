"""CLI write handoffs preserve generated sources and their captured snapshots."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from tests.support.cli import _tree_manifest, invoke, successful
from tests.support.library import ROOT_ID, _create_snapshot


@pytest.fixture(autouse=True)
def isolated_goodnotes_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in tuple(os.environ):
        if name.startswith("GOODNOTES_"):
            monkeypatch.delenv(name)


def _sync_generated_source(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = _create_snapshot(tmp_path / "generated-source").library_dir
    before = _tree_manifest(source)
    data_dir = tmp_path / "state"
    response = successful(invoke(["sync", "--library", str(source)], tmp_path, data_dir=data_dir))
    snapshot = Path(response["snapshot"])
    marker = json.loads((snapshot / "goodnotes-snapshot.json").read_text(encoding="utf-8"))
    assert marker["source_path"] == str(source.resolve())
    assert _tree_manifest(source) == before
    return source, snapshot, data_dir


def _visual_arguments(command: str, output: Path | None = None) -> list[str]:
    selection = "--page" if command == "render" else "--pages"
    arguments = [command, "doc-hostile", selection, "3", "--layer", "background"]
    if output is not None:
        arguments.extend(["--output", str(output)])
    return arguments


def _assert_source_rejected(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 1, (result.stdout, result.stderr)
    assert result.stdout == ""
    assert len(result.stderr.splitlines()) == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "operation_failed"
    assert "source" in error["message"].lower()


@pytest.mark.parametrize("command", ["render", "export", "bundle"])
@pytest.mark.parametrize("selection", ["managed", "explicit", "explicit-alias"])
def test_synced_custom_source_rejects_explicit_visual_output_before_mutation(
    tmp_path: Path, command: str, selection: str
) -> None:
    source, snapshot, data_dir = _sync_generated_source(tmp_path)
    destination_root = source
    if selection == "explicit-alias":
        destination_root = tmp_path / "source-alias"
        destination_root.symlink_to(source, target_is_directory=True)
    suffix = {"render": ".png", "export": ".pdf", "bundle": ""}[command]
    parent = destination_root / "forbidden-parent"
    output = parent / f"result{suffix}"
    source_before, snapshot_before = _tree_manifest(source), _tree_manifest(snapshot)

    result = invoke(
        _visual_arguments(command, output),
        tmp_path,
        sandbox=None if selection == "managed" else snapshot,
        data_dir=data_dir,
        verbose=False,
    )

    _assert_source_rejected(result)
    assert not parent.exists()
    assert _tree_manifest(source) == source_before
    assert _tree_manifest(snapshot) == snapshot_before


@pytest.mark.parametrize("command", ["render", "bundle"])
@pytest.mark.parametrize("warm", [False, True], ids=["cold", "warm"])
def test_managed_visual_output_rejects_source_data_dir_before_creation_or_maintenance(
    tmp_path: Path, command: str, warm: bool
) -> None:
    source, managed_snapshot, _ = _sync_generated_source(tmp_path)
    snapshot = tmp_path / "selected-snapshot"
    shutil.copytree(managed_snapshot, snapshot)
    data_dir = source / "forbidden-state"
    arguments = _visual_arguments(command)
    if warm:
        prepared_state = tmp_path / "prepared-state"
        response = successful(
            invoke(arguments, tmp_path, sandbox=snapshot, data_dir=prepared_state, verbose=False)
        )
        returned = Path(response["path"] if command == "render" else response["manifest"])
        assert returned.is_file()
        assert (prepared_state / "artifacts" / ".artifacts.lock").is_file()
        shutil.move(prepared_state, data_dir)
        assert (data_dir / returned.relative_to(prepared_state)).is_file()
    source_before, snapshot_before = _tree_manifest(source), _tree_manifest(snapshot)

    result = invoke(arguments, tmp_path, sandbox=snapshot, data_dir=data_dir, verbose=False)

    _assert_source_rejected(result)
    if not warm:
        assert not data_dir.exists()
    assert _tree_manifest(source) == source_before
    assert _tree_manifest(snapshot) == snapshot_before


@pytest.mark.parametrize("source_path", [None, "relative/source"], ids=["null", "relative"])
@pytest.mark.parametrize("managed", [False, True], ids=["explicit-output", "managed-output"])
def test_malformed_source_provenance_keeps_cli_reads_but_denies_visual_writes(
    tmp_path: Path, source_path: object, managed: bool
) -> None:
    snapshot = _create_snapshot(tmp_path / "generated-snapshot").root
    marker = snapshot / "goodnotes-snapshot.json"
    marker.write_text(
        json.dumps({"format_version": 1, "source_path": source_path}), encoding="utf-8"
    )
    output = tmp_path / "forbidden-parent" / "page.png"
    data_dir = tmp_path / "forbidden-state"
    before = _tree_manifest(snapshot)
    for arguments in (
        ["ls", "--limit", "1"],
        ["read", "doc-hostile", "--page", "1", "--source", "handwriting"],
    ):
        successful(invoke(arguments, tmp_path, sandbox=snapshot, data_dir=data_dir, verbose=False))

    result = invoke(
        _visual_arguments("render", None if managed else output),
        tmp_path,
        sandbox=snapshot,
        data_dir=data_dir,
        verbose=False,
    )

    _assert_source_rejected(result)
    assert not output.parent.exists()
    assert not data_dir.exists()
    assert _tree_manifest(snapshot) == before


@pytest.mark.parametrize("managed", [False, True], ids=["explicit-output", "managed-output"])
def test_historical_snapshot_without_source_provenance_keeps_cli_output_compatible(
    tmp_path: Path, managed: bool
) -> None:
    sandbox = _create_snapshot(tmp_path / "historical-snapshot")
    assert "source_path" not in sandbox.marker_data
    output = tmp_path / "allowed-output" / "page.png"
    before = _tree_manifest(sandbox.root)

    response = successful(
        invoke(
            _visual_arguments("render", None if managed else output),
            tmp_path,
            sandbox=sandbox.root,
            verbose=False,
        )
    )

    assert Path(response["path"]).is_file()
    assert _tree_manifest(sandbox.root) == before


def test_virtual_library_root_does_not_become_a_physical_output_deny_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, snapshot, data_dir = _sync_generated_source(tmp_path)
    output = tmp_path / ROOT_ID / "page.png"
    source_before, snapshot_before = _tree_manifest(source), _tree_manifest(snapshot)
    monkeypatch.chdir(tmp_path)

    response = successful(
        invoke(
            ["--library-root", ROOT_ID, *_visual_arguments("render", output)],
            tmp_path,
            sandbox=snapshot,
            data_dir=data_dir,
            verbose=False,
        )
    )

    assert Path(response["path"]) == output
    assert output.is_file()
    assert _tree_manifest(source) == source_before
    assert _tree_manifest(snapshot) == snapshot_before
