from __future__ import annotations

import errno
import json
import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import FilesystemAccessError, GoodnotesError, _permission_denied
from goodnotes_cli.library import Library
from goodnotes_cli.storage.artifacts import ArtifactCache, artifact_key
from goodnotes_cli.storage.artifacts.filesystem import ArtifactFilesystem
from goodnotes_cli.storage.sandbox import Sandbox
from tests.support.cli import _tree_manifest
from tests.support.library import _create_snapshot
from tests.support.snapshot import make_snapshot


def _with_source(sandbox: Sandbox, source: object) -> Sandbox:
    marker = sandbox.root / "goodnotes-snapshot.json"
    marker.write_text(json.dumps({"format_version": 1, "source_path": source}), encoding="utf-8")
    return Sandbox.load(sandbox.root)


def test_output_policy_construction_does_not_resolve_or_inspect_recorded_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "unavailable-source"
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Write-policy construction inspected the filesystem")

    with monkeypatch.context() as fault:
        fault.setattr(Path, "resolve", forbidden)
        fault.setattr(Path, "stat", forbidden)
        policy = sandbox.output_protection()

    assert policy.snapshot_root == sandbox.root
    assert policy.source_root == source
    assert not source.exists()


@pytest.mark.parametrize("location", ["equal", "descendant", "ancestor", "alias"])
def test_explicit_output_rejects_captured_source_overlap_before_mutation(
    tmp_path: Path, location: str
) -> None:
    source = tmp_path / "source-parent" / "source"
    source.mkdir(parents=True)
    (source / "generated.txt").write_bytes(b"generated source bytes")
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))
    if location == "equal":
        output = source
    elif location == "ancestor":
        output = source.parent
    elif location == "alias":
        alias = tmp_path / "alias"
        alias.symlink_to(source, target_is_directory=True)
        output = alias / "missing" / "result.png"
    else:
        output = source / "missing" / "result.png"
    before = _tree_manifest(source)

    with pytest.raises(GoodnotesError, match="source"):
        sandbox.ensure_output(output)

    assert _tree_manifest(source) == before
    assert not (source / "missing").exists()


@pytest.mark.parametrize("source_state", ["present", "missing", "not-directory", "spaces"])
def test_captured_source_keeps_lexical_exclusion_and_allows_unrelated_outputs(
    tmp_path: Path, source_state: str
) -> None:
    source = tmp_path / (" source " if source_state == "spaces" else "source")
    if source_state in {"present", "spaces"}:
        source.mkdir()
    elif source_state == "not-directory":
        source.write_bytes(b"generated regular file")
        source = source / "absent"
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))

    with pytest.raises(GoodnotesError, match="source"):
        sandbox.ensure_output(source / "missing" / "result.png")

    for output in (tmp_path / "source-other" / "result.png", tmp_path / "other" / "result.png"):
        assert sandbox.ensure_output(output) == output.resolve()
        assert not output.parent.exists()


def test_source_case_alias_uses_current_filesystem_identity(tmp_path: Path) -> None:
    source = tmp_path / "GeneratedSource"
    source.mkdir()
    alias = source.with_name("generatedsource")
    try:
        same = alias.samefile(source)
    except FileNotFoundError:
        same = False
    if not same:
        pytest.skip("The generated source has no case alias on this volume")
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))
    before = _tree_manifest(source)

    with pytest.raises(GoodnotesError, match="source"):
        sandbox.ensure_output(alias / "missing" / "result.png")

    assert _tree_manifest(source) == before


@pytest.mark.parametrize(
    "source", [None, False, 7, [], {}, "", "relative/source", "~/source", "/bad\x00source"]
)
def test_malformed_present_provenance_preserves_reads_but_denies_output_and_artifacts(
    tmp_path: Path, source: object
) -> None:
    sandbox = _with_source(_create_snapshot(tmp_path / "snapshot"), source)
    before = _tree_manifest(sandbox.root)
    with Library(sandbox) as library:
        assert library.list_items("/")
        assert library.read_page("doc-hostile", 1).parts[0].text == "Straße | Strasse"
    output = tmp_path / "new-output" / "result.png"
    artifact_root = tmp_path / "new-state" / "artifacts"

    with pytest.raises(GoodnotesError, match="source"):
        sandbox.ensure_output(output)
    with pytest.raises(GoodnotesError, match="source"):
        with ArtifactCache(artifact_root, sandbox):
            pytest.fail("Malformed provenance authorized artifact storage")

    assert not output.parent.exists()
    assert not artifact_root.parent.exists()
    assert _tree_manifest(sandbox.root) == before


