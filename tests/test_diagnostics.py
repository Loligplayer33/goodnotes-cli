from __future__ import annotations

import errno
import os
import sqlite3
from pathlib import Path

import pytest
from test_cache import make_library
from test_cli import _tree_manifest

from goodnotes_cli.cache import refresh_cache
from goodnotes_cli.diagnostics import inspect_environment
from goodnotes_cli.errors import CacheAccessError, FilesystemAccessError, GoodnotesError


def test_missing_cache_diagnostics_do_not_initialize_any_paths(tmp_path: Path) -> None:
    state = tmp_path / "missing" / "cache"
    result = inspect_environment(state)

    assert result["status"] == "issues"
    assert result["library"] is None
    assert result["scope"] == "filesystem-access"
    assert result["checks"][0]["status"] == "missing"
    assert result["checks"][0]["code"] == "cache_not_initialized"
    assert result["checks"][0]["path"] == str(state)
    assert not state.parent.exists()


def test_diagnostics_read_only_probe_does_not_open_databases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    state = tmp_path / "state"
    refresh_cache(source, state)
    before_cache = _tree_manifest(state)
    before_source = _tree_manifest(source)

    def forbidden_sqlite(*args: object, **kwargs: object) -> None:
        pytest.fail("access diagnostics must not open any database engine")

    monkeypatch.setattr(sqlite3, "connect", forbidden_sqlite)
    result = inspect_environment(state, source)

    assert result["status"] == "ready"
    assert result["cache_dir"] == str(state)
    assert result["library"] == str(source)
    assert result["errors"] == []
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["projection_database"]["status"] == "ok"
    assert checks["source_projection"]["status"] == "ok"
    assert checks["native_store"]["status"] == "missing"
    assert not checks["native_store"]["required"]
    assert _tree_manifest(state) == before_cache
    assert _tree_manifest(source) == before_source


def test_source_permission_denial_reports_selected_path_and_remedy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    target = source / "Databases" / "projection.sqlite"
    original_open = os.open

    def denied_open(path: object, *args: object, **kwargs: object) -> int:
        if path == target:
            raise PermissionError(errno.EACCES, "generated source denial", str(target))
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", denied_open)
    result = inspect_environment(tmp_path / "missing-cache", source)
    checks = {check["name"]: check for check in result["checks"]}
    check = checks["source_projection"]
    assert check["status"] == "denied"
    assert check["code"] == "source_permission_denied"
    assert check["path"] == str(target)
    assert check["operation"] == "inspect_source_projection"
    assert "sandboxed agent" in check["remedy"]
    assert "source permissions unchanged" in check["remedy"]


def test_cache_lock_denial_is_reported_without_repair_or_lock_bypass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_library(tmp_path / "source")
    state = tmp_path / "state"
    refresh_cache(source, state)
    before = _tree_manifest(state)
    lock = state / ".view.lock"
    original_open = os.open

    def denied_open(path: object, *args: object, **kwargs: object) -> int:
        if path == lock:
            raise PermissionError(errno.EPERM, "generated lock denial", str(lock))
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", denied_open)
    result = inspect_environment(state)
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["view_lock"]["status"] == "denied"
    assert checks["view_lock"]["code"] == "cache_permission_denied"
    assert checks["snapshot"]["status"] == "denied"
    assert checks["snapshot"]["operation"] == "read_view_lock"
    assert _tree_manifest(state) == before


@pytest.mark.parametrize("kind", ["source", "cache"])
def test_diagnostics_reject_symlinked_roots_without_traversing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    destination = tmp_path / "destination"
    destination.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(destination, target_is_directory=True)

    original_scandir = os.scandir

    def forbidden_scandir(path: object):
        if path == alias or path == destination:
            pytest.fail("a rejected symlink root must not be traversed")
        return original_scandir(path)  # type: ignore[call-overload]

    monkeypatch.setattr(os, "scandir", forbidden_scandir)
    result = inspect_environment(
        alias if kind == "cache" else tmp_path / "absent-cache",
        alias if kind == "source" else None,
    )
    check = next(
        check
        for check in result["checks"]
        if check["name"] == ("source_library" if kind == "source" else "cache_directory")
    )
    assert check["status"] == "invalid"
    assert "symlink" in check["message"]


