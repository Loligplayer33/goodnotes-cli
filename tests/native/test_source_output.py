from __future__ import annotations

import errno
import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import _permission_denied
from goodnotes_cli.native import NativeError, NativeStoreSession
from goodnotes_cli.native import store as native
from goodnotes_cli.storage.sandbox import Sandbox
from tests.native.test_native import FakeNativeRuntime
from tests.support.cli import _tree_manifest
from tests.support.snapshot import make_snapshot


def _native_snapshot(root: Path, source: object = ...) -> Sandbox:
    sandbox = make_snapshot(root)
    store = sandbox.library_dir / "Databases" / "notes_main.db"
    store.mkdir()
    (store / "CURRENT").write_bytes(b"MANIFEST-000001\n")
    (store / "MANIFEST-000001").write_bytes(b"generated native manifest")
    if source is not ...:
        (root / "goodnotes-snapshot.json").write_text(
            json.dumps({"format_version": 1, "source_path": source}), encoding="utf-8"
        )
    return Sandbox.load(root)


@pytest.mark.parametrize("location", ["equal", "descendant", "alias"])
def test_source_scratch_parent_is_rejected_before_allocation_copy_or_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "generated.txt").write_bytes(b"generated source")
    sandbox = _native_snapshot(tmp_path / "snapshot", str(source))
    parent = source
    if location == "descendant":
        parent = source / "scratch"
        parent.mkdir()
    elif location == "alias":
        parent = tmp_path / "source-alias"
        parent.symlink_to(source, target_is_directory=True)
    before = _tree_manifest(source), _tree_manifest(sandbox.root)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Protected scratch reached allocation, copy, or runtime")

    monkeypatch.setattr(native.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(native, "_clone_file_from_fd", forbidden)
    monkeypatch.setattr(native, "NativeRuntime", forbidden)

    with pytest.raises(NativeError) as captured:
        with NativeStoreSession(sandbox, scratch_dir=parent):
            pytest.fail("Protected scratch parent was admitted")

    assert captured.value.code == "native_data_unavailable"
    assert captured.value.details["operation"] == "create_native_scratch"
    assert (_tree_manifest(source), _tree_manifest(sandbox.root)) == before


@pytest.mark.parametrize("recorded", [False, True])
def test_common_ancestor_scratch_parent_remains_usable_and_cleanup_is_complete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recorded: bool
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "generated.txt").write_bytes(b"generated source")
    sandbox = _native_snapshot(tmp_path / "snapshot", str(source) if recorded else ...)
    runtime = FakeNativeRuntime()
    monkeypatch.setattr(native, "NativeRuntime", lambda: runtime)
    before = _tree_manifest(source), _tree_manifest(sandbox.root)

    with NativeStoreSession(sandbox, scratch_dir=tmp_path) as session:
        assert session.read_page_records("PAGE-A") == [
            (b"PAGE-A.1", b"first"),
            (b"PAGE-A.2", b"second"),
        ]
        scratch = runtime.open_paths[0]
        assert scratch.is_relative_to(tmp_path)
        assert not scratch.is_relative_to(source)
        assert not scratch.is_relative_to(sandbox.root)
        assert (scratch / "fake-engine-write").is_file()

    assert not scratch.parent.exists()
    assert runtime.closed == [2]
    assert runtime.destroyed_iterators == [4]
    assert (_tree_manifest(source), _tree_manifest(sandbox.root)) == before


