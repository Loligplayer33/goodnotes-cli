"""Generated native records and a writable engine for synthetic integration fixtures."""

from __future__ import annotations

import ctypes as C
import os
import sqlite3
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pymupdf as fitz
import pytest

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
IMAGE_LOGICAL_ID = "00000000-0000-4000-8000-000000000700"
IMAGE_RESOURCE_ID = "00000000-0000-4000-8000-000000000800"
IMAGE_ATTACHMENT_ID = "00000000-0000-4000-8000-000000000900"


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


def _native_image(page: int, *, sequence: int = 2) -> dict[bytes, bytes]:
    def point(x: float, y: float) -> bytes:
        return _fixed(1, x) + _fixed(2, y)

    def rectangle(x: float, y: float, width: float, height: float) -> bytes:
        return _field(1, point(x, y)) + _field(2, point(width, height))

    x, y, width, height = 110.0, 55.0, 55.0, 55.0
    body = (
        _field(1, IMAGE_LOGICAL_ID.encode())
        + _field(2, rectangle(x, y, width, height))
        + _field(3, rectangle(x + width / 2, y + height / 2, width, height))
        + _field(4, IMAGE_RESOURCE_ID.encode())
        + _field(5, b"")
        + _field(6, 1)
        + _field(15, b"")
        + _field(18, 24)
    )
    descriptor = (
        _field(1, IMAGE_LOGICAL_ID.encode())
        + _field(4, IMAGE_RESOURCE_ID.encode())
        + _field(7, IMAGE_ATTACHMENT_ID.encode())
        + _field(8, 1)
        + _field(9, sequence)
        + _field(16, 24)
    )
    root = str(UUID(int=UUID(PAGE_IDS[page]).int + 1)).upper()
    prefix = f"{root}.normal.{IMAGE_LOGICAL_ID}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(1, body)}


def _solid_png(color: tuple[int, int, int], *, width: int = 8, height: int = 8) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    pixels = bytes(color) * width
    raw = b"".join(b"\0" + pixels for _ in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _add_image_attachment(source: Path, data: bytes) -> None:
    (source / "Attachments" / "generated-image.png").write_bytes(data)
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as database:
        database.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, 1)",
            (IMAGE_ATTACHMENT_ID, "generated-image.png", NOTEBOOK),
        )


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
        self._closed = False
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
        if self._closed:
            return
        self._closed = True
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
            CREATE TABLE attachments (id TEXT PRIMARY KEY, path TEXT,
                document_id TEXT, available INTEGER);
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
        database.execute(
            "INSERT INTO attachments VALUES ('generated-pdf', 'generated.pdf', ?, 1)",
            (NOTEBOOK,),
        )
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
    parameter = getattr(request, "param", False)
    image_only = bool(parameter.get("image_only", False)) if isinstance(parameter, dict) else False
    store.direct_identity = (
        bool(parameter.get("direct_identity", False))
        if isinstance(parameter, dict)
        else bool(parameter)
    )
    if not image_only:
        store.write(_stroke(0, direct_identity=store.direct_identity))
    store.write(_stroke(1, offset_y=110, direct_identity=store.direct_identity))
    assert not list((databases / "notes_main.db").glob("*.sst"))
    assert any(path.stat().st_size for path in (databases / "notes_main.db").glob("*.log"))
    try:
        yield library, store
    finally:
        store.close()
