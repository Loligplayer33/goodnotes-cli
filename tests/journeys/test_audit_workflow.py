"""Generated end-to-end regressions for audited CLI behavior."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any
from uuid import UUID

import pymupdf as fitz

from scripts.verification.environment import isolated_environment
from tests.support.library import _create_snapshot
from tests.support.native_store_fixture import (
    LOGICAL_IDS,
    NOTEBOOK,
    PAGE_IDS,
    STORAGE_IDS,
)
from tests.support.native_store_fixture import (
    generated_source as _generated_source,
)
from tests.support.native_store_fixture import (
    native_runtime as _native_runtime,
)

generated_native_source = _generated_source
native_runtime = _native_runtime


def _environment(tmp_path: Path) -> dict[str, str]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return isolated_environment(
        {
            "GOODNOTES_DATA_DIR": str(tmp_path / "cache"),
            "GOODNOTES_SCRATCH_DIR": str(scratch),
            "PYTHONUNBUFFERED": "1",
        }
    )


def _run(arguments: list[str], environment: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )


def _success(arguments: list[str], environment: dict[str, str]) -> dict:
    result = _run(arguments, environment)
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


def _tree_state(root: Path) -> tuple[tuple[object, ...], ...]:
    paths = [root, *sorted(root.rglob("*"))]
    result = []
    for path in paths:
        info = path.lstat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        result.append(
            (
                str(path.relative_to(root)) if path != root else ".",
                info.st_mode,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                digest,
            )
        )
    return tuple(result)


def _generated_library(tmp_path: Path) -> Path:
    return _create_snapshot(tmp_path / "generated-view").library_dir


def _varint(value: int) -> bytes:
    result = bytearray()
    while value > 127:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, bytes):
        return _varint(number * 8 + 2) + _varint(len(value)) + value
    return _varint(number * 8) + _varint(value)


def _fixed(number: int, value: float) -> bytes:
    return _varint(number * 8 + 5) + struct.pack("<f", value)


def _two_disconnected_dots() -> dict[bytes, bytes]:
    """Replace page one's stroke with two accepted-schema starts and no segments."""
    schema = b"vuA(v)A(S(uu))A(S(uuuu))\0"
    values = (
        struct.pack("<HfI", 0, 6, 0)
        + struct.pack("<Iffff", 2, 55, 55, 165, 55)
        + struct.pack("<I", 0)
    )
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(schema) + len(values)) + schema + values
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    rgba = b"".join(_fixed(index, channel) for index, channel in enumerate((1.0, 0.0, 0.0, 1.0), 1))
    body = _field(1, LOGICAL_IDS[0].encode()) + _field(2, frame) + _field(4, rgba)
    descriptor = (
        _field(1, LOGICAL_IDS[0].encode())
        + _field(6, STORAGE_IDS[0].encode())
        + _field(8, 1)
        + _field(9, 1)
        + _field(16, 24)
    )
    root = str(UUID(int=UUID(PAGE_IDS[0]).int + 1)).upper()
    prefix = f"{root}.normal.{STORAGE_IDS[0]}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(7, body)}


def test_captured_cli_search_snippet_keeps_unicode_casefold_match_and_cache_readonly(
    tmp_path: Path,
) -> None:
    source = _generated_library(tmp_path)
    long_text = "ß" * 250 + " MATCH unicode-offset-marker " + "tail " * 30
    with sqlite3.connect(source / "Databases" / "fts.sqlite") as database:
        database.execute(
            "INSERT INTO annotation_text_content "
            "(text_id, page_id, text, engine_type, priority) VALUES (?, ?, ?, ?, ?)",
            (900, "page-02", long_text, "generated-engine", "primary"),
        )

    source_before = _tree_state(source)
    environment = _environment(tmp_path)
    capture = _success(["--verbose", "sync", "--library", str(source)], environment)
    snapshot = Path(capture["snapshot"])
    snapshot_before = _tree_state(snapshot)

    folder = _success(["--verbose", "ls", "--folder", "Shared A/Nested"], environment)
    assert folder["snapshot"] == capture["snapshot_id"]
    resolved = _success(
        ["--verbose", "info", "Shared A/Nested/quote ' ; DROP TABLE documents;--"], environment
    )
    assert resolved["id"] == "doc-hostile"
    assert resolved["snapshot"] == capture["snapshot_id"]
    pages = _success(["--verbose", "pages", "doc-hostile"], environment)
    assert pages["pages"][0]["id"] == "page-02"
    assert pages["snapshot"] == capture["snapshot_id"]
    read = _success(
        ["--verbose", "read", "doc-hostile", "--page", "1", "--source", "handwriting"],
        environment,
    )
    assert any(part["text"] == long_text for part in read["pages"][0]["parts"])
    assert read["snapshot"] == capture["snapshot_id"]
    search = _success(
        [
            "--verbose",
            "search",
            "MATCH",
            "--notebook",
            "doc-hostile",
            "--source",
            "handwriting",
        ],
        environment,
    )
    assert len(search["hits"]) == 1
    assert "MATCH" in search["hits"][0]["text"]
    assert search["hits"][0]["snippet"] is True
    assert search["snapshot"] == capture["snapshot_id"]

    assert _tree_state(source) == source_before
    assert _tree_state(snapshot) == snapshot_before
    assert not list((tmp_path / "scratch").iterdir())


