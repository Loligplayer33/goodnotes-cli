"""Protected receipt destinations and atomic report publication."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from .paths import inside
from .source import PROJECT_ROOT

SCHEMA = "goodnotes-verification/v1"
DEFAULT_REPORT = PROJECT_ROOT / ".local" / "verification" / "report.json"


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
        and inside(default, root)
        and default_relative is not None
        and default_relative.parts[:1] == (".local",)
    )
    for ancestor in (target.parent, *target.parent.parents):
        if (ancestor / "goodnotes-snapshot.json").is_file() or (ancestor / "Databases").is_dir():
            raise ValueError("report_path_protected")
    if inside(target, root):
        try:
            relative = target.relative_to(root)
        except ValueError as exc:
            raise ValueError("report_path_protected") from exc
        if not default_allowed and (not relative.parts or relative.parts[0] != ".local"):
            raise ValueError("report_path_protected")
        try:
            result = subprocess.run(
                ["git", "ls-files", "--error-unmatch", "--", relative.as_posix()],
                cwd=root,
                capture_output=True,
                check=False,
                timeout=15,
            )
        except subprocess.TimeoutExpired:
            raise ValueError("report_path_protection_unavailable") from None
        if result.returncode == 0:
            raise ValueError("report_path_protected")
        return


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
