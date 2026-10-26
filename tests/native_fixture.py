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
VARIANT_LOGICAL_ID = "00000000-0000-4000-8000-000000000a01"
VARIANT_STORAGE_ID = "00000000-0000-4000-8000-000000000a02"
VARIANT_RESOURCE_ID = "00000000-0000-4000-8000-000000000a03"
VARIANT_ATTACHMENT_ID = "00000000-0000-4000-8000-000000000a04"
UNSUPPORTED_PAGE_ID = "00000000-0000-4000-8000-000000000a10"

GENERATED_NATIVE_VARIANT_CASES = frozenset(
    {
        "image-body-missing-field-6",
        "descriptor-resource-without-attachment",
        "observed-combined-image-shape",
        "alternate-shape-envelope",
        "shape-with-transform",
    }
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


def _float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def _rgba(red: float, green: float, blue: float, alpha: float) -> bytes:
    return b"".join(
        _fixed(number, value) for number, value in enumerate((red, green, blue, alpha), 1)
    )


def _native_record_pair(
    page_id: str,
    body: bytes,
    *,
    object_type: int = 24,
    logical_id: str = VARIANT_LOGICAL_ID,
    storage_id: str = VARIANT_STORAGE_ID,
    storage_alias: bool = True,
    descriptor_extra: bytes = b"",
    sequence: int = 2,
) -> dict[bytes, bytes]:
    root = str(UUID(int=UUID(page_id).int + 1)).upper()
    key_storage_id = storage_id if storage_alias else logical_id
    descriptor = (
        _field(1, logical_id.encode())
        + (_field(6, storage_id.encode()) if storage_alias else b"")
        + _field(8, 1)
        + _field(9, sequence)
        + _field(16, object_type)
        + descriptor_extra
    )
    prefix = f"{root}.normal.{key_storage_id}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(7, body)}


def _native_ink_records(
    page_id: str,
    *,
    points: tuple[tuple[float, float], ...],
    width: float,
    color: tuple[float, float, float, float],
    translation: tuple[float, float] = (0.0, 0.0),
    highlighter_style: bool = False,
    direct_identity: bool = False,
) -> dict[bytes, bytes]:
    """Encode a generated pen/highlighter body from known point and style values."""
    if len(points) < 2:
        raise ValueError("Generated native ink requires at least two points")
    schema = b"vuA(v)A(S(uu))A(S(uuuu))\0"
    values = struct.pack("<HfI", 0, width, 0)
    values += struct.pack("<Iff", 1, *points[0])
    segments = tuple(zip(points[:-1], points[1:], strict=True))
    values += struct.pack("<I", len(segments))
    values += b"".join(struct.pack("<ffff", *start, *end) for start, end in segments)
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(schema) + len(values)) + schema + values
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    body = _field(1, VARIANT_LOGICAL_ID.encode()) + _field(2, frame) + _field(4, _rgba(*color))
    if highlighter_style:
        body += _field(3, 5) + _field(5, 1)
    if translation != (0.0, 0.0):
        body += _field(6, _fixed(1, translation[0]) + _fixed(2, translation[1]))
    if direct_identity:
        body += _field(20, b"")
    return _native_record_pair(
        page_id,
        body,
        storage_alias=not direct_identity,
        logical_id=VARIANT_LOGICAL_ID,
        storage_id=VARIANT_STORAGE_ID,
        descriptor_extra=_field(14, 123) if direct_identity else b"",
    )


