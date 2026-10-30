"""Tests for the contract runner's frozen-baseline provenance guard."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest

_RUNNER_PATH = Path(__file__).with_name("contract_compare.py")
_SPEC = importlib.util.spec_from_file_location("contract_compare", _RUNNER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUNNER = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUNNER)


def _generated_git_package(tmp_path: Path) -> tuple[Path, Path, str]:
    repo = tmp_path / "candidate"
    package = repo / "src" / "goodnotes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("PACKAGE = 'generated'\n", encoding="utf-8")
    (package / "reader.py").write_text("def read():\n    return 'generated'\n", encoding="utf-8")

    environment = dict(os.environ)
    environment.update(
        {
            "GIT_AUTHOR_NAME": "Generated Test",
            "GIT_AUTHOR_EMAIL": "generated@example.invalid",
            "GIT_COMMITTER_NAME": "Generated Test",
            "GIT_COMMITTER_EMAIL": "generated@example.invalid",
        }
    )
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "src/goodnotes_cli"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "generated baseline"],
        check=True,
        env=environment,
    )
    revision = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    baseline = tmp_path / "baseline"
    baseline_package = baseline / "src" / "goodnotes_cli"
    baseline_package.mkdir(parents=True)
    for source in package.glob("*.py"):
        (baseline_package / source.name).write_bytes(source.read_bytes())
    return repo, baseline, revision


def test_baseline_verifier_records_resolved_revision_and_python_digest(tmp_path: Path) -> None:
    repo, baseline, revision = _generated_git_package(tmp_path)

    provenance = _RUNNER._verify_baseline_package(baseline, repo, revision)

    assert provenance["revision"] == revision
    assert provenance["package_path"] == "src/goodnotes_cli"
    assert provenance["package_python_file_count"] == 2
    assert len(provenance["package_python_sha256"]) == 64


@pytest.mark.parametrize("mutation", ["bytes", "pathset"])
def test_baseline_verifier_rejects_mutated_package(tmp_path: Path, mutation: str) -> None:
    repo, baseline, revision = _generated_git_package(tmp_path)
    package = baseline / "src" / "goodnotes_cli"
    if mutation == "bytes":
        (package / "reader.py").write_text("def read():\n    return 'changed'\n", encoding="utf-8")
        message = "content differs from Git"
    else:
        (package / "reader.py").unlink()
        message = "pathset does not match Git"

    with pytest.raises(ValueError, match=message):
        _RUNNER._verify_baseline_package(baseline, repo, revision)


@pytest.mark.parametrize(
    ("stdout", "stderr", "expected_stdout", "expected_stderr"),
    [
        (b"normal out", b"normal err", "normal out", "normal err"),
        (b"out\xff", b"err\xfe", "out\ufffd", "err\ufffd"),
    ],
)
def test_timeout_output_is_decoded_and_serializable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: bytes,
    stderr: bytes,
    expected_stdout: str,
    expected_stderr: str,
) -> None:
    import json

    def timeout(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired("generated", 45, output=stdout, stderr=stderr)

    monkeypatch.setattr(_RUNNER.subprocess, "run", timeout)
    source = tmp_path / "source"
    source.mkdir()
    result = _RUNNER._run_case(
        root=tmp_path,
        output=tmp_path,
        run_root=tmp_path / "run",
        sandbox=tmp_path / "sandbox",
        source=source,
        name="generated",
        arguments=["ls"],
    )
    assert result["timeout"] is True
    assert result["stdout"] == expected_stdout
    assert result["stderr"] == expected_stderr
    assert "source_unchanged" not in result
    json.dumps(result)


@pytest.mark.parametrize(
    ("baseline", "candidate", "state", "incomplete", "differences"),
    [
        ({"timeout": True}, {"timeout": True}, "incomplete", ["case"], 0),
        ({"timeout": True}, {"exit_code": 0}, "incomplete", ["case"], 0),
        ({"exit_code": 0}, {"exit_code": 0}, "equivalent", [], 0),
        (
            {"exit_code": 1, "stderr": "generated error"},
            {"exit_code": 1, "stderr": "generated error"},
            "equivalent",
            [],
            0,
        ),
        ({"exit_code": 0}, {"exit_code": 1}, "differences_require_review", [], 1),
    ],
)
def test_main_aggregates_timeout_and_differences(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    baseline: dict[str, object],
    candidate: dict[str, object],
    state: str,
    incomplete: list[str],
    differences: int,
) -> None:
    import json
    import sys

    provenance = {"revision": "generated"}
    monkeypatch.setattr(_RUNNER, "_verify_baseline_package", lambda *args: provenance)
    monkeypatch.setattr(_RUNNER, "_create_fixture", lambda root: (root, root, root))
    monkeypatch.setattr(
        _RUNNER,
        "_run_side",
        lambda root, label, *args: {
            "commands": [],
            "records": {"case": baseline if label == "baseline" else candidate},
            "verified_baseline": provenance,
        },
    )
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--baseline", "base", "--candidate", "candidate", "--output", str(output)],
    )
    assert _RUNNER.main() == 0
    receipt = json.loads((output / "diff.json").read_text())
    assert receipt["comparison_state"] == state
    assert receipt["incomplete_cases"] == incomplete
    assert receipt["incomplete_case_count"] == len(incomplete)
    assert receipt["difference_count"] == differences
    assert receipt["exit_status_policy"] == "capture-completion-only"
    assert receipt["verified_baseline"] == provenance


def test_incomplete_and_semantic_differences_are_both_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import sys

    monkeypatch.setattr(_RUNNER, "_verify_baseline_package", lambda *args: {})
    monkeypatch.setattr(_RUNNER, "_create_fixture", lambda root: (root, root, root))
    monkeypatch.setattr(
        _RUNNER,
        "_run_side",
        lambda root, label, *args: {
            "commands": [],
            "records": {
                "timeout": {"timeout": True},
                "difference": {"exit_code": 0 if label == "baseline" else 1},
            },
        },
    )
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        ["runner", "--baseline", "base", "--candidate", "candidate", "--output", str(output)],
    )
    assert _RUNNER.main() == 0
    receipt = json.loads((output / "diff.json").read_text())
    assert receipt["comparison_state"] == "incomplete"
    assert receipt["incomplete_cases"] == ["timeout"]
    assert [difference["case"] for difference in receipt["differences"]] == ["difference"]


def test_comparator_runs_intended_success_under_hostile_inherited_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, sandbox, _ = _RUNNER._create_fixture(tmp_path / "fixture")
    for key, value in {
        "GOODNOTES_LIBRARY_ROOT": "unrelated-catalog-root",
        "GOODNOTES_SANDBOX": str(tmp_path / "unrelated-snapshot"),
        "GOODNOTES_DATA_DIR": str(tmp_path / "unrelated-data"),
        "GOODNOTES_SCRATCH_DIR": str(tmp_path / "unrelated-scratch"),
        "GOODNOTES_ROCKSDB_LIBRARY": "/generated/selected-rocksdb",
        "PYTHONHOME": str(tmp_path / "unrelated-python"),
        "PYTHONPATH": str(tmp_path / "unrelated-imports"),
        "PYTEST_ADDOPTS": "--unrelated-option",
    }.items():
        monkeypatch.setenv(key, value)
    parent = dict(os.environ)
    seen: dict[str, str] = {}
    original_run = subprocess.run

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        seen.update(kwargs["env"])
        return original_run(*args, **kwargs)

    monkeypatch.setattr(_RUNNER.subprocess, "run", run)
    result = _RUNNER._run_case(
        root=_RUNNER_PATH.parents[2],
        output=tmp_path,
        run_root=tmp_path / "run",
        sandbox=sandbox,
        source=source,
        name="intended-ls",
        arguments=["ls"],
    )

    assert result["exit_code"] == 0, result
    assert result["stderr"] == ""
    assert {item["id"] for item in result["stdout"]["items"]} >= {
        "folder-a",
        "folder-b",
        "doc-root",
    }
    assert result["source_unchanged"] is True
    assert seen["GOODNOTES_ROCKSDB_LIBRARY"] == "/generated/selected-rocksdb"
    assert seen["GOODNOTES_DATA_DIR"] == str(tmp_path / "run" / "state" / "intended-ls")
    assert seen["GOODNOTES_SCRATCH_DIR"] == str(tmp_path / "run" / "scratch")
    assert {key for key in seen if key.startswith("PYTHON")} == {"PYTHONPATH", "PYTHONNOUSERSITE"}
    assert seen["PYTHONPATH"] == str(_RUNNER_PATH.parents[2] / "src")
    assert seen["PYTHONNOUSERSITE"] == "1"
    assert "GOODNOTES_LIBRARY_ROOT" not in seen
    assert not any(key.startswith("PYTEST_") for key in seen)
    assert dict(os.environ) == parent


def test_explicit_sandbox_sync_conflict_uses_common_completed_record(tmp_path: Path) -> None:
    source, sandbox, _ = _RUNNER._create_fixture(tmp_path / "fixture")
    arguments = ["--sandbox", str(sandbox), "sync", "--library", str(source)]

    result = _RUNNER._run_case(
        root=_RUNNER_PATH.parents[2],
        output=tmp_path,
        run_root=tmp_path / "run",
        sandbox=sandbox,
        source=source,
        name="error/sync-with-sandbox",
        arguments=arguments,
    )

    assert result["exit_code"] == 1
    assert "--sandbox" in result["stderr"]["error"]["message"]
    assert result["command"].count("--sandbox") == 1
    assert result["source_unchanged"] is True
    assert result["files"] == []


@pytest.mark.parametrize(
    ("capture", "timeout_sides"),
    [("candidate", {"candidate"}), ("both", {"candidate"}), ("both", {"baseline", "candidate"})],
    ids=["single", "paired-one-timeout", "paired-matching-timeouts"],
)
def test_sync_conflict_timeout_uses_common_owner_and_writes_incomplete_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capture: str,
    timeout_sides: set[str],
) -> None:
    import json
    import sys

    case_name = "error/sync-with-sandbox"
    calls: list[str] = []
    original_case = _RUNNER._run_case
    provenance = {"revision": "generated"}
    monkeypatch.setattr(_RUNNER, "_verify_baseline_package", lambda *args: provenance)

    def only_conflict(**kwargs: Any) -> dict[str, Any]:
        if kwargs["name"] != case_name:
            return {"exit_code": 0, "stdout": {}}
        return original_case(**kwargs)

    def run(arguments: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        label = kwargs["cwd"].name
        calls.append(label)
        assert arguments.count("--sandbox") == 1
        assert arguments[3] == "--sandbox"
        assert "sync" in arguments
        if label in timeout_sides:
            raise subprocess.TimeoutExpired(arguments, 45, output=b"partial\xff", stderr=b"err\xfe")
        return subprocess.CompletedProcess(arguments, 1, "", '{"error":{"message":"conflict"}}')

    monkeypatch.setattr(_RUNNER, "_run_case", only_conflict)
    monkeypatch.setattr(_RUNNER.subprocess, "run", run)
    output = tmp_path / "output"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--baseline",
            str(tmp_path / "baseline"),
            "--candidate",
            str(tmp_path / "candidate"),
            "--output",
            str(output),
            "--capture",
            capture,
        ],
    )

    assert _RUNNER.main() == 0
    assert calls == (["candidate"] if capture == "candidate" else ["baseline", "candidate"])
    if capture == "both":
        summary = json.loads((output / "diff.json").read_text())
        assert summary["comparison_state"] == "incomplete"
        assert summary["difference_count"] == 0
        assert summary["incomplete_case_count"] == 1
        receipt = output / "candidate.json"
    else:
        summary = json.loads(next(output.glob("capture-*.json")).read_text())
        assert summary["comparison_state"] == "not_compared"
        receipt = Path(summary["receipt"])
    assert summary["capture_complete"] is True
    assert summary["incomplete_cases"] == [case_name]
    assert summary["exit_status_policy"] == "capture-completion-only"
    record = json.loads(receipt.read_text())["records"][case_name]
    assert record["timeout"] is True
    assert record["stdout"] == "partial\ufffd"
    assert record["stderr"] == "err\ufffd"
    assert "source_unchanged" not in record
