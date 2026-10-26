"""Run the generated acceptance suites and emit a compact verification receipt."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(PROJECT_ROOT))
from goodnotes_cli.safety import path_contains  # noqa: E402
from scripts.verification_paths import temporary_parent  # noqa: E402

SCHEMA = "goodnotes-verification/v1"
REQUIRED_SUITES = ("test_study_journeys.py", "test_verification_reference.py")
DEFAULT_REPORT = PROJECT_ROOT / ".local" / "verification" / "report.json"
_IGNORED_PARTS = {
    ".cache",
    ".git",
    ".hypothesis",
    ".local",
    ".mypy_cache",
    ".private-corpus",
    ".pytest_cache",
    ".ruff_cache",
    ".ty_cache",
    ".venv",
    "__pycache__",
    "caches",
    "privatecorpus",
    "private-corpus",
    "private_corpus",
}
_UNTRACKED_SUFFIXES = {".cfg", ".ini", ".json", ".md", ".py", ".sh", ".toml", ".yaml", ".yml"}
_UNTRACKED_NAMES = {".gitignore", "Makefile", "justfile"}
_SAFE_COMMANDS = {
    "attach-pdf",
    "bundle",
    "doctor",
    "export",
    "info",
    "ls",
    "pages",
    "read",
    "render",
    "scan-pdfs",
    "schema",
    "search",
    "snapshot",
    "sync",
    "tree",
    "watch",
}
_HEX_SHA = re.compile(r"\A[0-9a-f]{40,64}\Z")


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _inside(path: Path, parent: Path) -> bool:
    try:
        return path_contains(parent, path)
    except (OSError, RuntimeError, ValueError):
        return True


def _run_git(root: Path, *arguments: str) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=root, capture_output=True, check=False, timeout=15
    )
    if result.returncode:
        raise RuntimeError("git_unavailable")
    return result.stdout


def _hash_path(hasher: Any, root: Path, relative: bytes, marker: bytes) -> None:
    name_parts = relative.split(b"/")
    if any(os.fsdecode(part) in _IGNORED_PARTS for part in name_parts):
        return
    path = root / os.fsdecode(relative)
    hasher.update(len(relative).to_bytes(8, "big"))
    hasher.update(relative)
    hasher.update(marker)
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            hasher.update(b"L")
            hasher.update(os.fsencode(os.readlink(path)))
        elif stat.S_ISREG(info.st_mode):
            hasher.update(b"F")
            with path.open("rb") as source:
                for chunk in iter(lambda: source.read(1024 * 1024), b""):
                    hasher.update(chunk)
        else:
            hasher.update(b"O")
    except FileNotFoundError:
        hasher.update(b"MISSING")
    except OSError as exc:
        raise RuntimeError("source_unreadable") from exc


def _source_snapshot(root: Path) -> dict[str, Any]:
    root = root.resolve()
    commit = _run_git(root, "rev-parse", "--verify", "HEAD").decode("ascii", "strict").strip()
    if not _HEX_SHA.fullmatch(commit):
        raise RuntimeError("git_revision_unavailable")
    status = _run_git(root, "status", "--porcelain", "--untracked-files=all")
    tracked_paths = set(_run_git(root, "ls-files", "--cached", "-z").split(b"\0")) - {b""}
    tracked = _run_git(root, "ls-files", "--cached", "-z")
    untracked = _run_git(root, "ls-files", "--others", "--exclude-standard", "-z")
    hasher = hashlib.sha256()
    for relative in sorted(set(tracked.split(b"\0") + untracked.split(b"\0")) - {b""}):
        is_tracked = relative in tracked_paths
        if not is_tracked:
            basename = os.fsdecode(relative.rsplit(b"/", 1)[-1])
            if (
                Path(basename).suffix.lower() not in _UNTRACKED_SUFFIXES
                and basename not in _UNTRACKED_NAMES
            ):
                continue
        _hash_path(hasher, root, relative, b"T" if is_tracked else b"U")
    return {
        "git_commit": commit,
        "dirty": bool(status.strip()),
        "status_fingerprint": hashlib.sha256(status).hexdigest(),
        "content_fingerprint": hasher.hexdigest(),
    }


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
            environment = os.environ.copy()
            environment.pop("PYTHONPATH", None)
            environment.pop("PYTHONHOME", None)
            for key in ("TMPDIR", "TEMP", "TMP"):
                environment[key] = directory
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
        if _inside(origin_path, root):
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
        package_fingerprint = _python_package_fingerprint(origin_path.parent)
        source_fingerprint = _python_package_fingerprint(root / "src" / "goodnotes_cli")
        if package_fingerprint is None or source_fingerprint is None:
            return {"status": "blocked", "reason": "development_package_unavailable"}
        if package_fingerprint != source_fingerprint:
            return {"status": "blocked", "reason": "development_package_out_of_sync"}
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


def _empty_suite(status: str, reason: str) -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "collected": 0,
        "passed": 0,
        "failures": 0,
        "errors": 0,
        "skipped": 0,
        "duration_seconds": 0.0,
        "return_code": None,
    }


def _blocked_acceptance(reason: str) -> dict[str, Any]:
    return {
        "status": "blocked",
        "reason": reason,
        "suites": {name: _empty_suite("blocked", reason) for name in REQUIRED_SUITES},
    }


def _junit_counts(path: Path) -> tuple[int, int, int, int]:
    root = ET.parse(path).getroot()
    cases = list(root.iter("testcase"))
    failed = errors = skipped = 0
    for case in cases:
        failed += bool(case.find("failure") is not None)
        errors += bool(case.find("error") is not None)
        skipped += bool(case.find("skipped") is not None)
    return len(cases), int(failed), int(errors), int(skipped)


def _run_test_suite(
    root: Path,
    suite_name: str,
    python: Path,
    environment: dict[str, str],
    junit_path: Path,
) -> dict[str, Any]:
    suite_path = root / "tests" / suite_name
    if not suite_path.is_file():
        return _empty_suite("blocked", "required_suite_missing")
    started = time.monotonic()
    try:
        process = subprocess.run(
            [
                str(python),
                "-m",
                "pytest",
                "-q",
                "-o",
                "addopts=",
                "--junitxml",
                str(junit_path),
                str(suite_path),
            ],
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        value = _empty_suite("blocked", "required_suite_timeout")
        value["duration_seconds"] = round(time.monotonic() - started, 3)
        return value
    except OSError:
        value = _empty_suite("blocked", "required_suite_could_not_start")
        value["duration_seconds"] = round(time.monotonic() - started, 3)
        return value
    duration = round(time.monotonic() - started, 3)
    try:
        collected, failures, errors, skipped = _junit_counts(junit_path)
    except (OSError, ET.ParseError):
        value = _empty_suite("blocked", "required_suite_report_missing")
        value["duration_seconds"] = duration
        value["return_code"] = process.returncode
        return value
    if failures or errors or (process.returncode not in {0, 5}):
        status, reason = "fail", "required_suite_failed"
    elif process.returncode == 5 or collected == 0:
        status, reason = "blocked", "required_suite_collected_no_tests"
    elif skipped:
        status, reason = "blocked", "required_suite_skipped_tests"
    else:
        status, reason = "pass", None
    return {
        "status": status,
        **({"reason": reason} if reason else {}),
        "collected": collected,
        "passed": max(0, collected - failures - errors - skipped),
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "duration_seconds": duration,
        "return_code": process.returncode,
    }


def _aggregate_status(checks: list[dict[str, Any]]) -> str:
    statuses = [check.get("status") for check in checks]
    if "fail" in statuses:
        return "fail"
    if "blocked" in statuses:
        return "blocked"
    if statuses and all(status == "pass" for status in statuses):
        return "pass"
    return "unverified"


def _read_metrics(path: Path) -> dict[str, Any]:
    empty: dict[str, Any] = {
        "status": "unverified",
        "journey_count": 0,
        "tool_calls": 0,
        "stdout_bytes": 0,
        "journeys": [],
    }
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 4_000_000:
            return empty
        data = json.loads(path.read_text(encoding="utf-8"))
        journeys = data.get("journeys") if isinstance(data, dict) else None
        if not isinstance(journeys, list):
            return empty
        safe_journeys: list[dict[str, Any]] = []
        total_calls = total_bytes = 0
        for index, journey in enumerate(journeys[:100], start=1):
            if not isinstance(journey, dict):
                return empty
            duration = journey.get("duration_seconds")
            calls = journey.get("tool_calls")
            byte_count = journey.get("stdout_bytes")
            requests = journey.get("requests")
            if (
                isinstance(duration, bool)
                or not isinstance(duration, (int, float))
                or not math.isfinite(duration)
                or duration < 0
                or type(calls) is not int
                or calls < 0
                or type(byte_count) is not int
                or byte_count < 0
                or not isinstance(requests, list)
                or len(requests) > 100
            ):
                return empty
            safe_requests: list[dict[str, Any]] = []
            for request in requests:
                if not isinstance(request, dict) or not isinstance(request.get("pages"), list):
                    return empty
                pages = request["pages"]
                if any(type(page) is not int or page < 1 for page in pages):
                    return empty
                command = request.get("command")
                safe_command = command if command in _SAFE_COMMANDS else "other"
                safe_requests.append({"command": safe_command, "pages": pages[:100]})
            safe_journeys.append(
                {
                    "journey": f"journey_{index}",
                    "duration_seconds": round(float(duration), 3),
                    "tool_calls": calls,
                    "stdout_bytes": byte_count,
                    "requests": safe_requests,
                }
            )
            total_calls += calls
            total_bytes += byte_count
        return {
            "status": "pass" if safe_journeys else "unverified",
            "journey_count": len(safe_journeys),
            "tool_calls": total_calls,
            "stdout_bytes": total_bytes,
            "journeys": safe_journeys,
        }
    except (OSError, UnicodeError, ValueError, TypeError):
        return empty


def _private_result(manifest: Path, cli: Path, root: Path) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": "blocked",
        "manifest_sha256": None,
        "cases": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "comparisons": 0,
    }
    try:
        if (
            manifest.is_symlink()
            or not manifest.is_file()
            or manifest.stat().st_size > 256_000
            or _inside(manifest, root)
        ):
            result["blocked"] = 1
            return result
        hasher = hashlib.sha256()
        with manifest.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        result["manifest_sha256"] = hasher.hexdigest()
        from scripts.verification_reference import check_private_corpus

        aggregate = check_private_corpus(manifest, cli)
        if not isinstance(aggregate, dict):
            result["blocked"] = 1
            return result
        status = aggregate.get("status")
        result["status"] = status if status in {"pass", "fail", "blocked"} else "blocked"
        for key in ("cases", "passed", "failed", "blocked", "comparisons"):
            value = aggregate.get(key)
            if type(value) is int and value >= 0:
                result[key] = value
            else:
                result["status"] = "blocked"
                result[key] = 0
        fingerprint = aggregate.get("corpus_fingerprint")
        if isinstance(fingerprint, str) and re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            result["corpus_fingerprint"] = fingerprint
        reason = aggregate.get("reason")
        if reason == "corpus_changed":
            result["reason"] = reason
        return result
    except Exception:
        result["blocked"] = 1
        return result


def _normal_report_path(path: Path) -> Path:
    raw = path.expanduser()
    parent = raw.parent if raw.is_absolute() else Path.cwd() / raw.parent
    return parent.resolve() / raw.name


def _protect_report_destination(target: Path, root: Path) -> None:
    root = root.resolve()
    default = _normal_report_path(root / ".local" / "verification" / "report.json")
    try:
        default_relative = default.relative_to(root)
    except ValueError:
        default_relative = None
    default_allowed = (
        target == default
        and not (root / ".local").is_symlink()
        and _inside(default, root)
        and default_relative is not None
        and default_relative.parts[:1] == (".local",)
    )
    for ancestor in (target.parent, *target.parent.parents):
        if (ancestor / "goodnotes-snapshot.json").is_file() or (ancestor / "Databases").is_dir():
            raise ValueError("report_path_protected")
    if _inside(target, root):
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError("report_path_protected") from exc
        if not default_allowed and (not relative.parts or relative.parts[0] != ".local"):
            raise ValueError("report_path_protected")
        result = subprocess.run(
            ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
            cwd=root,
            capture_output=True,
            check=False,
            timeout=15,
        )
        if result.returncode == 0:
            raise ValueError("report_path_protected")
        return
    if default_allowed:
        raise ValueError("report_path_symlink_parent")


def _write_report(path: Path, receipt: dict[str, Any], root: Path) -> None:
    target = _normal_report_path(path)
    _protect_report_destination(target, root)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError("report_path_is_symlink")
    if target.exists():
        if not target.is_file():
            raise ValueError("report_path_not_regular")
        try:
            if target.stat().st_size > 1_000_000:
                raise ValueError("report_path_not_receipt")
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("report_path_not_receipt") from exc
        if not isinstance(old, dict) or old.get("schema") != SCHEMA:
            raise ValueError("report_path_not_receipt")
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_name = temporary.name
            json.dump(receipt, temporary, separators=(",", ":"), sort_keys=True)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        if target.is_symlink():
            raise ValueError("report_path_is_symlink")
        os.replace(temporary_name, target)
    except Exception:
        if temporary_name is not None:
            try:
                Path(temporary_name).unlink(missing_ok=True)
            except OSError:
                pass
        raise


def run_verification(
    *,
    root: Path = PROJECT_ROOT,
    cli: str | None = None,
    private_corpus: Path | None = None,
    report: Path | None = None,
    python_executable: Path | None = None,
) -> tuple[dict[str, Any], int]:
    root = root.resolve()
    python = Path(os.path.abspath(python_executable or Path(sys.executable)))
    report_path = report if report is not None else root / ".local" / "verification" / "report.json"
    started_at = _utc_now()
    started = time.monotonic()
    try:
        start_source = _source_snapshot(root)
        provenance_error = None
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        start_source = {
            "git_commit": None,
            "dirty": None,
            "status_fingerprint": None,
            "content_fingerprint": None,
        }
        provenance_error = "source_provenance_unavailable"

    target_path, target = _select_target(cli, root, python)
    checks: dict[str, Any] = {
        "generated_acceptance": {"status": "blocked", "suites": {}},
        "private_corpus": {
            "status": "unverified",
            "manifest_sha256": None,
            "cases": None,
            "passed": None,
            "failed": None,
            "blocked": None,
            "comparisons": None,
        },
        "tree_integrity": {"status": "blocked", "reason": provenance_error or "not_checked"},
    }
    metrics: dict[str, Any] = {
        "status": "unverified",
        "journey_count": 0,
        "tool_calls": 0,
        "stdout_bytes": 0,
        "journeys": [],
    }
    if target_path is not None and target.get("status") == "pass":
        try:
            temp_parent = temporary_parent(root)
        except ValueError:
            checks["generated_acceptance"] = _blocked_acceptance("unsafe_temporary_root")
        else:
            try:
                temporary = tempfile.TemporaryDirectory(
                    prefix="goodnotes-verification-", dir=temp_parent
                )
            except OSError:
                checks["generated_acceptance"] = _blocked_acceptance(
                    "temporary_directory_unavailable"
                )
            else:
                with temporary as directory:
                    temp_root = Path(directory).resolve()
                    metrics_path = temp_root / "metrics.json"
                    environment = os.environ.copy()
                    for key in (
                        "PYTHONPATH",
                        "PYTHONHOME",
                        "PYTEST_ADDOPTS",
                        "PYTEST_PLUGINS",
                        "GOODNOTES_REQUIRE_NATIVE_TESTS",
                    ):
                        environment.pop(key, None)
                    for key in ("TMPDIR", "TEMP", "TMP"):
                        environment[key] = str(temp_root)
                    environment["GOODNOTES_VERIFY_CLI"] = str(target_path)
                    environment["GOODNOTES_VERIFY_METRICS"] = str(metrics_path)
                    suites: dict[str, Any] = {}
                    for index, name in enumerate(REQUIRED_SUITES):
                        junit_path = temp_root / f"suite-{index}.xml"
                        suites[name] = _run_test_suite(root, name, python, environment, junit_path)
                    checks["generated_acceptance"] = {
                        "status": _aggregate_status(list(suites.values())),
                        "suites": suites,
                    }
                    metrics = _read_metrics(metrics_path)
    else:
        reason = target.get("reason", "target_unavailable")
        checks["generated_acceptance"] = {
            "status": "blocked",
            "reason": reason,
            "suites": {name: _empty_suite("blocked", reason) for name in REQUIRED_SUITES},
        }

    if private_corpus is not None:
        if target_path is None or target.get("status") != "pass":
            checks["private_corpus"] = {
                "status": "blocked",
                "manifest_sha256": None,
                "cases": 0,
                "passed": 0,
                "failed": 0,
                "blocked": 1,
                "comparisons": 0,
            }
        else:
            checks["private_corpus"] = _private_result(
                private_corpus.expanduser(), target_path, root
            )

    try:
        end_source = _source_snapshot(root)
        tree_unchanged = (
            start_source["content_fingerprint"] is not None
            and start_source["content_fingerprint"] == end_source["content_fingerprint"]
            and start_source["git_commit"] == end_source["git_commit"]
            and start_source["status_fingerprint"] == end_source["status_fingerprint"]
        )
        checks["tree_integrity"] = {
            "status": "pass" if tree_unchanged else "fail",
            "unchanged": tree_unchanged,
        }
    except (OSError, RuntimeError, UnicodeError, subprocess.TimeoutExpired):
        end_source = {
            "git_commit": None,
            "dirty": None,
            "status_fingerprint": None,
            "content_fingerprint": None,
        }
        tree_unchanged = False
        checks["tree_integrity"] = {"status": "blocked", "reason": "source_provenance_unavailable"}

    statuses = [
        target.get("status"),
        checks["generated_acceptance"]["status"],
        checks["tree_integrity"]["status"],
    ]
    if private_corpus is not None:
        statuses.append(checks["private_corpus"]["status"])
    overall = _aggregate_status([{"status": status} for status in statuses])
    exit_code = 0 if overall == "pass" and tree_unchanged else 1
    receipt: dict[str, Any] = {
        "schema": SCHEMA,
        "status": overall,
        "exit_code": exit_code,
        "started_at": started_at,
        "finished_at": _utc_now(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "provenance": {
            "git_commit": start_source["git_commit"],
            "dirty": start_source["dirty"],
            "dirty_end": end_source["dirty"],
            "git_commit_end": end_source["git_commit"],
            "content_fingerprint_start": start_source["content_fingerprint"],
            "content_fingerprint_end": end_source["content_fingerprint"],
        },
        "runtime": {"python": platform.python_version(), "platform": platform.platform()},
        "target": target,
        "selection": {
            "required_suites": list(REQUIRED_SUITES),
            "private_corpus_requested": private_corpus is not None,
        },
        "checks": checks,
        "journey_metrics": metrics,
        "report": {"status": "written"},
    }
    try:
        _write_report(report_path, receipt, root)
    except (OSError, ValueError, TypeError):
        receipt["status"] = "fail"
        receipt["exit_code"] = 1
        receipt["report"] = {"status": "failed", "reason": "report_write_failed"}
        exit_code = 1
    return receipt, exit_code


def _arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Goodnotes CLI generated verification suites.")
    parser.add_argument("--cli", help="absolute installed goodnotes executable to verify")
    parser.add_argument(
        "--private-corpus", type=Path, help="explicit outside-repository corpus manifest"
    )
    parser.add_argument("--report", type=Path, help="receipt destination")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    options = _arguments(argv)
    receipt, code = run_verification(
        cli=options.cli,
        private_corpus=options.private_corpus,
        report=options.report,
    )
    sys.stdout.write(json.dumps(receipt, separators=(",", ":"), sort_keys=True) + "\n")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