def test_diagnostics_reject_non_regular_active_pointer(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    os.mkfifo(state / "active.json")
    result = inspect_environment(state)
    check = next(check for check in result["checks"] if check["name"] == "active_pointer")
    assert check["status"] == "invalid"
    assert "regular file" in check["message"]


@pytest.mark.parametrize("kind", ["source", "output"])
def test_filesystem_error_uses_chained_permission_path(kind: str) -> None:
    original = PermissionError(errno.EPERM, "generated denial", b"/generated/denied")
    wrapped = GoodnotesError("outer error")
    wrapped.__cause__ = original
    error = FilesystemAccessError.from_exception(
        wrapped,
        operation="generated_operation",
        path=Path("/generated/fallback"),
        kind=kind,  # type: ignore[arg-type]
    )
    assert error.code == f"{kind}_permission_denied"
    assert error.details["path"] == "/generated/denied"
    assert error.details["operation"] == "generated_operation"
    assert "sandboxed agent" in error.details["remedy"]


def test_output_missing_parent_error_is_distinct_from_permission_denied() -> None:
    error = FilesystemAccessError.from_exception(
        FileNotFoundError(errno.ENOENT, "generated missing", "/generated/missing"),
        operation="publish_output",
        path=Path("/generated/output.png"),
        kind="output",
    )
    assert error.code == "output_unavailable"
    assert error.details["path"] == "/generated/missing"


def test_cache_permission_classification_includes_eperm() -> None:
    error = CacheAccessError.from_exception(
        OSError(errno.EPERM, "generated denial"),
        "cannot access",
        operation="read_view_lock",
        path="/generated/lock",
    )
    assert error.code == "cache_permission_denied"


def test_explicit_sandbox_diagnostics_do_not_inspect_default_cache(tmp_path: Path) -> None:
    from test_safety import make_snapshot

    selected = make_snapshot(tmp_path / "snapshot")
    state = tmp_path / "missing-cache"
    before = _tree_manifest(selected.root)
    result = inspect_environment(state, sandbox=selected.root, output_dir=tmp_path)

    assert result["status"] == "ready"
    assert result["cache_dir"] is None
    assert result["sandbox"] == str(selected.root)
    checks = {check["name"]: check for check in result["checks"]}
    assert "cache_directory" not in checks
    assert checks["snapshot"]["path"] == str(selected.root)
    assert checks["output_directory"]["write_access_hint"] is True
    assert checks["output_directory"]["write_tested"] is False
    assert _tree_manifest(selected.root) == before
    assert not state.exists()


def test_scratch_and_output_access_checks_do_not_create_probe_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "scratch"
    output = tmp_path / "output"
    scratch.mkdir()
    output.mkdir()
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", str(scratch))
    original_access = os.access

    def denied_output(path: object, mode: int) -> bool:
        if path == output:
            return False
        return original_access(path, mode)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "access", denied_output)
    before_scratch, before_output = _tree_manifest(scratch), _tree_manifest(output)
    result = inspect_environment(tmp_path / "missing-cache", output_dir=output)
    checks = {check["name"]: check for check in result["checks"]}
    assert result["scratch_dir"] == str(scratch)
    assert result["output_dir"] == str(output)
    assert checks["scratch_directory"]["write_access_hint"] is True
    assert checks["scratch_directory"]["write_tested"] is False
    assert checks["output_directory"]["status"] == "denied"
    assert checks["output_directory"]["code"] == "output_permission_denied"
    assert checks["output_directory"]["write_tested"] is False
    assert _tree_manifest(scratch) == before_scratch
    assert _tree_manifest(output) == before_output


def test_source_database_symlink_is_not_traversed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "Attachments").mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "projection.sqlite").write_bytes(b"generated")
    (source / "Databases").symlink_to(external, target_is_directory=True)
    original_open = os.open

    def forbidden_external(path: object, *args: object, **kwargs: object) -> int:
        if path == source / "Databases" / "projection.sqlite":
            pytest.fail("a rejected source database directory must not be traversed")
        return original_open(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(os, "open", forbidden_external)
    result = inspect_environment(tmp_path / "missing-cache", source)
    checks = {check["name"]: check for check in result["checks"]}
    assert checks["source_databases"]["status"] == "invalid"
    assert "source_projection" not in checks