def _native_filled_path_records(page_id: str) -> dict[bytes, bytes]:
    """Encode an independent filled-path fixture with four straight cubic edges."""
    format_string = b"vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"

    def array_u(values: tuple[int, ...]) -> bytes:
        return struct.pack("<I", len(values)) + b"".join(
            struct.pack("<I", value) for value in values
        )

    def array_v(values: tuple[int, ...]) -> bytes:
        return struct.pack("<I", len(values)) + b"".join(
            struct.pack("<H", value) for value in values
        )

    cubic_values = (
        160,
        90,
        170,
        90,
        180,
        90,
        180,
        100,
        180,
        110,
        180,
        120,
        170,
        120,
        160,
        120,
        150,
        120,
        150,
        110,
        150,
        100,
        150,
        90,
    )
    values = struct.pack("<H", 2)
    values += array_v((0, 1))
    values += array_u(tuple(_float_bits(value) for value in (10.0, 11.0, 1.0)))
    values += array_u((0, 0, 0, 0, 0, 0))
    values += array_v((5,))
    values += array_v((0, 2, 2, 2, 2))
    values += array_u(tuple(_float_bits(value) for value in (150.0, 90.0)))
    values += array_u(())
    values += array_u(tuple(_float_bits(value) for value in cubic_values))
    values += array_u(())
    values += array_v(())
    contents = format_string + b"\0" + values
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(contents)) + contents
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    body = (
        _field(1, VARIANT_LOGICAL_ID.encode())
        + _field(2, frame)
        + _field(3, 1)
        + _field(4, _rgba(0.1, 0.7, 0.2, 0.8))
        + _field(20, b"")
        + _field(21, 24)
    )
    return _native_record_pair(page_id, body)


def _native_variable_brush_records(page_id: str) -> dict[bytes, bytes]:
    """Encode one supported variable-width brush with a wider terminal sample."""
    format_string = b"vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"
    flags = (0, 1)
    start = (100.0, 130.0, 0.0, -1.0, 1.0)
    sample = (0.0, 120.0, 130.0, 0.0, 1.0, 2.0, 160.0, 130.0, 0.0, 1.0, 4.0)
    values = struct.pack("<HII", 1, _float_bits(2.0), len(flags))
    values += b"".join(struct.pack("<H", flag) for flag in flags)
    values += struct.pack("<I", 1) + struct.pack("<5I", *(_float_bits(value) for value in start))
    values += struct.pack("<I", 1) + struct.pack("<11I", *(_float_bits(value) for value in sample))
    values += struct.pack("<I", 0) * 5
    contents = format_string + b"\0" + values
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(contents)) + contents
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    body = (
        _field(1, VARIANT_LOGICAL_ID.encode())
        + _field(2, frame)
        + _field(3, 5)
        + _field(4, _rgba(0.2, 0.4, 0.6, 0.5))
        + _field(21, 25)
    )
    return _native_record_pair(page_id, body, object_type=25)


def _native_open_two_point_shape_records(
    page_id: str,
    *,
    points: tuple[tuple[float, float], tuple[float, float]],
    width: float = 3.0,
    color: tuple[float, float, float, float] = (0.4, 0.2, 0.8, 0.75),
    translation: tuple[float, float] = (0.0, 0.0),
) -> dict[bytes, bytes]:
    """Encode the observed open two-point line shape, including optional dx/dy."""
    format_string = b"vuA(v)A(S(uu))A(S(uuuu))vA(f)"
    values = (
        struct.pack("<HfI", 2, width, 0)
        + struct.pack("<I", 0)
        + struct.pack("<I", 0)
        + struct.pack("<H", 1)
        + struct.pack("<I", 0)
    )
    contents = format_string + b"\0" + values
    tpl = b"tpl\0" + struct.pack("<I", 8 + len(contents)) + contents
    frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"
    body = (
        _field(1, VARIANT_LOGICAL_ID.encode())
        + _field(2, frame)
        + _field(4, _rgba(*color))
        + _field(
            9,
            _field(
                1,
                _field(1, _fixed(1, points[0][0]) + _fixed(2, points[0][1]))
                + _field(1, _fixed(1, points[1][0]) + _fixed(2, points[1][1])),
            )
            + _field(5, b"\x10\x01")
            + _fixed(15, width),
        )
        + _field(20, b"")
        + _field(21, 24)
    )
    if translation != (0.0, 0.0):
        body += _field(6, _fixed(1, translation[0]) + _fixed(2, translation[1]))
    return _native_record_pair(page_id, body)


