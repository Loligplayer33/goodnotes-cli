"""Generated installation controls for actual launcher provenance and lifetime."""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from scripts.verification import runner, targets
from scripts.verification.environment import isolated_environment

_GENERATED_CLI = """\
import hashlib
import json
from pathlib import Path

def main():
    digest = hashlib.sha256()
    package = Path(__file__).parent
    for path in sorted(package.glob('*.py')):
        relative = path.name.encode()
        digest.update(len(relative).to_bytes(8, 'big'))
        digest.update(relative)
        digest.update(path.read_bytes())
    print(json.dumps({'build': {'version': '1.0', 'source_sha256': digest.hexdigest()}}))
"""


@dataclass
class Installation:
    root: Path
    python: Path
    cli: Path
    metadata: Path

    def update_launcher_record(self) -> None:
        record = self.metadata / "RECORD"
        rows = list(csv.reader(io.StringIO(record.read_text())))
        for row in rows:
            if (self.metadata.parent / row[0]).resolve() == self.cli.resolve():
                raw = self.cli.read_bytes()
                row[1] = "sha256=" + base64.urlsafe_b64encode(
                    hashlib.sha256(raw).digest()
                ).decode().rstrip("=")
                row[2] = str(len(raw))
        with record.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)


@pytest.fixture
def installation(tmp_path: Path, request: pytest.FixtureRequest) -> Installation:
    """Install a real dependency-free generated wheel using the prepared uv tool."""
    root = tmp_path / "checkout"
    package = root / "src" / "goodnotes_cli"
    package.mkdir(parents=True)
    files = {
        "goodnotes_cli/__init__.py": "VALUE = 'generated'\n",
        "goodnotes_cli/cli.py": _GENERATED_CLI,
    }
    for name, source in files.items():
        (package / Path(name).name).write_text(source)
    info = "goodnotes_local_cli-1.0.dist-info"
    files.update(
        {
            f"{info}/METADATA": "Metadata-Version: 2.1\nName: goodnotes-local-cli\nVersion: 1.0\n",
            f"{info}/WHEEL": "Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
            f"{info}/entry_points.txt": "[console_scripts]\ngoodnotes = goodnotes_cli.cli:main\n",
        }
    )
    rows = []
    for name, source in files.items():
        raw = source.encode()
        digest = base64.urlsafe_b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        rows.append((name, f"sha256={digest}", str(len(raw))))
    rows.append((f"{info}/RECORD", "", ""))
    record = io.StringIO()
    csv.writer(record).writerows(rows)
    files[f"{info}/RECORD"] = record.getvalue()
    wheel = tmp_path / "goodnotes_local_cli-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, source in files.items():
            archive.writestr(name, source)
    environment = tmp_path / getattr(request, "param", "installed")
    uv = shutil.which("uv")
    assert uv is not None
    python = environment / "bin" / "python"
    for command in (
        [uv, "venv", "--python", sys.executable, str(environment)],
        [uv, "pip", "install", "--offline", "--no-deps", "--python", str(python), str(wheel)],
    ):
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=30, env=isolated_environment()
        )
        assert result.returncode == 0, result.stderr
    metadata = next(environment.glob(f"lib/python*/site-packages/{info}"))
    return Installation(root, python, environment / "bin" / "goodnotes", metadata)


@pytest.mark.parametrize("installation", ["installed", "installed with spaces"], indirect=True)
def test_real_registered_launcher_and_alias_bind_to_matching_schema(
    installation: Installation,
) -> None:
    alias = installation.cli.parent.parent / "selected-command"
    alias.symlink_to(installation.cli)
    selected = targets._select_target(str(alias), installation.root, Path(sys.executable))

    assert selected.receipt["status"] == "pass"
    assert selected.receipt["binding_basis"] == "distribution-record-and-schema"
    assert selected.package == installation.metadata.parent / "goodnotes_cli"
    assert targets._check_target_integrity(
        str(alias), installation.root, Path(sys.executable), selected
    ) == {"status": "pass", "unchanged": True}
    assert str(installation.cli.parent.parent) not in json.dumps(selected.receipt)