def test_captured_missing_relative_pdf_keeps_diagnostic_through_scan_and_render(
    tmp_path: Path,
) -> None:
    source = _generated_library(tmp_path)
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as database:
        database.execute(
            "UPDATE attachments SET path = ? WHERE id = ?",
            ("missing/relative-background.pdf", "attachment-safe"),
        )
    source_before = _tree_state(source)
    environment = _environment(tmp_path)
    capture = _success(["--verbose", "sync", "--library", str(source)], environment)
    snapshot = Path(capture["snapshot"])
    snapshot_before = _tree_state(snapshot)

    scan = _success(
        [
            "--verbose",
            "scan-pdfs",
            "not-present",
            "--notebook",
            "doc-hostile",
            "--page-limit",
            "1",
        ],
        environment,
    )
    issue = scan["extraction_issues"][0]
    assert issue["page_id"] == "page-02"
    assert issue["issue"] == "missing-attachment"
    assert issue["detail"] == "The mapped copied attachment is missing from the sandbox."
    assert scan["snapshot"] == capture["snapshot_id"]

    output = tmp_path / "missing-background.png"
    failed = _run(
        [
            "--verbose",
            "render",
            "doc-hostile",
            "--page",
            "1",
            "--layer",
            "background",
            "--output",
            str(output),
        ],
        environment,
    )
    assert failed.returncode == 1
    assert failed.stdout == ""
    error = json.loads(failed.stderr)["error"]
    assert "background attachment for page 1 is missing from the sandbox copy" in error["message"]
    assert not output.exists()

    assert _tree_state(source) == source_before
    assert _tree_state(snapshot) == snapshot_before
    assert not list((tmp_path / "scratch").iterdir())


def test_native_cli_keeps_disconnected_dots_separate_and_cache_readonly(
    generated_native_source: tuple[Path, Any], tmp_path: Path
) -> None:
    source, store = generated_native_source
    store.write(_two_disconnected_dots())
    source_before = _tree_state(source)
    environment = _environment(tmp_path)
    capture = _success(["--verbose", "sync", "--library", str(source)], environment)
    snapshot = Path(capture["snapshot"])
    snapshot_before = _tree_state(snapshot)
    image = tmp_path / "disconnected-dots.png"

    result = _success(
        [
            "--verbose",
            "render",
            NOTEBOOK,
            "--page",
            "1",
            "--layer",
            "native",
            "--dpi",
            "72",
            "--output",
            str(image),
        ],
        environment,
    )
    assert result["snapshot"] == capture["snapshot_id"]
    assert result["native_annotations"]["rendered_objects"] == 1
    assert result["native_annotations"]["rendered_strokes"] == 2
    pixmap = fitz.Pixmap(str(image))
    assert pixmap.pixel(30, 30)[:3] == (255, 0, 0)
    assert pixmap.pixel(90, 30)[:3] == (255, 0, 0)
    assert pixmap.pixel(60, 30)[:3] == (255, 255, 255)

    assert _tree_state(source) == source_before
    assert _tree_state(snapshot) == snapshot_before
    assert not list((tmp_path / "scratch").iterdir())