def _native_image_layout_records(
    page_id: str,
    *,
    descriptor_layout: str = "explicit-attachment",
    body_field_6: int | None = 1,
    bounds: tuple[float, float, float, float] = (110.0, 55.0, 55.0, 55.0),
) -> dict[bytes, bytes]:
    """Encode an image with one of the generated exact-resource binding layouts."""
    if descriptor_layout not in {
        "explicit-attachment",
        "body-resource",
        "descriptor-resource",
    }:
        raise ValueError(f"Unknown generated image binding layout: {descriptor_layout}")
    x, y, width, height = bounds
    logical_id = VARIANT_LOGICAL_ID
    resource_id = VARIANT_RESOURCE_ID
    attachment_id = VARIANT_ATTACHMENT_ID

    def point(first: float, second: float) -> bytes:
        return _fixed(1, first) + _fixed(2, second)

    def rectangle(first: float, second: float) -> bytes:
        return _field(1, point(first, second)) + _field(2, point(width, height))

    body = (
        _field(1, logical_id.encode())
        + _field(2, rectangle(x, y))
        + _field(3, rectangle(x + width / 2, y + height / 2))
        + _field(4, resource_id.encode())
        + _field(5, b"generated")
        + (_field(6, body_field_6) if body_field_6 is not None else b"")
        + _field(15, b"generated")
        + _field(18, 24)
    )
    descriptor = _field(1, logical_id.encode())
    if descriptor_layout == "body-resource":
        # With neither descriptor resource nor attachment, the exact body
        # resource ID selects the projection attachment.
        key_storage_id = VARIANT_STORAGE_ID
        descriptor += _field(6, key_storage_id.encode())
    elif descriptor_layout == "descriptor-resource":
        key_storage_id = VARIANT_STORAGE_ID
        descriptor += _field(4, resource_id.encode()) + _field(6, key_storage_id.encode())
    else:
        key_storage_id = VARIANT_STORAGE_ID
        descriptor += (
            _field(4, resource_id.encode())
            + _field(6, key_storage_id.encode())
            + _field(7, attachment_id.encode())
        )
    descriptor += _field(8, 1) + _field(9, 2) + _field(16, 24)
    root = str(UUID(int=UUID(page_id).int + 1)).upper()
    prefix = f"{root}.normal.{key_storage_id}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(1, body)}


