"""Installed and editable CLI target validation."""

from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from base64 import urlsafe_b64encode
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .environment import isolated_environment
from .paths import inside, temporary_parent


@dataclass(frozen=True)
class Target:
    """Selected command and internal locations, separate from its public receipt."""

    path: Path | None
    receipt: dict[str, Any]
    interpreter: Path | None = None
    package: Path | None = None


def _package_version() -> str | None:
    try:
        return importlib.metadata.version("goodnotes-local-cli")
    except importlib.metadata.PackageNotFoundError:
        return None


def _python_package_fingerprint(directory: Path) -> str | None:
    if not directory.is_dir():
        return None
    hasher = hashlib.sha256()
    files = sorted(path for path in directory.rglob("*.py") if path.is_file())
    if not files:
        return None
    for path in files:
        relative = os.fsencode(path.relative_to(directory).as_posix())
        hasher.update(len(relative).to_bytes(8, "big"))
        hasher.update(relative)
        with path.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
    return hasher.hexdigest()


def _executable(path: Path) -> bool:
    try:
        return path.is_file() and os.access(path, os.X_OK)
    except OSError:
        return False


def _file_fingerprint(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _env_shebang(tokens: list[str]) -> tuple[Path, list[str]]:
    if not tokens:
        raise ValueError("interpreter_missing")
    args = list(tokens)
    if args[0] == "-S":
        args.pop(0)
    elif args[0] == "--":
        args.pop(0)
    elif args[0].startswith("-"):
        raise ValueError("env_shebang_unsupported")
    if not args:
        raise ValueError("interpreter_missing")
    command = args.pop(0)
    resolved = Path(command).expanduser() if "/" in command else Path(shutil.which(command) or "")
    if not resolved or not _executable(resolved):
        raise ValueError("interpreter_missing")
    return Path(os.path.abspath(resolved)), args


def _shebang_interpreter(executable: Path) -> tuple[Path, list[str]]:
    with executable.open("rb") as stream:
        line = stream.readline(4097)
        if line.rstrip(b"\r\n") == b"#!/bin/sh":
            # uv's quoted Python trampoline for installation paths containing spaces.
            trampoline = stream.readline(4097)
            if len(trampoline) > 4096 or stream.readline(4097).rstrip(b"\r\n") != b"' '''":
                raise ValueError("python_shebang_required")
            prefix, suffix = b"'''exec' ", b' "$0" "$@"'
            body = trampoline.rstrip(b"\r\n")
            if not body.startswith(prefix) or not body.endswith(suffix):
                raise ValueError("python_shebang_required")
            tokens = shlex.split(body[len(prefix) : -len(suffix)].decode("utf-8", "strict"))
            if len(tokens) != 1:
                raise ValueError("python_shebang_required")
            line = b"#!" + tokens[0].encode("utf-8")
    if len(line) > 4096 or not line.startswith(b"#!"):
        raise ValueError("python_shebang_required")
    raw = line[2:].decode("utf-8", "strict").strip()
    if not raw:
        raise ValueError("python_shebang_required")
    direct = Path(raw).expanduser()
    if direct.is_absolute() and _executable(direct):
        interpreter, args = Path(os.path.abspath(direct)), []
    else:
        try:
            tokens = shlex.split(raw, posix=True)
        except ValueError as exc:
            raise ValueError("python_shebang_invalid") from exc
        if not tokens:
            raise ValueError("python_shebang_required")
        if Path(tokens[0]).name == "env":
            interpreter, args = _env_shebang(tokens[1:])
        else:
            candidate = Path(tokens[0]).expanduser()
            if not candidate.is_absolute() or not _executable(candidate):
                raise ValueError("interpreter_missing")
            interpreter, args = Path(os.path.abspath(candidate)), tokens[1:]
    if not interpreter.name.lower().startswith("python"):
        raise ValueError("python_shebang_required")
    return interpreter, args


_PROBE_SOURCE = """\
import importlib.metadata as metadata
import importlib.util
import json
import pathlib
import sys
try:
    distribution = metadata.distribution('goodnotes-local-cli')
    version = distribution.version
except metadata.PackageNotFoundError:
    distribution = None
    version = None
spec = importlib.util.find_spec('goodnotes_cli')
origin = spec.origin if spec is not None else None
result = {'version': version, 'origin': origin}
if len(sys.argv) > 1 and distribution is not None:
    executable = pathlib.Path(sys.argv[1]).resolve()
    entries = [entry.value for entry in distribution.entry_points
               if entry.group == 'console_scripts' and entry.name == 'goodnotes']
    records = [record for record in distribution.files or []
               if pathlib.Path(distribution.locate_file(record)).resolve() == executable]
    record_hash = records[0].hash if len(records) == 1 else None
    result['launcher'] = {
        'entries': entries, 'records': len(records),
        'hash_mode': record_hash.mode if record_hash else None,
        'hash_value': record_hash.value if record_hash else None,
    }
print(json.dumps(result, separators=(',', ':')))
"""


def _probe_package(
    interpreter: Path, shebang_args: list[str], root: Path, executable: Path | None = None
) -> dict[str, Any]:
    try:
        try:
            parent = temporary_parent(root)
        except ValueError:
            return {"status": "blocked", "reason": "unsafe_temporary_root"}
        with tempfile.TemporaryDirectory(prefix="goodnotes-verify-probe-", dir=parent) as directory:
            environment = isolated_environment(
                {key: directory for key in ("TMPDIR", "TEMP", "TMP")}
            )
            result = subprocess.run(
                [
                    str(interpreter),
                    *shebang_args,
                    "-I",
                    "-c",
                    _PROBE_SOURCE,
                    *([str(executable)] if executable is not None else []),
                ],
                cwd=directory,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
        if result.returncode:
            return {"status": "blocked", "reason": "package_probe_failed"}
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            return {"status": "blocked", "reason": "package_not_found"}
        version = value.get("version")
        origin = value.get("origin")
        if not isinstance(version, str) or not version or not isinstance(origin, str) or not origin:
            return {"status": "blocked", "reason": "package_not_found"}
        return {
            "status": "pass",
            "version": version,
            "origin": origin,
            "launcher": value.get("launcher"),
        }
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return {"status": "blocked", "reason": "package_probe_failed"}


def _probe_cli_build(executable: Path, root: Path) -> dict[str, Any]:
    parent = temporary_parent(root)
    with tempfile.TemporaryDirectory(prefix="goodnotes-verify-cli-", dir=parent) as directory:
        result = subprocess.run(
            [str(executable), "schema"],
            cwd=directory,
            env=isolated_environment({key: directory for key in ("TMPDIR", "TEMP", "TMP")}),
            capture_output=True,
            text=True,
            check=False,
            timeout=15,
        )
    if result.returncode:
        raise ValueError("installed_cli_build_unavailable")
    value = json.loads(result.stdout)
    build = value.get("build") if isinstance(value, dict) else None
    if not isinstance(build, dict):
        raise ValueError("installed_cli_build_unavailable")
    return build


def _probe_installed_cli(executable: Path, root: Path) -> Target:
    receipt: dict[str, Any] = {"status": "blocked", "reason": "installed_target_probe_failed"}
    try:
        if inside(executable, root):
            return Target(executable, {"status": "blocked", "reason": "executable_in_checkout"})
        launcher_fingerprint = _file_fingerprint(executable)
        interpreter, shebang_args = _shebang_interpreter(executable)
        probe = _probe_package(interpreter, shebang_args, root, executable)
        if probe.get("status") != "pass":
            reason = probe.get("reason")
            if reason == "unsafe_temporary_root":
                return Target(executable, probe)
            if reason == "package_not_found":
                receipt["reason"] = "installed_package_not_found"
            return Target(executable, receipt)
        version = probe["version"]
        origin_path = Path(probe["origin"]).resolve(strict=True)
        if inside(origin_path, root):
            return Target(executable, {"status": "blocked", "reason": "package_origin_in_checkout"})
        installed_fingerprint = _python_package_fingerprint(origin_path.parent)
        source_fingerprint = _python_package_fingerprint(root / "src" / "goodnotes_cli")
        if installed_fingerprint is None or source_fingerprint is None:
            raise ValueError("installed_package_fingerprint_unavailable")
        receipt.update(
            version=version,
            import_origin="external",
            executable_fingerprint=launcher_fingerprint,
            package_fingerprint=installed_fingerprint,
            source_package_fingerprint=source_fingerprint,
        )
        if installed_fingerprint != source_fingerprint:
            raise ValueError("installed_package_out_of_sync")
        launcher = probe.get("launcher")
        if not isinstance(launcher, dict) or launcher.get("records") != 1:
            raise ValueError("installed_launcher_record_unavailable")
        if launcher.get("entries") != ["goodnotes_cli.cli:main"]:
            raise ValueError("installed_entry_point_mismatch")
        if launcher.get("hash_mode") != "sha256" or not isinstance(launcher.get("hash_value"), str):
            raise ValueError("installed_launcher_record_unavailable")
        expected_hash = urlsafe_b64encode(bytes.fromhex(launcher_fingerprint)).decode().rstrip("=")
        if launcher["hash_value"] != expected_hash:
            raise ValueError("installed_launcher_hash_mismatch")
        build = _probe_cli_build(executable, root)
        if build.get("version") != version or build.get("source_sha256") != installed_fingerprint:
            raise ValueError("installed_cli_build_mismatch")
        receipt.pop("reason")
        receipt.update(status="pass", binding_basis="distribution-record-and-schema")
        return Target(executable, receipt, interpreter.resolve(strict=True), origin_path.parent)
    except ValueError as exc:
        if str(exc) in {
            "unsafe_temporary_root",
            "installed_package_fingerprint_unavailable",
            "installed_package_out_of_sync",
            "installed_launcher_record_unavailable",
            "installed_entry_point_mismatch",
            "installed_launcher_hash_mismatch",
            "installed_cli_build_unavailable",
            "installed_cli_build_mismatch",
        }:
            receipt["reason"] = str(exc)
    except (
        OSError,
        RuntimeError,
        UnicodeError,
        subprocess.TimeoutExpired,
    ):
        pass
    return Target(executable, receipt)


def _probe_development_package(
    interpreter: Path, shebang_args: list[str], root: Path
) -> dict[str, Any]:
    try:
        probe = _probe_package(interpreter, shebang_args, root)
        if probe.get("status") != "pass":
            reason = probe.get("reason")
            if reason == "unsafe_temporary_root":
                return probe
            if reason == "package_not_found":
                return {"status": "blocked", "reason": "development_package_not_found"}
            return {"status": "blocked", "reason": "development_target_probe_failed"}
        version = probe["version"]
        origin = probe["origin"]
        origin_path = Path(origin).resolve(strict=True)
        expected_origin = (root / "src" / "goodnotes_cli" / "__init__.py").resolve(strict=True)
        if origin_path != expected_origin:
            return {"status": "blocked", "reason": "development_package_origin_mismatch"}
        if not origin_path.parent.is_dir():
            return {"status": "blocked", "reason": "development_package_unavailable"}
        return {"status": "pass", "version": version, "import_origin": "source"}
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return {"status": "blocked", "reason": "development_target_probe_failed"}


def _probe_development_cli(executable: Path, root: Path, expected_python: Path) -> Target:
    try:
        interpreter, shebang_args = _shebang_interpreter(executable)
        if interpreter.resolve(strict=True) != expected_python.resolve(strict=True):
            return Target(
                executable, {"status": "blocked", "reason": "development_interpreter_mismatch"}
            )
        fingerprint = _file_fingerprint(executable)
        receipt = _probe_development_package(interpreter, shebang_args, root)
        package = root / "src" / "goodnotes_cli"
        if receipt.get("status") == "pass":
            package_fingerprint = _python_package_fingerprint(package)
            if package_fingerprint is None:
                raise ValueError("development_package_unavailable")
            receipt.update(
                executable_fingerprint=fingerprint, package_fingerprint=package_fingerprint
            )
        return Target(executable, receipt, interpreter.resolve(strict=True), package)
    except (OSError, RuntimeError, ValueError):
        return Target(
            executable, {"status": "blocked", "reason": "development_interpreter_unavailable"}
        )


def _select_target(cli: str | None, root: Path, python: Path) -> Target:
    scope = "source_editable" if cli is None else "installed_external"
    try:
        candidate = python.with_name("goodnotes") if cli is None else Path(cli).expanduser()
        if not candidate.is_absolute() or not _executable(candidate):
            raise ValueError("executable_unavailable")
        candidate = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError):
        return Target(
            None,
            {
                "scope": scope,
                "status": "blocked",
                "reason": "development_cli_missing"
                if cli is None
                else "installed_cli_must_be_absolute_executable",
                "version": _package_version() if cli is None else None,
            },
        )
    probe = (
        _probe_development_cli(candidate, root, python)
        if cli is None
        else _probe_installed_cli(candidate, root)
    )
    return Target(
        candidate,
        {
            "scope": scope,
            **probe.receipt,
            "version": probe.receipt.get("version"),
        },
        probe.interpreter,
        probe.package,
    )


def _check_target_integrity(
    cli: str | None, root: Path, python: Path, start: Target
) -> dict[str, Any]:
    if start.receipt.get("status") != "pass":
        return {"status": "unverified"}
    changed = {"status": "fail", "unchanged": False, "reason": "target_changed"}
    try:
        selected = Path(cli).expanduser() if cli is not None else python.with_name("goodnotes")
        if selected.resolve(strict=True) != start.path:
            return changed
        if _file_fingerprint(selected) != start.receipt["executable_fingerprint"]:
            return changed
        interpreter, _ = _shebang_interpreter(selected)
        if interpreter.resolve(strict=True) != start.interpreter:
            return changed
        if start.package is not None:
            start.package.stat()
            if _python_package_fingerprint(start.package) != start.receipt["package_fingerprint"]:
                return changed
        end = _select_target(cli, root, python)
        identity_fields = (
            "version",
            "import_origin",
            "executable_fingerprint",
            "package_fingerprint",
            "source_package_fingerprint",
        )
        if any(
            end.receipt.get(key) is not None
            and start.receipt.get(key) is not None
            and end.receipt[key] != start.receipt[key]
            for key in identity_fields
        ):
            return changed
        if end.receipt.get("status") != "pass":
            if end.receipt.get("reason") in {
                "package_origin_in_checkout",
                "installed_package_out_of_sync",
                "installed_launcher_hash_mismatch",
                "installed_entry_point_mismatch",
                "installed_cli_build_mismatch",
            }:
                return changed
            return {"status": "blocked", "reason": "target_revalidation_unavailable"}
        if (end.path, end.interpreter, end.package, end.receipt) != (
            start.path,
            start.interpreter,
            start.package,
            start.receipt,
        ):
            return changed
        return {"status": "pass", "unchanged": True}
    except FileNotFoundError:
        return changed
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired):
        return {"status": "blocked", "reason": "target_revalidation_unavailable"}
