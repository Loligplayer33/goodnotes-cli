"""Generated successful commands ignore unrelated inherited harness settings."""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.library import Library
from tests.journeys import test_audit_workflow, test_native_workflow
from tests.support import cli
from tests.support.library import _create_snapshot
from tests.support.native_store_fixture import GeneratedNativeStore
from tests.support.native_store_fixture import generated_source as generated_source
from tests.support.native_store_fixture import native_runtime as native_runtime
from tests.verification import test_verification_reference as reference_tests


def _hostile_configuration(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    for key, value in {
        "GOODNOTES_SANDBOX": str(tmp_path / "unrelated-snapshot"),
        "GOODNOTES_DATA_DIR": str(tmp_path / "unrelated-data"),
        "GOODNOTES_LIBRARY_ROOT": "unrelated-catalog-root",
        "GOODNOTES_SCRATCH_DIR": str(tmp_path / "unrelated-scratch"),
        "GOODNOTES_VERIFY_CLI": str(tmp_path / "unrelated-command"),
        "GOODNOTES_VERIFY_METRICS": str(tmp_path / "unrelated-metrics"),
        "PYTHONHOME": str(tmp_path / "unrelated-python"),
        "PYTHONPATH": str(tmp_path / "unrelated-imports"),
        "PYTHONUSERBASE": str(tmp_path / "unrelated-userbase"),
        "PYTEST_ADDOPTS": "--unrelated-pytest-option",
        "PYTEST_PLUGINS": "unrelated_pytest_plugin",
    }.items():
        monkeypatch.setenv(key, value)


def _assert_clean(environment: dict[str, str], *, unbuffered: bool = False) -> None:
    assert not any(key.startswith("PYTEST_") for key in environment)
    python_keys = {key for key in environment if key.startswith("PYTHON")}
    assert python_keys == ({"PYTHONUNBUFFERED"} if unbuffered else set())
    assert {key for key in environment if key.startswith("GOODNOTES_")} <= {
        "GOODNOTES_ROCKSDB_LIBRARY",
        "GOODNOTES_DATA_DIR",
        "GOODNOTES_SCRATCH_DIR",
    }


@pytest.mark.parametrize("in_process", [False, True])
def test_shared_cli_helpers_run_the_intended_snapshot_and_restore_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, in_process: bool
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    _hostile_configuration(monkeypatch, tmp_path)
    monkeypatch.setenv("GOODNOTES_ROCKSDB_LIBRARY", "/generated/selected-rocksdb")
    parent = dict(os.environ)
    seen: dict[str, str] = {}
    if in_process:
        original_list = Library.list_items

        def listing(self: Library, *args: Any, **kwargs: Any) -> Any:
            seen.update(os.environ)
            return original_list(self, *args, **kwargs)

        monkeypatch.setattr(Library, "list_items", listing)
    else:
        original_run = subprocess.run

        def run(*args: Any, **kwargs: Any) -> Any:
            seen.update(kwargs["env"])
            return original_run(*args, **kwargs)

        monkeypatch.setattr(cli.subprocess, "run", run)
    invoke: Callable[..., subprocess.CompletedProcess[str]] = (
        cli.invoke_in_process if in_process else cli.invoke
    )
    data = cli.successful(
        invoke(["ls"], tmp_path, sandbox=sandbox.root, data_dir=tmp_path / "selected-data")
    )

    assert {item["id"] for item in data["items"]} >= {"folder-a", "folder-b", "doc-root"}
    _assert_clean(seen)
    assert seen["GOODNOTES_DATA_DIR"] == str(tmp_path / "selected-data")
    assert seen["GOODNOTES_ROCKSDB_LIBRARY"] == "/generated/selected-rocksdb"
    assert dict(os.environ) == parent
    assert not (tmp_path / "unrelated-data").exists()


def test_in_process_helper_restores_filtered_parent_keys_after_parser_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hostile_configuration(monkeypatch, tmp_path)
    parent = dict(os.environ)

    result = cli.invoke_in_process(["not-a-command"], tmp_path)

    assert result.returncode == 2
    assert dict(os.environ) == parent


@pytest.mark.parametrize("journey", [test_audit_workflow, test_native_workflow])
def test_journey_environments_run_intended_snapshot_with_deliberate_child_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, journey: Any
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    _hostile_configuration(monkeypatch, tmp_path)
    monkeypatch.setenv("GOODNOTES_ROCKSDB_LIBRARY", "/generated/selected-rocksdb")
    parent = dict(os.environ)

    environment = journey._environment(tmp_path)
    completed = subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", "--sandbox", str(sandbox.root), "ls"],
        capture_output=True,
        text=True,
        env=environment,
        check=False,
        timeout=30,
    )

    data = cli.successful(completed)
    assert {item["id"] for item in data["items"]} >= {"folder-a", "folder-b", "doc-root"}
    _assert_clean(environment, unbuffered=True)
    assert environment["GOODNOTES_DATA_DIR"] == str(tmp_path / "cache")
    assert environment["GOODNOTES_SCRATCH_DIR"] == str(tmp_path / "scratch")
    assert environment["PYTHONUNBUFFERED"] == "1"
    assert environment["GOODNOTES_ROCKSDB_LIBRARY"] == "/generated/selected-rocksdb"
    assert dict(os.environ) == parent


def test_selected_reference_child_uses_clean_environment_and_still_matches_native_oracle(
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = Path(sys.executable).parent / "goodnotes"
    assert executable.is_file()
    _hostile_configuration(monkeypatch, tmp_path)
    monkeypatch.setenv("GOODNOTES_VERIFY_CLI", str(executable))
    parent = dict(os.environ)
    seen: dict[str, str] = {}
    original_run = subprocess.run

    def run(arguments: list[str], **kwargs: Any) -> Any:
        assert arguments[0] == str(executable.resolve())
        seen.update(kwargs["env"])
        return original_run(arguments, **kwargs)

    monkeypatch.setattr(reference_tests.subprocess, "run", run)

    reference_tests.test_generated_native_output_matches_independently_drawn_reference(
        generated_source, tmp_path
    )

    _assert_clean(seen)
    assert seen["GOODNOTES_DATA_DIR"] == str(tmp_path / "data")
    assert seen["GOODNOTES_SCRATCH_DIR"] == str(tmp_path / "scratch")
    assert seen.get("GOODNOTES_ROCKSDB_LIBRARY") == parent.get("GOODNOTES_ROCKSDB_LIBRARY")
    assert dict(os.environ) == parent
