"""Focused tests for the development-only verification receipt and runner."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.support.paths import PROJECT_ROOT  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT))

from scripts.verification import environment, paths, report, runner, source, suites, targets  # noqa: E402
from scripts.verification.paths import temporary_parent  # noqa: E402


def _project(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "project"
    (root / "tests").mkdir(parents=True)
    package = root / "src" / "goodnotes_cli"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VALUE = 1\n")
    (package / "module.py").write_text("VALUE = 2\n")
    for name in suites.REQUIRED_SUITES:
        suite = root / "tests" / name
        suite.parent.mkdir(parents=True, exist_ok=True)
        suite.write_text("def test_fixture():\n    assert True\n")
    environment = root / "venv with spaces" / "bin"
    environment.mkdir(parents=True)
    python = environment / "python"
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    cli = environment / "goodnotes"
    cli.write_text(f"#!{python}\n")
    cli.chmod(0o755)
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    # Git 2.56 can detach automatic maintenance after the fixture commit.
    # Keep that background writer out of filesystem immutability checks.
    subprocess.run(["git", "-C", str(root), "config", "maintenance.auto", "false"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "gc.auto", "0"], check=True)
    subprocess.run(
        ["git", "-C", str(root), "config", "user.email", "tests@example.invalid"], check=True
    )
    subprocess.run(["git", "-C", str(root), "config", "user.name", "Verification Test"], check=True)
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)
    return root, python


def _filesystem_state(root: Path) -> tuple[tuple[Any, ...], ...]:
    entries = [root, *sorted(root.rglob("*"))]
    state = []
    for path in entries:
        info = path.lstat()
        if stat.S_ISREG(info.st_mode):
            content = hashlib.sha256(path.read_bytes()).hexdigest()
        elif stat.S_ISLNK(info.st_mode):
            content = str(path.readlink())
        else:
            content = None
        state.append(
            (
                path.relative_to(root).as_posix(),
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                info.st_ino,
                content,
            )
        )
    return tuple(state)


def _case_variant(path: Path) -> Path:
    for index, character in enumerate(path.name):
        if character.isalpha():
            replacement = character.lower() if character.isupper() else character.upper()
            return path.with_name(path.name[:index] + replacement + path.name[index + 1 :])
    return path


def _patch_source_package_probe(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        targets,
        "_probe_development_package",
        lambda *args: {"status": "pass", "version": "0.1.0", "import_origin": "source"},
    )


def _passing_suite(
    root: Path,
    suite_name: str,
    python: Path,
    environment: dict[str, str],
    junit_path: Path,
) -> dict[str, Any]:
    metrics = Path(environment["GOODNOTES_VERIFY_METRICS"])
    if not metrics.exists():
        metrics.write_text(
            json.dumps(
                {
                    "journeys": [
                        {
                            "name": "fixture journey",
                            "duration_seconds": 0.125,
                            "tool_calls": 2,
                            "stdout_bytes": 31,
                            "requests": [{"command": "render", "pages": [1, 2]}],
                            "private_note": "PRIVATE_SENTINEL",
                        }
                    ],
                    "private_path": "/private/PRIVATE_SENTINEL",
                }
            )
        )
    return {
        "status": "pass",
        "collected": 1,
        "passed": 1,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "duration_seconds": 0.01,
        "return_code": 0,
    }


def test_receipt_has_source_provenance_and_sanitized_journey_aggregates(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    report = tmp_path / "receipt.json"
    _patch_source_package_probe(monkeypatch)
    monkeypatch.setattr(runner, "_run_test_suite", _passing_suite)

    receipt, code = runner.run_verification(root=root, report=report, python_executable=python)

    saved = json.loads(report.read_text())
    rendered = json.dumps(receipt)
    assert code == 0
    assert receipt == saved
    assert receipt["schema"] == "goodnotes-verification/v1"
    assert receipt["status"] == "pass"
    assert receipt["target"]["scope"] == "source_editable"
    assert len(receipt["provenance"]["git_commit"]) == 40
    assert receipt["provenance"]["dirty"] is False
    assert (
        receipt["provenance"]["content_fingerprint_start"]
        == receipt["provenance"]["content_fingerprint_end"]
    )
    assert receipt["checks"]["generated_acceptance"]["status"] == "pass"
    assert receipt["checks"]["target_integrity"] == {"status": "pass", "unchanged": True}
    assert receipt["checks"]["private_corpus"]["status"] == "unverified"
    assert receipt["journey_metrics"]["journey_count"] == 1
    assert receipt["journey_metrics"]["journeys"][0]["journey"] == "journey_1"
    assert receipt["journey_metrics"]["journeys"][0]["requests"] == [
        {"command": "render", "pages": [1, 2]}
    ]
    assert "PRIVATE_SENTINEL" not in rendered
    assert "fixture journey" not in rendered


def test_tree_change_during_acceptance_invalidates_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    _patch_source_package_probe(monkeypatch)
    original_runner = _passing_suite
    changed = False

    def mutate_source(
        project: Path,
        suite_name: str,
        executable: Path,
        environment: dict[str, str],
        junit_path: Path,
    ) -> dict[str, Any]:
        nonlocal changed
        result = original_runner(project, suite_name, executable, environment, junit_path)
        if not changed:
            (project / "src" / "goodnotes_cli" / "module.py").write_text("VALUE = 99\n")
            changed = True
        return result

    monkeypatch.setattr(runner, "_run_test_suite", mutate_source)
    receipt, code = runner.run_verification(
        root=root, report=tmp_path / "changed.json", python_executable=python
    )

    assert code != 0
    assert receipt["status"] == "fail"
    assert receipt["checks"]["tree_integrity"] == {"status": "fail", "unchanged": False}
    assert (
        receipt["provenance"]["content_fingerprint_start"]
        != receipt["provenance"]["content_fingerprint_end"]
    )


def test_required_suite_skip_is_blocked_and_subprocess_text_is_not_reported(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    junit = tmp_path / "skip.xml"

    def fake_pytest(command: list[str], **kwargs: Any) -> types.SimpleNamespace:
        assert command[0] == str(python)
        assert "PYTHONPATH" not in kwargs["env"]
        junit_path = Path(command[command.index("--junitxml") + 1])
        junit_path.write_text(
            '<testsuites><testsuite tests="1"><testcase classname="fixture" name="test">'
            '<skipped message="PRIVATE_SENTINEL"/></testcase></testsuite></testsuites>'
        )
        return types.SimpleNamespace(
            returncode=0,
            stdout="PRIVATE_SENTINEL",
            stderr="PRIVATE_SENTINEL",
        )

    monkeypatch.setattr(runner.subprocess, "run", fake_pytest)
    result = runner._run_test_suite(root, suites.REQUIRED_SUITES[0], python, {}, junit)

    assert result["status"] == "blocked"
    assert result["skipped"] == 1
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def test_inherited_pytest_filters_cannot_narrow_required_suite_selection(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k unrelated")
    monkeypatch.setenv("PYTEST_PLUGINS", "untrusted.plugin")
    monkeypatch.setenv("GOODNOTES_REQUIRE_NATIVE_TESTS", "1")
    _patch_source_package_probe(monkeypatch)
    real_run = subprocess.run
    commands: list[list[str]] = []

    def fake_pytest(command: list[str], **kwargs: Any) -> Any:
        if command[0] == str(python) and "pytest" in command:
            commands.append(command)
            assert "PYTEST_ADDOPTS" not in kwargs["env"]
            assert "PYTEST_PLUGINS" not in kwargs["env"]
            assert "GOODNOTES_REQUIRE_NATIVE_TESTS" not in kwargs["env"]
            temp_root = Path(kwargs["env"]["GOODNOTES_VERIFY_METRICS"]).parent
            assert all(Path(kwargs["env"][key]) == temp_root for key in ("TMPDIR", "TEMP", "TMP"))
            report = Path(command[command.index("--junitxml") + 1])
            report.write_text(
                '<testsuites><testsuite tests="1"><testcase classname="fixed" name="case"/>'
                "</testsuite></testsuites>"
            )
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        return real_run(command, **kwargs)

    monkeypatch.setattr(runner.subprocess, "run", fake_pytest)

    receipt, code = runner.run_verification(
        root=root, report=tmp_path / "filtered.json", python_executable=python
    )

    assert code == 0
    assert len(commands) == len(suites.REQUIRED_SUITES)
    assert all(command[command.index("-o") + 1] == "addopts=" for command in commands)
    assert all("-k" not in command for command in commands)
    assert receipt["checks"]["generated_acceptance"]["status"] == "pass"


def test_subprocess_environment_scrubs_harness_configuration_and_keeps_rocksdb(
    monkeypatch: Any,
) -> None:
    monkeypatch.setenv("GOODNOTES_SANDBOX", "/inherited/sandbox")
    monkeypatch.setenv("GOODNOTES_DATA_DIR", "/inherited/data")
    monkeypatch.setenv("GOODNOTES_LIBRARY_ROOT", "/inherited/library")
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", "/inherited/scratch")
    monkeypatch.setenv("GOODNOTES_VERIFY_CLI", "/inherited/cli")
    monkeypatch.setenv("GOODNOTES_ROCKSDB_LIBRARY", "/selected/rocksdb")
    monkeypatch.setenv("PYTHONPATH", "/inherited/pythonpath")
    monkeypatch.setenv("PYTHONHOME", "/inherited/pythonhome")
    monkeypatch.setenv("PYTHONUSERBASE", "/inherited/userbase")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-k unrelated")
    monkeypatch.setenv("PYTEST_PLUGINS", "untrusted.plugin")

    result = environment.isolated_environment(
        {"GOODNOTES_VERIFY_CLI": "/selected/cli", "TMPDIR": "/selected/tmp"}
    )

    assert result["GOODNOTES_ROCKSDB_LIBRARY"] == "/selected/rocksdb"
    assert result["GOODNOTES_VERIFY_CLI"] == "/selected/cli"
    assert result["TMPDIR"] == "/selected/tmp"
    assert not any(key.startswith(("PYTHON", "PYTEST_")) for key in result)
    assert not any(
        key.startswith("GOODNOTES_")
        and key not in {"GOODNOTES_ROCKSDB_LIBRARY", "GOODNOTES_VERIFY_CLI"}
        for key in result
    )


def test_untracked_source_config_changes_fingerprint(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    before = source._source_snapshot(root)
    (root / "new-config.yml").write_text("enabled: true\n")
    after = source._source_snapshot(root)

    assert before["content_fingerprint"] != after["content_fingerprint"]
    assert after["dirty"] is True


def test_private_result_keeps_only_aggregate_and_opaque_fingerprint(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, _ = _project(tmp_path)
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    cli = tmp_path / "goodnotes"
    cli.write_text("#!/bin/sh\nexit 0\n")
    cli.chmod(0o755)
    helper = types.ModuleType("scripts.verification.reference")
    helper.__dict__["check_private_corpus"] = lambda manifest, cli: {
        "status": "pass",
        "cases": 2,
        "passed": 2,
        "failed": 0,
        "blocked": 0,
        "comparisons": 4,
        "corpus_fingerprint": "a" * 64,
        "private_path": "/private/PRIVATE_SENTINEL",
        "notebook": "PRIVATE_SENTINEL",
    }
    monkeypatch.setitem(sys.modules, "scripts.verification.reference", helper)

    result = runner._private_result(manifest, cli, root)

    assert result["status"] == "pass"
    assert result["cases"] == 2
    assert result["corpus_fingerprint"] == "a" * 64
    assert "private_path" not in result
    assert "notebook" not in result
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


def test_installed_target_rejects_same_version_with_stale_package_and_space_path(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    installed = tmp_path / "installed package" / "goodnotes_cli"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("VALUE = 'old wheel'\n")
    (installed / "module.py").write_text("VALUE = 2\n")
    interpreter = tmp_path / "python fake with spaces"
    payload = json.dumps(
        {"version": "0.1.0", "origin": str(installed / "__init__.py")}, separators=(",", ":")
    )
    interpreter.write_text(f"#!/bin/sh\nprintf '%s\\n' '{payload}'\n")
    interpreter.chmod(0o755)
    cli = tmp_path / "external-goodnotes"
    cli.write_text(f"#!{interpreter}\n")
    cli.chmod(0o755)
    monkeypatch.setattr(
        runner,
        "_run_test_suite",
        lambda *args: (_ for _ in ()).throw(AssertionError("blocked target must not run suites")),
    )

    receipt, code = runner.run_verification(
        root=root,
        cli=str(cli),
        report=tmp_path / "stale.json",
        python_executable=python,
    )

    assert code != 0
    assert receipt["target"]["scope"] == "installed_external"
    assert receipt["target"]["version"] == "0.1.0"
    assert receipt["target"]["status"] == "blocked"
    assert receipt["target"]["reason"] == "installed_package_out_of_sync"
    assert (
        receipt["target"]["package_fingerprint"] != receipt["target"]["source_package_fingerprint"]
    )
    assert str(installed) not in json.dumps(receipt)


def test_default_target_requires_the_expected_interpreter(tmp_path: Path, monkeypatch: Any) -> None:
    root, python = _project(tmp_path)
    other_python = tmp_path / "other environment" / "python"
    other_python.parent.mkdir()
    other_python.write_text("#!/bin/sh\nexit 0\n")
    other_python.chmod(0o755)
    cli = python.with_name("goodnotes")
    cli.write_text(f"#!{other_python}\n")
    cli.chmod(0o755)
    monkeypatch.setattr(
        targets,
        "_probe_development_package",
        lambda *args: (_ for _ in ()).throw(AssertionError("package probe must not run")),
    )

    selection = targets._select_target(None, root, python)
    target_path, target = selection.path, selection.receipt

    assert target_path == cli.resolve()
    assert target["scope"] == "source_editable"
    assert target["status"] == "blocked"
    assert target["reason"] == "development_interpreter_mismatch"


def test_default_target_rejects_a_package_origin_outside_the_checkout(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    installed = tmp_path / "other checkout" / "src" / "goodnotes_cli"
    installed.mkdir(parents=True)
    (installed / "__init__.py").write_text("VALUE = 'different checkout'\n")
    payload = json.dumps(
        {"version": "0.1.0", "origin": str(installed / "__init__.py")}, separators=(",", ":")
    )
    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda *args, **kwargs: types.SimpleNamespace(returncode=0, stdout=payload),
    )

    probe = targets._probe_development_package(python, [], root)

    assert probe == {
        "status": "blocked",
        "reason": "development_package_origin_mismatch",
    }
    assert str(installed) not in json.dumps(probe)


@pytest.mark.parametrize("location", ["repository", "snapshot", "source"])
def test_temporary_work_blocks_protected_tmpdir_without_filesystem_changes(
    tmp_path: Path, monkeypatch: Any, location: str
) -> None:
    root, python = _project(tmp_path)
    if location == "repository":
        protected = root
    elif location == "snapshot":
        protected = tmp_path / "protected snapshot"
        protected.mkdir()
        (protected / "goodnotes-snapshot.json").write_text('{"format_version":1}\n')
    else:
        protected = tmp_path / "protected source"
        (protected / "Databases").mkdir(parents=True)

    installed_cli = tmp_path / "installed-goodnotes"
    installed_cli.write_text(f"#!{python}\n")
    installed_cli.chmod(0o755)
    monkeypatch.setenv("TMPDIR", str(protected))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    monkeypatch.setattr(runner.tempfile, "tempdir", None)
    source_snapshot = {
        "git_commit": "a" * 40,
        "dirty": False,
        "status_fingerprint": "b" * 64,
        "content_fingerprint": "c" * 64,
    }
    monkeypatch.setattr(runner, "_source_snapshot", lambda project: source_snapshot)
    before = _filesystem_state(protected)

    installed_result = targets._probe_installed_cli(installed_cli, root).receipt
    development_result = targets._probe_development_package(python, [], root)
    assert installed_result == {"status": "blocked", "reason": "unsafe_temporary_root"}
    assert development_result == {"status": "blocked", "reason": "unsafe_temporary_root"}
    assert _filesystem_state(protected) == before

    monkeypatch.setattr(
        targets,
        "_probe_development_package",
        lambda *args: {"status": "pass", "version": "0.1.0", "import_origin": "source"},
    )
    monkeypatch.setattr(
        runner,
        "_run_test_suite",
        lambda *args: (_ for _ in ()).throw(AssertionError("acceptance suite must not run")),
    )
    receipt, code = runner.run_verification(
        root=root,
        report=tmp_path / "verification-report.json",
        python_executable=python,
    )

    assert code != 0
    assert receipt["checks"]["generated_acceptance"] == suites._blocked_acceptance(
        "unsafe_temporary_root"
    )
    assert _filesystem_state(protected) == before


def test_case_aliases_remain_inside_protected_repository_paths(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, _ = _project(tmp_path)
    alias = _case_variant(root)
    if alias == root or not alias.exists() or not alias.samefile(root):
        return
    monkeypatch.setenv("TMPDIR", str(alias))
    monkeypatch.delenv("TEMP", raising=False)
    monkeypatch.delenv("TMP", raising=False)
    before = _filesystem_state(root)

    with pytest.raises(ValueError, match="unsafe_temporary_root"):
        temporary_parent(root)
    assert paths.inside(alias / "missing", root)
    with pytest.raises(ValueError, match="report_path_protected"):
        report._write_report(alias / ".local" / "report.json", {"schema": report.SCHEMA}, root)
    assert not (root / ".local").exists()
    assert _filesystem_state(root) == before

    from scripts.verification import reference as reference_checks

    monkeypatch.setattr(reference_checks, "REPOSITORY", root)
    with pytest.raises(reference_checks.ReferenceInputError, match="repository_input"):
        reference_checks._outside_repository(alias / "manifest.json")
    assert _filesystem_state(root) == before


def test_report_does_not_overwrite_an_unrelated_existing_file(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    report = tmp_path / "existing.json"
    report.write_text('{"owner":"keep"}\n')
    _patch_source_package_probe(monkeypatch)
    monkeypatch.setattr(runner, "_run_test_suite", _passing_suite)

    receipt, code = runner.run_verification(root=root, report=report, python_executable=python)

    assert code != 0
    assert receipt["status"] == "fail"
    assert receipt["report"] == {"status": "failed", "reason": "report_write_failed"}
    assert report.read_text() == '{"owner":"keep"}\n'


def test_report_path_git_timeout_returns_sanitized_failure_receipt(
    tmp_path: Path, monkeypatch: Any
) -> None:
    root, python = _project(tmp_path)
    source_snapshot = {
        "git_commit": "a" * 40,
        "dirty": False,
        "status_fingerprint": "b" * 64,
        "content_fingerprint": "c" * 64,
    }
    monkeypatch.setattr(runner, "_source_snapshot", lambda project: source_snapshot)
    monkeypatch.setattr(
        runner,
        "_select_target",
        lambda cli, root, python: targets.Target(
            None, {"status": "blocked", "reason": "fixture_target"}
        ),
    )
    real_run = report.subprocess.run

    def timeout_report_protection(command: list[str], **kwargs: Any) -> Any:
        if command[0] == "git" and "ls-files" in command:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return real_run(command, **kwargs)

    monkeypatch.setattr(report.subprocess, "run", timeout_report_protection)

    receipt, code = runner.run_verification(
        root=root,
        report=root / ".local" / "verification" / "report.json",
        python_executable=python,
    )

    assert code == 1
    assert receipt["status"] == "fail"
    assert receipt["report"] == {"status": "failed", "reason": "report_write_failed"}
    assert "TimeoutExpired" not in json.dumps(receipt)
    assert not (root / ".local" / "verification" / "report.json").exists()


def test_report_rejects_source_files_and_completed_snapshots(tmp_path: Path) -> None:
    root, _ = _project(tmp_path)
    source = root / "src" / "goodnotes_cli" / "module.py"
    original_source = source.read_bytes()
    with pytest.raises(ValueError, match="report_path_protected"):
        report._write_report(source, {"schema": report.SCHEMA}, root)
    assert source.read_bytes() == original_source

    snapshot = tmp_path / "completed-copy"
    snapshot.mkdir()
    (snapshot / "goodnotes-snapshot.json").write_text('{"format_version":1}\n')
    destination = snapshot / "report.json"
    with pytest.raises(ValueError, match="report_path_protected"):
        report._write_report(destination, {"schema": report.SCHEMA}, root)
    assert not destination.exists()

    local_snapshot = root / ".local" / "snapshot"
    local_snapshot.mkdir(parents=True)
    (local_snapshot / "goodnotes-snapshot.json").write_text('{"format_version":1}\n')
    local_snapshot_report = local_snapshot / "report.json"
    with pytest.raises(ValueError, match="report_path_protected"):
        report._write_report(local_snapshot_report, {"schema": report.SCHEMA}, root)
    assert not local_snapshot_report.exists()

    local_source = root / ".local" / "source"
    (local_source / "Databases").mkdir(parents=True)
    local_source_report = local_source / "report.json"
    with pytest.raises(ValueError, match="report_path_protected"):
        report._write_report(local_source_report, {"schema": report.SCHEMA}, root)
    assert not local_source_report.exists()
