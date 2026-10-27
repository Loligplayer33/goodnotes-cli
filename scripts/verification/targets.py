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
from pathlib import Path
from typing import Any

from .environment import isolated_environment
from .paths import inside, temporary_parent


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
try:
    version = metadata.version('goodnotes-local-cli')
except metadata.PackageNotFoundError:
    version = None
spec = importlib.util.find_spec('goodnotes_cli')
origin = spec.origin if spec is not None else None
print(json.dumps({'version': version, 'origin': origin}, separators=(',', ':')))
"""


def _probe_package(interpreter: Path, shebang_args: list[str], root: Path) -> dict[str, Any]:
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
                [str(interpreter), *shebang_args, "-I", "-c", _PROBE_SOURCE],
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
        return {"status": "pass", "version": version, "origin": origin}
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return {"status": "blocked", "reason": "package_probe_failed"}


def _probe_installed_cli(executable: Path, root: Path) -> dict[str, Any]:
    try:
        interpreter, shebang_args = _shebang_interpreter(executable)
        probe = _probe_package(interpreter, shebang_args, root)
        if probe.get("status") != "pass":
            reason = probe.get("reason")
            if reason == "unsafe_temporary_root":
                return probe
            if reason == "package_not_found":
                return {"status": "blocked", "reason": "installed_package_not_found"}
            return {"status": "blocked", "reason": "installed_target_probe_failed"}
        version = probe["version"]
        origin_path = Path(probe["origin"]).resolve()
        if inside(origin_path, root):
            return {"status": "blocked", "reason": "package_origin_in_checkout"}
        installed_fingerprint = _python_package_fingerprint(origin_path.parent)
        source_fingerprint = _python_package_fingerprint(root / "src" / "goodnotes_cli")
        if installed_fingerprint is None or source_fingerprint is None:
            return {"status": "blocked", "reason": "installed_package_fingerprint_unavailable"}
        if installed_fingerprint != source_fingerprint:
            return {
                "status": "blocked",
                "reason": "installed_package_out_of_sync",
                "version": version,
                "import_origin": "external",
                "package_fingerprint": installed_fingerprint,
                "source_package_fingerprint": source_fingerprint,
            }
        return {
            "status": "pass",
            "version": version,
            "import_origin": "external",
            "package_fingerprint": installed_fingerprint,
            "source_package_fingerprint": source_fingerprint,
        }
    except (
        OSError,
        RuntimeError,
        ValueError,
        UnicodeError,
        json.JSONDecodeError,
        subprocess.TimeoutExpired,
    ):
        return {"status": "blocked", "reason": "installed_target_probe_failed"}


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


def _probe_development_cli(executable: Path, root: Path, expected_python: Path) -> dict[str, Any]:
    try:
        interpreter, shebang_args = _shebang_interpreter(executable)
        if interpreter.resolve(strict=True) != expected_python.resolve(strict=True):
            return {"status": "blocked", "reason": "development_interpreter_mismatch"}
    except (OSError, RuntimeError, ValueError):
        return {"status": "blocked", "reason": "development_interpreter_unavailable"}
    return _probe_development_package(interpreter, shebang_args, root)


def _select_target(cli: str | None, root: Path, python: Path) -> tuple[Path | None, dict[str, Any]]:
    if cli is None:
        candidate = python.with_name("goodnotes")
        if not _executable(candidate):
            return None, {
                "scope": "source_editable",
                "status": "blocked",
                "reason": "development_cli_missing",
                "version": _package_version(),
            }
        candidate = candidate.resolve()
        probe = _probe_development_cli(candidate, root, python)
        return candidate, {
            "scope": "source_editable",
            **probe,
            "version": probe.get("version"),
        }
    candidate = Path(cli).expanduser()
    if not candidate.is_absolute() or not _executable(candidate):
        return None, {
            "scope": "installed_external",
            "status": "blocked",
            "reason": "installed_cli_must_be_absolute_executable",
            "version": None,
        }
    candidate = candidate.resolve(strict=True)
    probe = _probe_installed_cli(candidate, root)
    return candidate, {
        "scope": "installed_external",
        **probe,
        "version": probe.get("version"),
    }
