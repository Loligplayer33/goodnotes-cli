"""Independently encoded native record fixtures used as decoder oracles."""

from __future__ import annotations

import struct

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
) -> bytes:
    points = points if points is not None else [(10.0, 20.0), (40.0, 20.0), (40.0, 50.0)]
    starts = points[:1] if starts is None else starts
    segments = list(zip(points, points[1:], strict=False)) if segments is None else segments
    flags = flags if flags is not None else [1] * (len(segments) + 1)
    values = struct.pack("<HfI", 0, width, len(flags))
    values += b"".join(struct.pack("<H", value) for value in flags)
    values += struct.pack("<I", len(starts))
    values += b"".join(struct.pack("<ff", *point) for point in starts)
    values += struct.pack("<I", len(segments))
    values += b"".join(struct.pack("<ffff", *start, *end) for start, end in segments)
    if format_string.endswith("vA(f)"):
        values += struct.pack("<HI", 0, len(dash))
        values += b"".join(struct.pack("<d", value) for value in dash)
    contents = format_string.encode() + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def float_bits(value: float) -> int:
    return struct.unpack("<I", struct.pack("<f", value))[0]


def variable_brush_tpl(
    *,
    samples: list[tuple[float, ...]],
    start: tuple[float, ...] = (1.0, 1.0, 0.0, -1.0, 1.0),
    width: float = 2.0,
    flags: list[int] | None = None,
    version: int = 1,
) -> bytes:
    flags = [0, *([1] * len(samples))] if flags is None else flags
    values = struct.pack("<HII", version, float_bits(width), len(flags))
    values += b"".join(struct.pack("<H", value) for value in flags)
    values += struct.pack("<I", 1) + struct.pack("<5I", *(float_bits(v) for v in start))
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
    arcs: list[float] = []
    arc_count = 0
    for group in command_groups:
        for code, values in group:
            codes.append(code)
            if code == 0:
                moves.extend(values)
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
    values += array_u([])
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