def test_missing_historical_provenance_preserves_existing_write_behavior(tmp_path: Path) -> None:
    sandbox = make_snapshot(tmp_path / "snapshot")
    output = tmp_path / "outputs" / "result.png"
    assert "source_path" not in sandbox.marker_data
    assert sandbox.ensure_output(output) == output.resolve()
    with ArtifactCache(tmp_path / "artifacts", sandbox) as cache:
        with cache.staging() as stage:
            (stage / "generated.json").write_text("{}", encoding="utf-8")
    assert not output.parent.exists()


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
@pytest.mark.parametrize("owner", ["output", "artifacts"])
def test_source_identity_permission_denial_is_an_expected_failure_without_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int, owner: str
) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))
    destination = tmp_path / "untouched" / "result"
    before = _tree_manifest(source)
    original = Path.stat

    def denied(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == source:
            raise PermissionError(error_number, "generated source identity denial", str(path))
        return original(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "stat", denied)
        with pytest.raises(GoodnotesError) as captured:
            if owner == "output":
                sandbox.ensure_output(destination)
            else:
                with ArtifactCache(destination, sandbox):
                    pytest.fail("Source permission denial authorized artifact storage")
    assert _permission_denied(captured.value)
    if owner == "output":
        assert isinstance(captured.value, FilesystemAccessError)
        assert captured.value.code == "output_permission_denied"
        assert captured.value.details["operation"] == "validate_output"
    assert not destination.parent.exists()
    assert _tree_manifest(source) == before


@pytest.mark.parametrize("owner", ["output", "artifacts"])
def test_source_symlink_loop_is_an_expected_failure_before_mutation(
    tmp_path: Path, owner: str
) -> None:
    source = tmp_path / "source-loop"
    source.symlink_to(source)
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))
    destination = tmp_path / "untouched" / "result"

    with pytest.raises(GoodnotesError):
        if owner == "output":
            sandbox.ensure_output(destination)
        else:
            with ArtifactCache(destination, sandbox):
                pytest.fail("A source loop authorized artifact storage")

    assert source.is_symlink()
    assert not destination.parent.exists()


@pytest.mark.parametrize("location", ["equal", "descendant", "ancestor"])
@pytest.mark.parametrize("warm", [False, True])
def test_artifact_root_source_admission_precedes_acquisition_and_maintenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, warm: bool
) -> None:
    source = tmp_path / "owned-parent" / "source"
    snapshot = make_snapshot(tmp_path / "snapshot")
    if location == "equal":
        root = source
    elif location == "ancestor":
        root = source.parent
    else:
        root = source / "new-parent" / "artifacts"
    if warm:
        key = artifact_key({"generated": location})
        with ArtifactCache(root, snapshot.root, clock=lambda: 1, grace_seconds=0) as cache:
            with cache.staging() as stage:
                (stage / "generated.json").write_text("{}", encoding="utf-8")
                cache.publish(key, stage, {"generated": True})
    sandbox = _with_source(snapshot, str(source))
    before = _tree_manifest(tmp_path)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Forbidden artifact root was opened or maintained")

    monkeypatch.setattr(ArtifactFilesystem, "_open_directory_tree", forbidden)
    monkeypatch.setattr(ArtifactCache, "_maintain", forbidden)
    with pytest.raises(GoodnotesError, match="source"):
        with ArtifactCache(root, sandbox, clock=lambda: 100_000, ttl_seconds=0):
            pytest.fail("Source overlap authorized artifact storage")

    assert _tree_manifest(tmp_path) == before


def test_artifact_opened_root_rechecks_source_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), str(source))
    root = tmp_path / "artifacts"
    original = ArtifactFilesystem._open_directory_tree

    def acquire_and_alias(path: Path) -> int:
        descriptor = original(path)
        source.rmdir()
        source.symlink_to(root, target_is_directory=True)
        return descriptor

    monkeypatch.setattr(ArtifactFilesystem, "_open_directory_tree", staticmethod(acquire_and_alias))
    with pytest.raises(GoodnotesError, match="source"):
        with ArtifactCache(root, sandbox):
            pytest.fail("Opened artifact root bypassed the source recheck")

    assert root.is_dir()
    assert list(root.iterdir()) == []


@pytest.mark.parametrize("path_form", ["path", "string"])
def test_artifact_bare_path_compatibility_does_not_load_source_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path_form: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    sandbox = _with_source(make_snapshot(tmp_path / "snapshot"), None)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Bare snapshot path attempted to load a marker or database")

    monkeypatch.setattr(Sandbox, "load", forbidden)
    monkeypatch.setattr(Sandbox, "output_protection", forbidden)
    argument = str(sandbox.root) if path_form == "string" else sandbox.root
    with ArtifactCache(source / "artifacts", argument) as cache:
        with cache.staging() as stage:
            (stage / "generated.json").write_text("{}", encoding="utf-8")
    with pytest.raises(GoodnotesError, match="snapshot"):
        with ArtifactCache(sandbox.root / "artifacts", argument):
            pytest.fail("Bare-path compatibility lost snapshot protection")