def _native_variant_records(page_id: str, variant: str) -> dict[bytes, bytes]:
    """Encode one named generated native variant or the ellipse control."""
    if variant not in {*GENERATED_NATIVE_VARIANT_CASES, "supported-ellipse-control"}:
        raise ValueError(f"Unknown generated native variant: {variant}")

    root = str(UUID(int=UUID(page_id).int + 1)).upper()
    logical_id = VARIANT_LOGICAL_ID
    storage_id = VARIANT_STORAGE_ID
    descriptor_prefix = _field(1, logical_id.encode())
    actor_sequence = _field(8, 1) + _field(9, 2) + _field(16, 24)

    if variant in {
        "image-body-missing-field-6",
        "descriptor-resource-without-attachment",
        "observed-combined-image-shape",
    }:

        def point(x: float, y: float) -> bytes:
            return _fixed(1, x) + _fixed(2, y)

        def rectangle(x: float, y: float, width: float, height: float) -> bytes:
            return _field(1, point(x, y)) + _field(2, point(width, height))

        x, y, width, height = 110.0, 55.0, 55.0, 55.0
        body = (
            _field(1, logical_id.encode())
            + _field(2, rectangle(x, y, width, height))
            + _field(3, rectangle(x + width / 2, y + height / 2, width, height))
            + _field(4, VARIANT_RESOURCE_ID.encode())
            + _field(5, b"")
            + (
                b""
                if variant in {"image-body-missing-field-6", "observed-combined-image-shape"}
                else _field(6, 1)
            )
            + _field(15, b"")
            + _field(18, 24)
        )
        descriptor = descriptor_prefix + _field(4, VARIANT_RESOURCE_ID.encode())
        if variant == "observed-combined-image-shape":
            descriptor += _field(2, b"generated") + _field(14, 1)
            record_storage_id = logical_id
        else:
            descriptor += _field(6, storage_id.encode())
            record_storage_id = storage_id
        if variant != "descriptor-resource-without-attachment":
            descriptor += _field(7, VARIANT_ATTACHMENT_ID.encode())
        descriptor += actor_sequence
        item = _field(1, body)
    else:
        # A generated, known observed ellipse control. The two unsupported
        # geometry cases mutate only the auxiliary envelope or opaque field 6.
        format_string = b"vuA(v)A(S(uu))A(S(uuuu))vA(f)"
        values = (
            struct.pack("<HfI", 2, 2.0, 0)
            + struct.pack("<I", 0)
            + struct.pack("<I", 0)
            + struct.pack("<H", 1)
            + struct.pack("<I", 0)
        )
        contents = format_string + b"\0" + values
        tpl = b"tpl\0" + struct.pack("<I", 8 + len(contents)) + contents
        frame = b"bv4-" + struct.pack("<II", len(tpl), len(tpl)) + tpl + b"bv4$"

        def point(x: float, y: float) -> bytes:
            return _fixed(1, x) + _fixed(2, y)

        shape = (
            _field(1, point(170.0, 115.0))
            + _field(2, point(25.0, 15.0))
            + _fixed(3, 1.5707963267948966)
        )
        auxiliary = (
            _field(4, shape) + _field(5, b"\x10\x01") + _fixed(15, 2.0)
            if variant != "alternate-shape-envelope"
            else _field(1, _field(1, point(1.0, 2.0)) + _field(2, point(10.0, 20.0)))
            + _field(5, b"\x10\x01")
            + _fixed(15, 2.0)
        )
        body = (
            _field(1, logical_id.encode())
            + _field(2, frame)
            + _field(4, _fixed(1, 0.1) + _fixed(2, 0.3) + _fixed(3, 0.5) + _fixed(4, 0.75))
            + _field(9, auxiliary)
            + _field(21, 24)
        )
        if variant == "shape-with-transform":
            body += _field(6, _fixed(1, 1.0) + _fixed(2, 2.0))
        descriptor = descriptor_prefix + _field(6, storage_id.encode()) + actor_sequence
        record_storage_id = storage_id
        item = _field(7, body)

    prefix = f"{root}.normal.{record_storage_id}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": item}


def _add_generated_page(source: Path, page_id: str = UNSUPPORTED_PAGE_ID) -> None:
    """Add a generated page with a valid PDF binding and no native records."""
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as database:
        database.execute(
            "INSERT INTO pages VALUES (?, ?, 'generated-template-1', '3', '', 0)",
            (page_id, NOTEBOOK),
        )


def _native_unknown_object_type_records(page_id: str) -> dict[bytes, bytes]:
    """Encode a generated object type the current reader does not recognize."""
    root = str(UUID(int=UUID(page_id).int + 1)).upper()
    logical_id = "00000000-0000-4000-8000-000000000a20"
    storage_id = "00000000-0000-4000-8000-000000000a21"
    descriptor = (
        _field(1, logical_id.encode())
        + _field(6, storage_id.encode())
        + _field(8, 1)
        + _field(9, 2)
        + _field(16, 99)
    )
    prefix = f"{root}.normal.{storage_id}.".encode()
    return {prefix + b"descriptor": descriptor, prefix + b"item": _field(7, b"")}


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


def _add_variant_attachments(source: Path) -> None:
    """Add exact generated bindings used by the Issue 8 image records."""
    values = (
        (VARIANT_ATTACHMENT_ID, "generated-variant-bound.png"),
        (VARIANT_RESOURCE_ID, "generated-variant-direct.png"),
    )
    for _, name in values:
        (source / "Attachments" / name).write_bytes(_solid_png((0, 240, 0)))
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as database:
        database.executemany(
            "INSERT INTO attachments VALUES (?, ?, ?, 1)",
            ((attachment_id.upper(), name, NOTEBOOK) for attachment_id, name in values),
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
