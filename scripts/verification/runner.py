"""Coordinate verification selection, suites, provenance and receipt output."""

from __future__ import annotations

import hashlib
import os
import platform
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from . import source
from .environment import isolated_environment
from .paths import inside, temporary_parent
from .report import SCHEMA, _write_report
from .source import _source_snapshot, _utc_now
from .suites import (
    REQUIRED_SUITES,
    _aggregate_status,
    _blocked_acceptance,
    _empty_suite,
    _read_metrics,
    _run_test_suite,
)
from .targets import _select_target


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
            or inside(manifest, root)
        ):
            result["blocked"] = 1
            return result
        hasher = hashlib.sha256()
        with manifest.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        result["manifest_sha256"] = hasher.hexdigest()
        from scripts.verification.reference import check_private_corpus

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


def run_verification(
    *,
    root: Path = source.PROJECT_ROOT,
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
                    environment = isolated_environment(
                        {
                            **{key: str(temp_root) for key in ("TMPDIR", "TEMP", "TMP")},
                            "GOODNOTES_VERIFY_CLI": str(target_path),
                            "GOODNOTES_VERIFY_METRICS": str(metrics_path),
                        }
                    )
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
    except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
        receipt["status"] = "fail"
        receipt["exit_code"] = 1
        receipt["report"] = {"status": "failed", "reason": "report_write_failed"}
        exit_code = 1
    return receipt, exit_code
