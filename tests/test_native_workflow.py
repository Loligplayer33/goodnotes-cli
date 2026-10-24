"""Generated native-store workflows exercised through the installed CLI."""

from __future__ import annotations

import ctypes as C
import hashlib
import json
import os
import select
import signal
import sqlite3
import stat
import struct
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pymupdf as fitz
import pytest

from goodnotes_cli.cache import read_view
from goodnotes_cli.config import Settings
from goodnotes_cli.documents import render_page
from goodnotes_cli.library import Library
from goodnotes_cli.native import NativeError, NativeRuntime

NOTEBOOK = "generated-native-notebook"
PAGE_IDS = (
    "00000000-0000-4000-8000-000000000100",
    "00000000-0000-4000-8000-000000000200",
)
STORAGE_IDS = (
    "00000000-0000-4000-8000-000000000300",
    "00000000-0000-4000-8000-000000000400",
)
LOGICAL_IDS = (
    "00000000-0000-4000-8000-000000000500",
    "00000000-0000-4000-8000-000000000600",
)


def _varint(value: int) -> bytes:
    encoded = bytearray()
    while value > 127:
        encoded.append((value & 127) | 128)
        value >>= 7
    encoded.append(value)
    return bytes(encoded)


def _field(number: int, value: bytes | int) -> bytes:
    if isinstance(value, bytes):
        return _varint(number * 8 + 2) + _varint(len(value)) + value
    return _varint(number * 8) + _varint(value)


def _fixed(number: int, value: float) -> bytes:
    return _varint(number * 8 + 5) + struct.pack("<f", value)


def _stroke(
    page: int, *, offset_y: float = 0, erased: bool = False, direct_identity: bool = False
) -> dict[bytes, bytes]:
    """Encode a horizontal stroke without using the production encoder or decoder."""
    schema = b"vuA(v)A(S(uu))A(S(uuuu))\0"
    values = (
        struct.pack("<HfI", 0, 6, 0)
        + struct.pack("<Iff", 1, 55, 55)
        + struct.pack("<Iffff", 1, 55, 55, 165, 55)
    )
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(schema) + len(values)) + schema + values
    # An Apple stored block avoids making this fixture depend on a compressor.
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    rgba = b"".join(
        _fixed(index, channel)
        for index, channel in enumerate(
            (1.0, 0.0, 0.0, 1.0) if page == 0 else (0.0, 0.0, 1.0, 1.0), 1
        )
    )
    body = _field(1, LOGICAL_IDS[page].encode()) + _field(2, frame) + _field(4, rgba)
    if offset_y:
        body += _field(6, _fixed(1, 0) + _fixed(2, offset_y))
    if direct_identity:
        body += _field(20, b"")
    descriptor = (
        _field(1, LOGICAL_IDS[page].encode())
        + (_field(3, 1) if erased else b"")
        + (b"" if direct_identity else _field(6, STORAGE_IDS[page].encode()))
        + _field(8, 1)
        + _field(9, page + 1)
        + _field(16, 24)
        + (_field(14, 123) if direct_identity else b"")
    )
    root = str(UUID(int=UUID(PAGE_IDS[page]).int + 1)).upper()
    storage_id = LOGICAL_IDS[page] if direct_identity else STORAGE_IDS[page]
    prefix = f"{root}.normal.{storage_id}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(7, body)}


