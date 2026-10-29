"""Independently encoded native record fixtures used as decoder oracles."""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path
from typing import cast

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.models import Page
from goodnotes_cli.native.assets import NativeImageData

PAGE_ID = "00000000-0000-0000-0000-000000000100"
ROOT_ID = "00000000-0000-0000-0000-000000000101"
STORAGE_ID = "00000000-0000-0000-0000-000000000200"
LOGICAL_ID = "00000000-0000-0000-0000-000000000300"
RESOURCE_ID = "00000000-0000-0000-0000-000000000400"
ATTACHMENT_ID = "00000000-0000-0000-0000-000000000401"
FORMAT = "vuA(v)A(S(uu))A(S(uuuu))"
VARIABLE_BRUSH_FORMAT = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"
NATIVE_PATH_FORMAT = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"


def varint(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number: int, value: bytes | int | float) -> bytes:
    if isinstance(value, bytes):
        return varint(number * 8 + 2) + varint(len(value)) + value
    if isinstance(value, float):
        return varint(number * 8 + 5) + struct.pack("<f", value)
    return varint(number * 8) + varint(value)


def tpl(
    *,
    points: list[tuple[float, float]] | None = None,
    width: float = 4.0,
    flags: list[int] | None = None,
    format_string: str = FORMAT,
    starts: list[tuple[float, float]] | None = None,
    segments: list[tuple[tuple[float, float], tuple[float, float]]] | None = None,
    dash: tuple[float, ...] = (),
    version: int = 0,
) -> bytes:
    points = points if points is not None else [(10.0, 20.0), (40.0, 20.0), (40.0, 50.0)]
    starts = points[:1] if starts is None else starts
    segments = list(zip(points, points[1:], strict=False)) if segments is None else segments
    flags = flags if flags is not None else [0 if version == 2 else 1, *([1] * len(segments))]
    values = struct.pack("<HfI", version, width, len(flags))
    values += b"".join(struct.pack("<H", value) for value in flags)
    values += struct.pack("<I", len(starts))
    values += b"".join(struct.pack("<ff", *point) for point in starts)
    values += struct.pack("<I", len(segments))
    values += b"".join(struct.pack("<ffff", *start, *end) for start, end in segments)
    if format_string.endswith("vA(f)"):
        values += struct.pack("<HI", 1 if version == 2 else 0, len(dash))
        values += b"".join(struct.pack("<d", value) for value in dash)
    contents = format_string.encode() + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def variable_brush_tpl(
    *,
    samples: list[tuple[float, ...]],
    start: tuple[float, ...] = (1.0, 1.0, 0.0, -1.0, 1.0),
    starts: list[tuple[float, ...]] | None = None,
    width: float = 2.0,
    flags: list[int] | None = None,
    version: int = 1,
) -> bytes:
    starts = [start] if starts is None else starts
    flags = [*([0] * len(starts)), *([1] * len(samples))] if flags is None else flags
    values = struct.pack("<HII", version, float_bits(width), len(flags))
    values += b"".join(struct.pack("<H", value) for value in flags)
    values += struct.pack("<I", len(starts))
    values += b"".join(struct.pack("<5I", *(float_bits(v) for v in row)) for row in starts)
    values += struct.pack("<I", len(samples))
    values += b"".join(
        struct.pack("<11I", *(float_bits(value) for value in sample)) for sample in samples
    )
    values += struct.pack("<I", 0)  # A(S(uu))
    values += struct.pack("<I", 0)  # A(v)
    values += struct.pack("<I", 0)  # A(S(uu))
    values += struct.pack("<I", 0)  # A(S(uuuu))
    values += struct.pack("<I", 0)  # A(u)
    contents = VARIABLE_BRUSH_FORMAT.encode() + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def native_path_tpl(
    *,
    command_groups: list[list[tuple[int, tuple[float, ...]]]],
    ellipse: bool = False,
    flags: list[int] | None = None,
    version: int = 2,
    arc_directions: list[int] | None = None,
) -> bytes:
    subpath_count = len(command_groups)
    flags = (
        ([4, *([5] * subpath_count)] if ellipse else [0, *([1] * subpath_count)])
        if flags is None
        else flags
    )
    codes: list[int] = []
    moves: list[float] = []
    cubics: list[float] = []
    quadratics: list[float] = []
    arcs: list[float] = []
    arc_count = 0
    for group in command_groups:
        for code, values in group:
            codes.append(code)
            if code == 0:
                moves.extend(values)
            elif code == 1:
                quadratics.extend(values)
            elif code == 2:
                cubics.extend(values)
            elif code in {3, 6}:
                arcs.extend(values)
                arc_count += 1
    directions = [1] * arc_count if arc_directions is None else arc_directions
    sample_metadata = [10.0, 11.0, 1.0, 0.0] if ellipse else [10.0, 11.0, 1.0]
    segment_metadata = [0.0] * ((9 if ellipse else 6) * subpath_count)

    def array_u(values: list[int]) -> bytes:
        return struct.pack("<I", len(values)) + b"".join(struct.pack("<I", v) for v in values)

    def array_v(values: list[int]) -> bytes:
        return struct.pack("<I", len(values)) + b"".join(struct.pack("<H", v) for v in values)

    values = struct.pack("<H", version)
    values += array_v(flags)
    values += array_u([float_bits(value) for value in sample_metadata])
    values += array_u([float_bits(value) for value in segment_metadata])
    values += array_v([len(group) for group in command_groups])
    values += array_v(codes)
    values += array_u([float_bits(value) for value in moves])
    values += array_u([float_bits(value) for value in quadratics])
    values += array_u([float_bits(value) for value in cubics])
    values += array_u([float_bits(value) for value in arcs])
    values += array_v(directions)
    contents = NATIVE_PATH_FORMAT.encode() + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def rgba_message(
    red: float = 0.0, green: float = 0.0, blue: float = 0.0, alpha: float = 1.0
) -> bytes:
    return field(1, red) + field(2, green) + field(3, blue) + field(4, alpha)


