"""Generated acceptance subprocess execution and safe receipt metrics."""

from __future__ import annotations

import json
import math
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REQUIRED_SUITES = (
    "journeys/test_study_journeys.py",
    "verification/test_verification_reference.py",
)

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