class GeneratedNativeStore:
    """A writable engine used only to create generated test data."""

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)

    def __init__(self, runtime: NativeRuntime, path: Path) -> None:
        self.runtime = runtime
        self.direct_identity = False
        library = runtime.library
        ptr, text, size, error = C.c_void_p, C.c_char_p, C.c_size_t, C.POINTER(C.c_void_p)
        for name, arguments, result in (
            ("open", [ptr, text, error], ptr),
            ("writeoptions_create", [], ptr),
            ("writeoptions_destroy", [ptr], None),
            ("writeoptions_set_sync", [ptr, C.c_ubyte], None),
            ("put", [ptr, ptr, text, size, text, size, error], None),
        ):
            function = getattr(library, "rocksdb_" + name)
            function.argtypes, function.restype = arguments, result
            setattr(self, name, function)
        options = runtime.options_create()
        runtime.options_set_create_if_missing(options, 1)
        failure = C.c_void_p()
        try:
            self.database = self.open(options, os.fsencode(path), C.byref(failure))
            runtime.check(failure)
        finally:
            runtime.options_destroy(options)
        assert self.database
        self.write_options = self.writeoptions_create()
        self.writeoptions_set_sync(self.write_options, 1)

    def write(self, records: dict[bytes, bytes]) -> None:
        for key, value in records.items():
            failure = C.c_void_p()
            self.put(
                self.database,
                self.write_options,
                key,
                len(key),
                value,
                len(value),
                C.byref(failure),
            )
            self.runtime.check(failure)

    def close(self) -> None:
        self.writeoptions_destroy(self.write_options)
        self.runtime.close(self.database)


@pytest.fixture
def native_runtime() -> NativeRuntime:
    try:
        return NativeRuntime()
    except NativeError as exc:
        if os.environ.get("GOODNOTES_REQUIRE_NATIVE_TESTS") == "1":
            pytest.fail(f"Native integration tests require the RocksDB runtime: {exc}")
        pytest.skip(f"Optional RocksDB runtime is unavailable: {exc}")


@pytest.fixture
def generated_source(
    tmp_path: Path, native_runtime: NativeRuntime, request: pytest.FixtureRequest
) -> Iterator[tuple[Path, GeneratedNativeStore]]:
    library = tmp_path / "generated-source" / "Library"
    databases = library / "Databases"
    attachments = library / "Attachments"
    databases.mkdir(parents=True)
    attachments.mkdir()
    with sqlite3.connect(databases / "projection.sqlite") as database:
        database.executescript("""
            CREATE TABLE folders (id TEXT PRIMARY KEY, name TEXT, updated_at REAL,
                deleted INTEGER, position TEXT);
            CREATE TABLE documents (id TEXT PRIMARY KEY, name TEXT, updated_at REAL,
                deleted INTEGER, position TEXT);
            CREATE TABLE folder_to_folder_items (id TEXT PRIMARY KEY, item_type INTEGER,
                item_id TEXT, parent_folder_id TEXT, root_folder_id TEXT,
                item_position TEXT, deleted INTEGER);
            CREATE TABLE pages (id TEXT PRIMARY KEY, document_id TEXT, template_id TEXT,
                position TEXT, group_id TEXT, deleted INTEGER);
            CREATE TABLE templates (id TEXT PRIMARY KEY, document_id TEXT, attachment_id TEXT,
                page_number INTEGER, size_width REAL, size_height REAL);
            CREATE TABLE attachments (id TEXT PRIMARY KEY, path TEXT);
            CREATE TABLE notes_items (id TEXT PRIMARY KEY, group_id TEXT, deleted INTEGER);
        """)
        database.execute(
            "INSERT INTO documents VALUES (?, 'Generated notes', 1, 0, 'a')", (NOTEBOOK,)
        )
        database.execute(
            "INSERT INTO folder_to_folder_items VALUES "
            "('generated-link', 1, ?, '', 'generated-root', 'a', 0)",
            (NOTEBOOK,),
        )
        database.execute("INSERT INTO attachments VALUES ('generated-pdf', 'generated.pdf')")
        for number, page_id in enumerate(PAGE_IDS, 1):
            template = f"generated-template-{number}"
            database.execute(
                "INSERT INTO pages VALUES (?, ?, ?, ?, '', 0)",
                (page_id, NOTEBOOK, template, str(number)),
            )
            database.execute(
                "INSERT INTO templates VALUES (?, ?, 'generated-pdf', ?, 440, 330)",
                (template, NOTEBOOK, number),
            )
    with fitz.open() as pdf:
        for number in (1, 2):
            page = pdf.new_page(width=240, height=180)
            page.insert_text((10, 160), f"Generated background {number}")
        pdf.save(attachments / "generated.pdf")
    store = GeneratedNativeStore(native_runtime, databases / "notes_main.db")
    store.direct_identity = bool(getattr(request, "param", False))
    store.write(_stroke(0, direct_identity=store.direct_identity))
    store.write(_stroke(1, offset_y=110, direct_identity=store.direct_identity))
    assert not list((databases / "notes_main.db").glob("*.sst"))
    assert any(path.stat().st_size for path in (databases / "notes_main.db").glob("*.log"))
    try:
        yield library, store
    finally:
        store.close()