def test_trampoline_discovery_reads_without_executing_shell(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    installation.cli.write_text(
        f"#!/bin/sh\n'''exec' '{installation.python}' \"$0\" \"$@\"\n' '''\nraise SystemExit(7)\n"
    )
    monkeypatch.setattr(
        targets.subprocess,
        "run",
        lambda *args, **kwargs: pytest.fail("discovery executed a command"),
    )
    interpreter, arguments = targets._shebang_interpreter(installation.cli)
    assert interpreter == installation.python
    assert arguments == []
    installation.cli.write_text("#!/bin/sh\nexec anything\n")
    with pytest.raises(ValueError, match="python_shebang_required"):
        targets._shebang_interpreter(installation.cli)


@pytest.mark.parametrize("symlink", [False, True])
def test_checkout_executable_is_rejected_before_probing(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    symlink: bool,
) -> None:
    copied = installation.root / "goodnotes"
    shutil.copy(installation.cli, copied)
    candidate = copied
    if symlink:
        candidate = installation.root.parent / "alias"
        candidate.symlink_to(copied)
    monkeypatch.setattr(
        targets, "_probe_package", lambda *args: pytest.fail("checkout target was probed")
    )
    assert (
        targets._select_target(str(candidate), installation.root, installation.python).receipt[
            "reason"
        ]
        == "executable_in_checkout"
    )


@pytest.mark.parametrize("external", [False, True])
def test_editable_package_origin_retains_external_consistency_contract(
    installation: Installation,
    external: bool,
) -> None:
    package = installation.metadata.parent / "goodnotes_cli"
    shutil.rmtree(package)
    source = installation.root / "src"
    if external:
        source = installation.root.parent / "external-source"
        shutil.copytree(installation.root / "src", source)
    (installation.metadata.parent / "generated-editable.pth").write_text(str(source) + "\n")
    (installation.metadata / "direct_url.json").write_text(
        json.dumps({"url": source.as_uri(), "dir_info": {"editable": True}})
    )
    selected = targets._select_target(str(installation.cli), installation.root, installation.python)
    if external:
        assert selected.receipt["status"] == "pass"
        assert selected.package == source / "goodnotes_cli"
    else:
        assert selected.receipt["reason"] == "package_origin_in_checkout"


def test_real_installed_package_with_same_version_but_stale_source_blocks(
    installation: Installation,
) -> None:
    (installation.root / "src" / "goodnotes_cli" / "__init__.py").write_text(
        "VALUE = 'new source'\n"
    )
    selected = targets._select_target(str(installation.cli), installation.root, installation.python)
    assert selected.receipt["reason"] == "installed_package_out_of_sync"
    assert selected.receipt["version"] == "1.0"


@pytest.mark.parametrize(
    "mutation", ["missing", "unhashed", "wrong-hash", "duplicate", "entry-point"]
)
def test_installed_metadata_binding_is_required(installation: Installation, mutation: str) -> None:
    record = installation.metadata / "RECORD"
    if mutation == "missing":
        record.unlink()
    elif mutation == "entry-point":
        (installation.metadata / "entry_points.txt").write_text(
            "[console_scripts]\ngoodnotes = unrelated:main\n"
        )
    else:
        rows = list(csv.reader(io.StringIO(record.read_text())))
        row = next(
            row
            for row in rows
            if (installation.metadata.parent / row[0]).resolve() == installation.cli
        )
        if mutation == "duplicate":
            rows.append(list(row))
        else:
            row[1] = "" if mutation == "unhashed" else "sha256=wrong"
        with record.open("w", newline="") as stream:
            csv.writer(stream).writerows(rows)
    result = targets._select_target(
        str(installation.cli), installation.root, installation.python
    ).receipt
    assert result["status"] == "blocked"
    assert result["reason"] in {
        "installed_launcher_record_unavailable",
        "installed_launcher_hash_mismatch",
        "installed_entry_point_mismatch",
    }


@pytest.mark.parametrize(
    "response", ["wrong-version", "wrong-digest", "no-build", "invalid-json", "timeout"]
)
def test_actual_executable_must_report_matching_build(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    response: str,
) -> None:
    if response == "timeout":
        real_run = subprocess.run

        def run(command: list[str], **kwargs: Any) -> Any:
            if command == [str(installation.cli), "schema"]:
                raise subprocess.TimeoutExpired(command, 15, output=b"PRIVATE_SENTINEL")
            return real_run(command, **kwargs)

        monkeypatch.setattr(targets.subprocess, "run", run)
    else:
        digest = targets._python_package_fingerprint(installation.metadata.parent / "goodnotes_cli")
        build = {
            "version": "9" if response == "wrong-version" else "1.0",
            "source_sha256": "0" * 64 if response == "wrong-digest" else digest,
        }
        output = (
            "PRIVATE_SENTINEL"
            if response == "invalid-json"
            else json.dumps({} if response == "no-build" else {"build": build})
        )
        lines = installation.cli.read_text().splitlines(keepends=True)
        header = "".join(lines[:3] if lines[0].strip() == "#!/bin/sh" else lines[:1])
        installation.cli.write_text(header + f"print({output!r})\n")
        installation.update_launcher_record()
    result = targets._select_target(
        str(installation.cli), installation.root, installation.python
    ).receipt
    assert result["status"] == "blocked"
    assert "PRIVATE_SENTINEL" not in json.dumps(result)


@pytest.mark.parametrize(
    "mutation", ["launcher", "package", "alias", "checkout-alias", "deleted", "entry-point"]
)
def test_observable_end_mutation_fails_even_when_new_validation_blocks(
    installation: Installation,
    mutation: str,
) -> None:
    alias = installation.root.parent / "selected"
    alias.symlink_to(installation.cli)
    selected = targets._select_target(str(alias), installation.root, installation.python)
    assert selected.receipt["status"] == "pass"
    if mutation == "launcher":
        with installation.cli.open("a") as stream:
            stream.write("# generated change\n")
    elif mutation == "package":
        assert selected.package is not None
        (selected.package / "__init__.py").write_text("VALUE = 'changed'\n")
    elif mutation in {"alias", "checkout-alias"}:
        duplicate = (
            installation.root / "replacement"
            if mutation == "checkout-alias"
            else installation.root.parent / "replacement"
        )
        shutil.copy(installation.cli, duplicate)
        alias.unlink()
        alias.symlink_to(duplicate)
    elif mutation == "deleted":
        installation.cli.unlink()
    else:
        (installation.metadata / "entry_points.txt").write_text(
            "[console_scripts]\ngoodnotes = unrelated:main\n"
        )
    assert (
        targets._check_target_integrity(
            str(alias), installation.root, installation.python, selected
        )["status"]
        == "fail"
    )


def test_unreadable_end_identity_blocks(
    installation: Installation, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = targets._select_target(str(installation.cli), installation.root, installation.python)

    def unreadable(path: Path) -> str:
        raise PermissionError("PRIVATE_SENTINEL")

    monkeypatch.setattr(targets, "_file_fingerprint", unreadable)
    result = targets._check_target_integrity(
        str(installation.cli), installation.root, installation.python, selected
    )
    assert result == {"status": "blocked", "reason": "target_revalidation_unavailable"}


def test_runner_integrates_end_target_change_and_keeps_paths_private(
    installation: Installation,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = {
        "git_commit": "a" * 40,
        "dirty": False,
        "status_fingerprint": "b" * 64,
        "content_fingerprint": "c" * 64,
    }
    monkeypatch.setattr(runner, "_source_snapshot", lambda root: source)

    def suite(*args: Any) -> dict[str, str]:
        with installation.cli.open("a") as stream:
            stream.write("# changed during suite\n")
        return {"status": "pass"}

    monkeypatch.setattr(runner, "_run_test_suite", suite)
    receipt, code = runner.run_verification(
        root=installation.root, cli=str(installation.cli), report=tmp_path / "receipt.json"
    )
    assert code == 1
    assert receipt["status"] == "fail"
    assert receipt["checks"]["target_integrity"]["status"] == "fail"
    assert str(installation.root.parent) not in json.dumps(receipt)