def native_path_body(payload: bytes, *, color: bytes | None = None, metadata: bytes = b"") -> bytes:
    body = (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(payload))
        + field(3, 1)
        + field(4, color or rgba_message(0.25, 0.5, 0.75, 1.0))
        + field(20, metadata)
        + field(21, 24)
    )
    return body


def native_image_records(
    *,
    logical_id: str = LOGICAL_ID,
    resource_id: str = RESOURCE_ID,
    attachment_id: str | None = ATTACHMENT_ID,
    center_offset: tuple[float, float] = (0.0, 0.0),
) -> list[tuple[bytes, bytes]]:
    x, y, width, height = 20.0, 30.0, 40.0, 50.0

    def point_message(first: float, second: float) -> bytes:
        return field(1, first) + field(2, second)

    def rectangle(origin_x: float, origin_y: float) -> bytes:
        return field(1, point_message(origin_x, origin_y)) + field(2, point_message(width, height))

    body = (
        field(1, logical_id.encode())
        + field(2, rectangle(x, y))
        + field(3, rectangle(x + width / 2 + center_offset[0], y + height / 2 + center_offset[1]))
        + field(4, resource_id.encode())
        + field(5, b"synthetic-ts")
        + field(6, 1)
        + field(15, b"synthetic")
        + field(18, 24)
    )
    descriptor = (
        field(1, logical_id.encode())
        + field(4, resource_id.encode())
        + field(6, STORAGE_ID.encode())
        + (field(7, attachment_id.encode()) if attachment_id else b"")
        + field(8, 1)
        + field(9, 1)
        + field(16, 24)
    )
    prefix = f"{ROOT_ID}.normal.{STORAGE_ID}.".encode()
    return [(prefix + b"descriptor", descriptor), (prefix + b"item", field(1, body))]


def framed(data: bytes, *, compressed: bool = True) -> bytes:
    if not compressed:
        return b"bv4-" + struct.pack("<II", len(data), len(data)) + data + b"bv4$"
    remaining = max(0, len(data) - 15)
    extension = bytearray()
    if len(data) >= 15:
        while remaining >= 255:
            extension.append(255)
            remaining -= 255
        extension.append(remaining)
    block = bytes([min(len(data), 15) << 4]) + bytes(extension) + data
    return b"bv41" + struct.pack("<II", len(data), len(block)) + block + b"bv4$"


def native_records(
    *,
    storage_id: str = STORAGE_ID,
    logical_id: str = LOGICAL_ID,
    category: str = "normal",
    erased: bool = False,
    type_code: int = 24,
    actor: int = 1,
    sequence: int = 1,
    body: bytes | None = None,
    extra_descriptor: bytes = b"",
    storage_alias: bool = True,
) -> list[tuple[bytes, bytes]]:
    descriptor = (
        field(1, logical_id.encode())
        + (field(6, storage_id.encode()) if storage_alias else b"")
        + field(8, actor)
        + field(9, sequence)
        + field(16, type_code)
        + extra_descriptor
    )
    if erased:
        descriptor += field(3, 1)
    if body is None:
        body = field(1, logical_id.encode()) + field(2, framed(tpl()))
    prefix = f"{ROOT_ID}.{category}.{storage_id}.".encode()
    return [(prefix + b"descriptor", descriptor), (prefix + b"item", field(7, body))]


class _FixtureSnapshot:
    root = Path("/synthetic-snapshot")
    library_dir = root / "Library"
    projection_path = library_dir / "Databases" / "projection.sqlite"
    attachments_dir = library_dir / "Attachments"


