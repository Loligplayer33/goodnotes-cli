from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from goodnotes_cli.cache import refresh_cache
from goodnotes_cli.diagnostics import inspect_environment
from tests.support.cli import _tree_manifest
from tests.support.library import make_library


def _initialized_cache(tmp_path: Path) -> tuple[Path, Path]:
    source = make_library(tmp_path / "source")
    state = tmp_path / "state"
    refresh_cache(source, state)
    return source, state


def test_missing_artifact_root_uses_nearest_parent_without_creating_it(
    tmp_path: Path,
) -> None:
    _, state = _initialized_cache(tmp_path)
    artifact_root = state / "artifacts"
    before = _tree_manifest(state)

    result = inspect_environment(state, artifact_dir=artifact_root)

    check = next(check for check in result["checks"] if check["name"] == "artifact_directory")
    assert result["status"] == "ready"
    assert result["artifact_dir"] == str(artifact_root)
    assert check["required"] is False
    assert check["planned_path"] == str(artifact_root)
    assert check["access_path"] == str(state)
    assert check["directory_exists"] is False
    assert check["write_access_hint"] is True
    assert check["write_tested"] is False
    assert not artifact_root.exists()
    assert _tree_manifest(state) == before


def test_managed_sandbox_selects_its_cache_artifact_root(tmp_path: Path) -> None:
    source = make_library(tmp_path / "source")
    custom_state = tmp_path / "custom-cache"
    snapshot_result = refresh_cache(source, custom_state)
    snapshot = snapshot_result["snapshot"]
    assert isinstance(snapshot, str)
    default_state = tmp_path / "different-default-cache"

    result = inspect_environment(default_state, sandbox=Path(snapshot))

    check = next(check for check in result["checks"] if check["name"] == "artifact_directory")
    assert result["status"] == "ready"
    assert result["cache_dir"] == str(custom_state)
    assert result["artifact_dir"] == str(custom_state / "artifacts")
    assert check["planned_path"] == str(custom_state / "artifacts")
    assert check["access_path"] == str(custom_state)
    assert check["directory_exists"] is False


def test_unwritable_artifact_cache_does_not_block_readiness_or_write_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state = _initialized_cache(tmp_path)
    artifact_root = state / "artifacts"
    artifact_root.mkdir()
    durable_output = tmp_path / "durable-output"
    durable_output.mkdir()
    before_cache = _tree_manifest(state)
    before_output = _tree_manifest(durable_output)
    original_access = os.access

    def deny_artifact_write(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int
    ) -> bool:
        if path == artifact_root:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", deny_artifact_write)
    result = inspect_environment(state, output_dir=durable_output, artifact_dir=artifact_root)

    checks = {check["name"]: check for check in result["checks"]}
    artifact_check = checks["artifact_directory"]
    assert result["status"] == "ready"
    assert artifact_check["required"] is False
    assert artifact_check["status"] == "denied"
    assert artifact_check["code"] == "output_permission_denied"
    assert artifact_check["write_access_hint"] is False
    assert artifact_check["write_tested"] is False
    assert checks["output_directory"]["write_access_hint"] is True
    assert _tree_manifest(state) == before_cache
    assert _tree_manifest(durable_output) == before_output


def test_unwritable_nearest_parent_is_an_optional_hint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, state = _initialized_cache(tmp_path)
    artifact_root = state / "not-created" / "artifacts"
    original_access = os.access

    def deny_parent_write(
        path: int | str | bytes | os.PathLike[str] | os.PathLike[bytes], mode: int
    ) -> bool:
        if path == state:
            return False
        return original_access(path, mode)

    monkeypatch.setattr(os, "access", deny_parent_write)
    result = inspect_environment(state, artifact_dir=artifact_root)

    check = next(check for check in result["checks"] if check["name"] == "artifact_directory")
    assert result["status"] == "ready"
    assert check["required"] is False
    assert check["status"] == "denied"
    assert check["access_path"] == str(state)
    assert check["planned_path"] == str(artifact_root)
    assert check["write_access_hint"] is False
    assert check["write_tested"] is False
    assert not artifact_root.exists()


@pytest.mark.parametrize("symlink_component", ["artifact-root", "parent"])
def test_artifact_diagnostics_reject_symlinked_roots_without_traversing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    symlink_component: str,
) -> None:
    _, state = _initialized_cache(tmp_path)
    external = tmp_path / "external-artifacts"
    external.mkdir()
    if symlink_component == "artifact-root":
        artifact_root = state / "artifacts"
        artifact_root.symlink_to(external, target_is_directory=True)
        unsafe_path = artifact_root
    else:
        alias = tmp_path / "artifact-alias"
        alias.symlink_to(external, target_is_directory=True)
        artifact_root = alias / "artifacts"
        unsafe_path = alias

    original_scandir = os.scandir

    def forbid_symlink_traversal(
        path: int | str | os.PathLike[str] | None = None,
    ) -> Iterator[os.DirEntry[str]]:
        if path == unsafe_path or path == external:
            pytest.fail("artifact diagnostics must reject symlinks before traversal")
        if path is None:
            return original_scandir()
        return original_scandir(path)

    monkeypatch.setattr(os, "scandir", forbid_symlink_traversal)
    result = inspect_environment(state, artifact_dir=artifact_root)

    check = next(check for check in result["checks"] if check["name"] == "artifact_directory")
    assert result["status"] == "ready"
    assert check["required"] is False
    assert check["status"] == "invalid"
    assert check["code"] == "output_unavailable"
    assert "symlinks" in check["message"]
    assert check["write_access_hint"] is False
    assert check["write_tested"] is False


def test_macos_system_alias_and_arbitrary_symlink_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from goodnotes_cli.diagnostics import _nearest_existing_artifact_directory

    if sys.platform == "darwin":
        path = Path("/tmp") / f"goodnotes-absent-{os.getpid()}" / "artifacts"
        assert _nearest_existing_artifact_directory(path) == (Path("/tmp"), False)
        with pytest.raises(ValueError, match="symlink"):
            _nearest_existing_artifact_directory(Path("/tmp"))
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(sys, "platform", "darwin")
    with pytest.raises(ValueError, match="symlink"):
        _nearest_existing_artifact_directory(link / "artifacts")


def test_macos_alias_requires_expected_directory_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys
    from types import SimpleNamespace

    from goodnotes_cli.diagnostics import _nearest_existing_artifact_directory

    if sys.platform != "darwin":
        pytest.skip("macOS system aliases are platform-specific")
    original_stat = Path.stat

    def changed_expected_identity(path: Path, *, follow_symlinks: bool = True) -> object:
        info = original_stat(path, follow_symlinks=follow_symlinks)
        if path == Path("/private/tmp"):
            return SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1, st_mode=info.st_mode)
        return info

    monkeypatch.setattr(Path, "stat", changed_expected_identity)
    with pytest.raises(ValueError, match="symlinks"):
        _nearest_existing_artifact_directory(Path("/tmp") / "generated-missing" / "artifacts")
