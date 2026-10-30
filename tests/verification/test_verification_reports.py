"""Git admission proves receipt destinations are untracked before publication."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.verification import report


def _tracked_receipt(tmp_path: Path, name: str = "tracked.json") -> tuple[Path, Path]:
    root = tmp_path / "repository"
    destination = root / ".local" / name
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps({"schema": report.SCHEMA, "original": True}))
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", ".local"], check=True)
    return root, destination


@pytest.mark.parametrize("failure", [1, 128, "unavailable", "timeout"])
def test_git_failure_cannot_replace_a_generated_tracked_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: int | str,
) -> None:
    root, destination = _tracked_receipt(tmp_path)
    original = destination.read_bytes()
    before = tuple(destination.parent.iterdir())

    def fail_lookup(command: list[str], **kwargs: Any) -> Any:
        assert command[:3] == ["git", "--literal-pathspecs", "ls-files"]
        if failure == "unavailable":
            raise OSError("PRIVATE_SENTINEL")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(command, 15, output=b"PRIVATE_SENTINEL")
        return SimpleNamespace(returncode=failure, stdout=b"", stderr=b"PRIVATE_SENTINEL")

    monkeypatch.setattr(report.subprocess, "run", fail_lookup)
    with pytest.raises(ValueError, match="^report_path_protection_unavailable$"):
        report._write_report(destination, {"schema": report.SCHEMA, "new": True}, root)
    assert destination.read_bytes() == original
    assert tuple(destination.parent.iterdir()) == before
    with pytest.raises(ValueError, match="^report_path_protection_unavailable$"):
        report._write_report(
            root / ".local" / "absent" / "receipt.json", {"schema": report.SCHEMA}, root
        )
    assert not (root / ".local" / "absent").exists()


def test_successful_git_lookup_uses_literal_pathnames(tmp_path: Path) -> None:
    root, tracked = _tracked_receipt(tmp_path, "[receipt].json")
    original = tracked.read_bytes()
    with pytest.raises(ValueError, match="^report_path_protected$"):
        report._write_report(tracked, {"schema": report.SCHEMA}, root)
    assert tracked.read_bytes() == original
    untracked = tracked.with_name("*.json")
    receipt = {"schema": report.SCHEMA, "generated": True}
    report._write_report(untracked, receipt, root)
    assert json.loads(untracked.read_text()) == receipt