def page() -> Page:
    return Page(PAGE_ID, "synthetic-notebook", 1, "a", width=132, height=132)


def _brush_body(payload: bytes, *, extra: bytes = b"") -> bytes:
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(payload))
        + field(3, 5)
        + field(4, rgba_message(0.2, 0.4, 0.6, 0.5))
        + field(21, 25)
        + extra
    )


def _ellipse_body(
    *,
    angle: float = math.pi / 2,
    extra_shape: bytes = b"",
    style: bytes = b"\x10\x01",
    extra_ink: bool = False,
    auxiliary_width: float = 2.0,
) -> bytes:
    payload = tpl(
        points=[] if not extra_ink else [(2, 3)],
        flags=[],
        format_string=FORMAT + "vA(f)",
        width=2.0,
    )
    start = 8 + len(FORMAT + "vA(f)") + 1
    raw = bytearray(payload)
    struct.pack_into("<H", raw, start, 2)
    struct.pack_into("<H", raw, len(raw) - 6, 1)
    shape = (
        field(1, field(1, 30.0) + field(2, 40.0))
        + field(2, field(1, 8.0) + field(2, 3.0))
        + field(3, angle)
        + extra_shape
    )
    auxiliary = field(4, shape) + field(5, style) + field(15, auxiliary_width)
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(bytes(raw)))
        + field(4, rgba_message(0.1, 0.3, 0.5, 0.75))
        + field(9, auxiliary)
        + field(21, 24)
    )


def _without_message_fields(payload: bytes, numbers: set[int]) -> bytes:
    return b"".join(
        item.raw for item in decode_message(payload).fields if item.number not in numbers
    )


def _image_records_without_field_6(*, logical_id: str = LOGICAL_ID) -> list[tuple[bytes, bytes]]:
    records = native_image_records(logical_id=logical_id)
    key, item = records[1]
    body = cast(bytes, decode_message(item).by_number(1)[0].value)
    records[1] = (key, field(1, _without_message_fields(body, {6})))
    return records


def _issue_8_combined_image_records() -> list[tuple[bytes, bytes]]:
    records = _image_records_without_field_6(logical_id=STORAGE_ID)
    descriptor_key, descriptor_value = records[0]
    descriptor_value = _without_message_fields(descriptor_value, {6, 7})
    descriptor_value += field(2, b"generated") + field(14, 1)
    records[0] = (descriptor_key, descriptor_value)
    return records


def _issue_8_alternate_shape_envelope_records() -> list[tuple[bytes, bytes]]:
    return _native_two_point_shape_records([(1.0, 2.0), (10.0, 20.0)])


def _native_two_point_shape_records(
    points: list[tuple[float, float]], *, offset: tuple[float, float] | None = None
) -> list[tuple[bytes, bytes]]:
    body = decode_message(_ellipse_body())
    auxiliary = decode_message(cast(bytes, body.by_number(9)[0].value))
    point_stream = b"".join(field(1, field(1, x) + field(2, y)) for x, y in points)
    replacement = field(1, point_stream) + b"".join(
        item.raw for item in auxiliary.fields if item.number != 4
    )
    body_bytes = b"".join(
        field(9, replacement) if item.number == 9 else item.raw for item in body.fields
    )
    if offset is not None:
        body_bytes += field(6, field(1, offset[0]) + field(2, offset[1]))
    return native_records(body=body_bytes)


def _ellipse_with_unverified_field_6_records() -> list[tuple[bytes, bytes]]:
    body = decode_message(_ellipse_body())
    body_bytes = b"".join(item.raw for item in body.fields if item.number != 6)
    body_bytes += field(6, field(1, 1.0) + field(2, 2.0))
    return native_records(body=body_bytes)


def _pattern_metadata(
    *, resource: bytes = RESOURCE_ID.encode(), scale: float = 1.0, width: float = 3.0
) -> bytes:
    anchor = field(1, 7.0) + field(2, 11.0)
    return field(2, field(1, resource) + field(3, anchor) + field(4, width) + field(5, scale))


def _pattern_body(metadata: bytes) -> bytes:
    payload = native_path_tpl(
        command_groups=[[(0, (2.0, 3.0)), (1, (5.0, 9.0, 8.0, 3.0))]], ellipse=True
    )
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(payload))
        + field(3, 1)
        + field(4, rgba_message(0.0, 1.0, 0.0, 0.5))
        + field(20, metadata)
        + field(21, 28)
    )


def _texture(*, alpha: int = 255) -> NativeImageData:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    scanline = b"\0" + bytes((255, 0, 0, alpha)) * 6 + bytes((0, 0, 255, alpha)) * 6
    data = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 12, 12, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scanline * 12))
        + chunk(b"IEND", b"")
    )
    return NativeImageData(data, "image/png", 12, 12)