def _environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GOODNOTES_SANDBOX", None)
    environment["GOODNOTES_DATA_DIR"] = str(tmp_path / "cache")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    environment["GOODNOTES_SCRATCH_DIR"] = str(scratch)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _command(arguments: list[str], environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


def _manifest(root: Path) -> tuple:
    return tuple(
        (
            str(path.relative_to(root)),
            path.lstat().st_mode,
            path.lstat().st_size,
            path.lstat().st_mtime_ns,
            path.lstat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    )


def _pixel(path: Path, x: int, y: int) -> tuple[int, ...]:
    return fitz.Pixmap(str(path)).pixel(x, y)


def _native_render(page: int, output: Path) -> list[str]:
    return [
        "render",
        NOTEBOOK,
        "--page",
        str(page),
        "--layer",
        "native",
        "--dpi",
        "72",
        "--output",
        str(output),
    ]


@pytest.mark.parametrize("generated_source", [False, True], indirect=True)
def test_native_png_pdf_bundle_preserve_sources_and_readonly_cache(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    original = _manifest(source)
    captured = _command(["--verbose", "sync", "--library", str(source)], environment)
    brief_refresh = _command(["sync", "--library", str(source)], environment)
    assert brief_refresh["snapshot_id"] == captured["snapshot_id"]
    assert brief_refresh["captured_at"] == captured["captured_at"]
    assert brief_refresh["source_changed_during_refresh"] is False
    assert "snapshot" not in brief_refresh
    assert "capture_stats" not in brief_refresh
    assert _manifest(source) == original
    cache = tmp_path / "cache"
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in [cache, *cache.rglob("*")]}
    try:
        for path, mode in sorted(modes.items(), key=lambda pair: len(pair[0].parts), reverse=True):
            path.chmod(mode & ~0o222)
        before = _manifest(cache)
        red_png = tmp_path / "red.png"
        rendered = _command(["--verbose", *_native_render(1, red_png)], environment)
        assert rendered["snapshot"] == captured["snapshot_id"]
        assert rendered["native_annotations"]["rendered_strokes"] == 1
        assert rendered["native_annotations"]["coverage"] == (
            "partial" if store.direct_identity else "supported-pen-records"
        )
        if store.direct_identity:
            assert rendered["native_annotations"]["unmodeled_descriptor_fields"] == [14]
            assert rendered["native_annotations"]["unmodeled_empty_body_fields"] == [20]
        assert rendered["native_annotations"]["full_fidelity_verified"] is False
        assert rendered["includes_handwriting"] is True
        assert _pixel(red_png, 60, 30)[:3] == (255, 0, 0)
        assert _pixel(red_png, 60, 90)[:3] == (255, 255, 255)
        output_pdf = tmp_path / "selected.pdf"
        exported = _command(
            [
                "export",
                NOTEBOOK,
                "--pages",
                "2,1",
                "--layer",
                "native",
                "--output",
                str(output_pdf),
            ],
            environment,
        )
        assert exported["selected_pages"] == [2, 1]
        assert [entry["page"] for entry in exported["native_annotations"]] == [2, 1]
        with fitz.open(output_pdf) as pdf:
            assert len(pdf) == 2
            assert "Generated background 2" in pdf[0].get_text()
            assert pdf[0].get_pixmap().pixel(60, 90)[:3] == (0, 0, 255)
            assert pdf[1].get_pixmap().pixel(60, 30)[:3] == (255, 0, 0)
        bundle = _command(
            [
                "--verbose",
                "bundle",
                NOTEBOOK,
                "--pages",
                "2",
                "--dpi",
                "72",
                "--output",
                str(tmp_path / "bundle"),
            ],
            environment,
        )
        manifest = json.loads(Path(bundle["manifest"]).read_text())
        entry = manifest["pages"][0]
        assert entry["page"] == 2
        assert entry["render"]["native_annotations"]["rendered_strokes"] == 1
        assert _pixel(Path(bundle["directory"]) / entry["image"], 60, 90)[:3] == (0, 0, 255)
        inline = _command(
            [
                "--expect-snapshot",
                captured["snapshot_id"],
                "bundle",
                NOTEBOOK,
                "--pages",
                "2,1",
                "--dpi",
                "72",
                "--max-chars",
                "15",
                "--output",
                str(tmp_path / "inline-bundle"),
            ],
            environment,
        )
        full_inline = json.loads(Path(inline["manifest"]).read_text())
        assert inline["layer"] == "native"
        assert inline["document_id"] == NOTEBOOK
        assert inline["snapshot"] == full_inline["snapshot"] == captured["snapshot_id"]
        assert inline["captured_at"] == full_inline["captured_at"]
        assert inline["text_source"] == full_inline["text_source"] == "pdf"
        assert inline["truncated"] is full_inline["truncated"] is True
        assert inline["pages"] == [2, 1]
        assert [page["page_id"] for page in inline["page_evidence"]] == [
            PAGE_IDS[1],
            PAGE_IDS[0],
        ]
        for evidence, full in zip(inline["page_evidence"], full_inline["pages"], strict=True):
            assert Path(evidence["image"]).is_absolute()
            assert evidence["parts"] == full["text"]["parts"]
            assert all(part["source"] == "pdf" for part in evidence["parts"])
            assert evidence["image_source"] == full["render"]["source"]
            assert evidence["includes_handwriting"] is True
            assert evidence["annotation_status"] == "native-reconstructed-approximate"
            assert evidence["native_annotations"]["rendered_objects"] == 1
            assert evidence["native_annotations"]["rendered_strokes"] == 1
            assert evidence["native_annotations"]["unsupported_objects"] == 0
            assert evidence["native_annotations"]["full_fidelity_verified"] is False
            assert evidence["native_annotations"]["coverage"] == (
                "partial" if store.direct_identity else "supported-pen-records"
            )
            assert set(evidence["warnings"]) == set(
                full["render"]["warnings"] + full["text"]["warnings"]
            )
            assert "descriptor_variants" in full["render"]["native_annotations"]
            assert "descriptor_variants" not in evidence["native_annotations"]
            if store.direct_identity:
                assert full["render"]["native_annotations"]["unmodeled_descriptor_fields"] == [14]
                assert any("field 14" in warning for warning in evidence["warnings"])
        assert _pixel(Path(inline["page_evidence"][0]["image"]), 60, 90)[:3] == (0, 0, 255)
        assert _pixel(Path(inline["page_evidence"][1]["image"]), 60, 30)[:3] == (255, 0, 0)
        assert _manifest(cache) == before
        assert _manifest(source) == original
        assert not list((tmp_path / "scratch").iterdir())
    finally:
        for path, mode in sorted(modes.items(), key=lambda pair: len(pair[0].parts)):
            path.chmod(mode)


def _watch_event(process: subprocess.Popen[str], *, timeout: float = 15) -> dict:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, "The watcher did not report an event before the deadline"
    line = process.stdout.readline()
    assert line, f"Watcher exited before reporting an event, return code {process.poll()}"
    event = json.loads(line)
    assert event.get("event") != "error", event
    return event


def test_native_reader_keeps_old_generation_until_render_finishes(
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", environment["GOODNOTES_SCRATCH_DIR"])
    cache = tmp_path / "cache"
    first = _command(["--verbose", "sync", "--library", str(source)], environment)
    refresher: subprocess.Popen[str] | None = None
    try:
        with read_view(Settings(data_dir=cache)) as sandbox:
            original_snapshot = _manifest(sandbox.root)
            store.write(_stroke(0, offset_y=110))
            source_after_edit = _manifest(source)
            refresher = subprocess.Popen(
                [sys.executable, "-m", "goodnotes_cli", "sync", "--library", str(source)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                completed = [
                    path
                    for path in (cache / "snapshots").iterdir()
                    if not path.name.startswith(".")
                    and (path / "goodnotes-snapshot.json").is_file()
                ]
                if len(completed) == 2:
                    break
                assert refresher.poll() is None, "Refresh exited while its old reader was pinned"
                time.sleep(0.01)
            else:
                pytest.fail("Refresh did not capture its replacement generation")
            assert refresher.poll() is None
            pointer = json.loads((cache / "active.json").read_text())
            assert pointer["snapshot_id"] == first["snapshot_id"]
            old_image = tmp_path / "pinned-old-page.png"
            with Library(sandbox) as library:
                rendered = render_page(
                    sandbox, library.pages(NOTEBOOK)[0], old_image, layer="native", dpi=72
                )
            native_metadata = rendered["native_annotations"]
            assert isinstance(native_metadata, dict)
            assert native_metadata["rendered_strokes"] == 1
            assert _pixel(old_image, 60, 30)[:3] == (255, 0, 0)
            assert _pixel(old_image, 60, 90)[:3] == (255, 255, 255)
            assert _manifest(sandbox.root) == original_snapshot
            assert _manifest(source) == source_after_edit
        stdout, stderr = refresher.communicate(timeout=15)
        assert refresher.returncode == 0, stderr
        replacement = json.loads(stdout)
        assert replacement["snapshot_id"] != first["snapshot_id"]
        assert not Path(first["snapshot"]).exists()
        fresh_image = tmp_path / "fresh-page.png"
        fresh = _command(_native_render(1, fresh_image), environment)
        assert fresh["snapshot"] == replacement["snapshot_id"]
        assert _pixel(fresh_image, 60, 90)[:3] == (255, 0, 0)
        assert not list((tmp_path / "scratch").iterdir())
    finally:
        if refresher is not None and refresher.poll() is None:
            refresher.kill()
            refresher.communicate(timeout=15)


def test_watch_refreshes_native_wal_edits_moves_and_erasure_without_manual_sync(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    watcher = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "goodnotes_cli",
            "--verbose",
            "watch",
            "--library",
            str(source),
            "--interval",
            "0.05",
            "--debounce",
            "0.05",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _watch_event(watcher)
        assert ready["event"] == "ready"
        first = tmp_path / "first.png"
        _command(_native_render(1, first), environment)
        assert _pixel(first, 60, 30)[:3] == (255, 0, 0)

        # The writer remains open. These changes are committed to the native WAL,
        # while the projection and PDF attachment stay unchanged.
        store.write(_stroke(0, offset_y=110))
        moved_event = _watch_event(watcher)
        assert moved_event["event"] == "updated"
        assert moved_event["snapshot_id"] != ready["snapshot_id"]
        assert moved_event["capture_stats"]["cache_reused_files"] >= 2
        moved = tmp_path / "moved.png"
        moved_result = _command(_native_render(1, moved), environment)
        assert moved_result["snapshot"] == moved_event["snapshot_id"]
        assert _pixel(moved, 60, 30)[:3] == (255, 255, 255)
        assert _pixel(moved, 60, 90)[:3] == (255, 0, 0)

        store.write(_stroke(0, erased=True))
        erased_event = _watch_event(watcher)
        assert erased_event["event"] == "updated"
        erased = tmp_path / "erased.png"
        erased_result = _command(_native_render(1, erased), environment)
        assert erased_result["native_annotations"]["erased_objects"] == 1
        assert erased_result["native_annotations"]["rendered_strokes"] == 0
        assert erased_result["includes_handwriting"] is False
        assert _pixel(erased, 60, 30)[:3] == (255, 255, 255)
        assert _pixel(erased, 60, 90)[:3] == (255, 255, 255)
        generations = [
            path
            for path in (tmp_path / "cache" / "snapshots").iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
        assert len(generations) == 1
    finally:
        watcher.send_signal(signal.SIGINT)
        stdout, stderr = watcher.communicate(timeout=15)
        assert watcher.returncode == 130, (watcher.returncode, stdout, stderr)
        assert stderr == ""
        assert not list((tmp_path / "cache" / "snapshots").glob(".staging-*"))
        assert not list((tmp_path / "scratch").iterdir())