@pytest.mark.parametrize("protected_child", ["root", "descendant"])
def test_actual_scratch_child_is_admitted_before_store_creation_copy_or_engine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protected_child: str
) -> None:
    child = tmp_path / "allocated-native-child"
    source = child if protected_child == "root" else child / "missing" / "source"
    sandbox = _native_snapshot(tmp_path / "snapshot", str(source))
    before = _tree_manifest(sandbox.root)
    events: list[str] = []
    cleanup_entries: list[str] = []

    class AllocatedScratch:
        name = str(child)

        def __init__(self, *, prefix: str, dir: Path) -> None:
            assert prefix == "goodnotes-native-"
            assert dir == tmp_path
            child.mkdir(mode=0o700)
            events.append("allocated")

        def cleanup(self) -> None:
            events.append("cleanup")
            cleanup_entries.extend(path.name for path in child.iterdir())
            shutil.rmtree(child)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Unvalidated actual scratch child reached copying or runtime")

    monkeypatch.setattr(native.tempfile, "TemporaryDirectory", AllocatedScratch)
    monkeypatch.setattr(native, "_clone_file_from_fd", forbidden)
    monkeypatch.setattr(native, "NativeRuntime", forbidden)

    with pytest.raises(NativeError) as captured:
        with NativeStoreSession(sandbox, scratch_dir=tmp_path):
            pytest.fail("Protected actual scratch child was admitted")

    assert captured.value.details["operation"] == "create_native_scratch"
    assert events == ["allocated", "cleanup"]
    assert cleanup_entries == []
    assert not child.exists()
    assert _tree_manifest(sandbox.root) == before


@pytest.mark.parametrize("source", [None, "relative/source", "/bad\x00source"])
def test_malformed_provenance_denies_native_scratch_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: object
) -> None:
    sandbox = _native_snapshot(tmp_path / "snapshot", source)
    before = _tree_manifest(sandbox.root)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Malformed provenance reached scratch allocation or runtime")

    monkeypatch.setattr(native.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(native, "NativeRuntime", forbidden)
    with pytest.raises(NativeError) as captured:
        with NativeStoreSession(sandbox, scratch_dir=tmp_path):
            pytest.fail("Malformed provenance authorized native scratch")

    assert captured.value.details["operation"] == "create_native_scratch"
    assert not _permission_denied(captured.value)
    assert _tree_manifest(sandbox.root) == before


@pytest.mark.parametrize("error_number", [errno.EACCES, errno.EPERM])
def test_source_identity_permission_denial_preserves_native_cause_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_number: int
) -> None:
    source = (tmp_path / "source").resolve()
    source.mkdir()
    sandbox = _native_snapshot(tmp_path / "snapshot", str(source))
    before = _tree_manifest(source), _tree_manifest(sandbox.root)
    original = Path.stat

    def denied(path: Path, *args: Any, **kwargs: Any) -> os.stat_result:
        if path == source:
            raise PermissionError(error_number, "generated source identity denial", str(path))
        return original(path, *args, **kwargs)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("Denied source identity reached scratch allocation or runtime")

    with monkeypatch.context() as fault:
        fault.setattr(Path, "stat", denied)
        fault.setattr(native.tempfile, "TemporaryDirectory", forbidden)
        fault.setattr(native, "NativeRuntime", forbidden)
        with pytest.raises(NativeError) as captured:
            with NativeStoreSession(sandbox, scratch_dir=tmp_path):
                pytest.fail("Denied source identity authorized native scratch")

    assert captured.value.details["operation"] == "create_native_scratch"
    assert _permission_denied(captured.value)
    assert (_tree_manifest(source), _tree_manifest(sandbox.root)) == before


def test_source_symlink_loop_is_an_expected_native_failure_before_allocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source-loop"
    source.symlink_to(source)
    sandbox = _native_snapshot(tmp_path / "snapshot", str(source))
    before = _tree_manifest(sandbox.root)

    def forbidden(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("A source loop reached scratch allocation or runtime")

    monkeypatch.setattr(native.tempfile, "TemporaryDirectory", forbidden)
    monkeypatch.setattr(native, "NativeRuntime", forbidden)
    with pytest.raises(NativeError) as captured:
        with NativeStoreSession(sandbox, scratch_dir=tmp_path):
            pytest.fail("A source loop authorized native scratch")

    assert captured.value.details["operation"] == "create_native_scratch"
    assert source.is_symlink()
    assert _tree_manifest(sandbox.root) == before
