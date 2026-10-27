"""Tests for the contract runner's frozen-baseline provenance guard."""

from __future__ import annotations

import importlib.util
import os
import subprocess
from pathlib import Path

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
