from __future__ import annotations

import math
import struct
from enum import IntEnum
from typing import cast
from uuid import UUID

from .._vendor.compression import decode_apple_lz4
from .._vendor.tpl import TplImage, decode_tpl
from .._vendor.wire import Message, WireType, decode_message
from ..native.records import (
    _DecodeBudget,
    _NativeDecodeIssue,
    _UnsupportedNativeVariant,
    _UnsupportedReason,
)

# Native-unit policy and observed drawing-envelope selectors.
MAX_NATIVE_COORDINATE = 100_000

MIN_PEN_WIDTH = 0.05

MAX_PEN_WIDTH = 100

MAX_PAYLOAD_DECODE_BYTES = 4 * 1024 * 1024

MAX_OPAQUE_METADATA_BYTES = 64

_TPL_HEADER_BYTES = 8

_TPL_FORMAT_SCAN_END = 256


class NativeObjectType(IntEnum):
    STANDARD = 24
    VARIABLE_BRUSH = 25
    PATTERN_FILL = 28


class DrawingField(IntEnum):
    """Observed pen/path envelope tags, not image or fill envelope tags."""

    ID = 1
    TPL = 2
    VARIANT = 3
    COLOR = 4
    STYLE = 5
    TRANSLATION = 6
    OPAQUE_7 = 7
    GEOMETRY = 9
    OPAQUE_15 = 15
    METADATA = 20
    OBJECT_TYPE = 21


def _one(message: Message, field: int, kind: type, *, required: bool = True) -> bytes | int | None:
    fields = message.by_number(field)
    if not fields and not required:
        return None
    if (
        len(fields) != 1
        or not isinstance(fields[0].value, kind)
        or (kind is int and fields[0].wire_type != WireType.VARINT)
    ):
        raise ValueError(f"Missing or invalid protobuf field {field}")
    return fields[0].value


def _uuid(value: bytes | int | None) -> str:
    if not isinstance(value, bytes):
        raise ValueError("Invalid annotation UUID")
    text = value.decode("ascii")
    parsed = UUID(text)
    if str(parsed).upper() != text.upper():
        raise ValueError("Noncanonical annotation UUID")
    return text.upper()


def _float(value: int) -> float:
    return struct.unpack("<f", struct.pack("<I", value))[0]


def _point(x: int, y: int) -> tuple[float, float]:
    point = (_float(x), _float(y))
    if not all(math.isfinite(value) and abs(value) <= MAX_NATIVE_COORDINATE for value in point):
        raise ValueError("Invalid native ink coordinate")
    return point


def _fixed(message: Message, number: int, default: float) -> float:
    fields = message.by_number(number)
    if not fields:
        return default
    if len(fields) != 1 or fields[0].wire_type != WireType.FIXED32:
        raise ValueError("Invalid native color or translation float")
    value = fields[0].fixed_float()
    if value is None or not math.isfinite(value):
        raise ValueError("Nonfinite native color or translation")
    return value


def _native_translation(
    body: Message,
    *,
    unsupported_reason: _UnsupportedReason | None = None,
    unsupported_message: str = "Unsupported native translation field",
    require_pair: bool = False,
) -> tuple[float, float]:
    offset = _one(body, 6, bytes, required=False)
    if offset in {None, b""}:
        return 0.0, 0.0
    offset_message = decode_message(cast(bytes, offset))
    if any(item.number not in {1, 2} for item in offset_message.fields):
        if unsupported_reason is not None:
            raise _UnsupportedNativeVariant(unsupported_reason)
        raise ValueError(unsupported_message)
    if require_pair and {item.number for item in offset_message.fields} != {1, 2}:
        if unsupported_reason is not None:
            raise _UnsupportedNativeVariant(unsupported_reason)
        raise ValueError(unsupported_message)
    dx = _fixed(offset_message, 1, 0.0)
    dy = _fixed(offset_message, 2, 0.0)
    if abs(dx) > MAX_NATIVE_COORDINATE or abs(dy) > MAX_NATIVE_COORDINATE:
        raise ValueError("Native translation exceeds coordinate bounds")
    return dx, dy


def _decode_tpl_payload(
    body: Message,
    budget: _DecodeBudget,
    *,
    expected_format: str | set[str] | None = None,
    schema_error: str = "Unsupported native annotation TPL schema",
) -> TplImage:
    payload = cast(bytes, _one(body, 2, bytes))
    if not payload.startswith((b"bv41", b"bv4-")):
        raise ValueError("Unsupported native annotation compression")
    decompressed, consumed = decode_apple_lz4(
        payload,
        max_output=MAX_PAYLOAD_DECODE_BYTES,
        charge_block=lambda size: budget.charge(byte_count=size),
    )
    if consumed != len(payload):
        raise ValueError("Unconsumed compressed native annotation bytes")
    format_end = decompressed.find(b"\0", _TPL_HEADER_BYTES, _TPL_FORMAT_SCAN_END)
    if format_end < 0:
        raise ValueError("Missing native annotation TPL format")
    if expected_format is not None:
        expected = {expected_format} if isinstance(expected_format, str) else expected_format
        try:
            found_format = decompressed[_TPL_HEADER_BYTES:format_end].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("Unsupported native annotation TPL format encoding") from error
        if found_format not in expected:
            raise _NativeDecodeIssue("unsupported-annotation-schema", schema_error)
    return decode_tpl(decompressed)


def _native_float(
    value: float, *, label: str, limit: float = MAX_NATIVE_COORDINATE, reason: str = "decode-failed"
) -> float:
    result = value
    if not math.isfinite(result) or abs(result) > limit:
        raise _NativeDecodeIssue(reason, f"Invalid native {label}")
    return result


def _tpl_float(
    value: int, *, label: str, limit: float = MAX_NATIVE_COORDINATE, reason: str = "decode-failed"
) -> float:
    return _native_float(_float(value), label=label, limit=limit, reason=reason)


def _rgb_alpha(
    body: Message, *, required: bool = False, field_number: int = 4
) -> tuple[str, float]:
    payload = _one(body, field_number, bytes, required=required)
    color_message = decode_message(payload) if isinstance(payload, bytes) else None
    if color_message and any(item.number not in {1, 2, 3, 4} for item in color_message.fields):
        raise ValueError("Unsupported native color field")
    channels = (
        [_fixed(color_message, index, 0.0) for index in (1, 2, 3)]
        if color_message
        else [0.0, 0.0, 0.0]
    )
    alpha = _fixed(color_message, 4, 1.0) if color_message else 1.0
    if any(not 0 <= value <= 1 for value in [*channels, alpha]):
        raise ValueError("Native RGBA value is outside 0..1")
    color = "#" + "".join(f"{round(channel * 255):02x}" for channel in channels)
    return color, alpha


def _fixed_point(payload: bytes) -> tuple[float, float]:
    message = decode_message(payload)
    if len(message.fields) != 2 or {item.number for item in message.fields} != {1, 2}:
        raise ValueError("Invalid native image point or size")
    x = _fixed(message, 1, 0.0)
    y = _fixed(message, 2, 0.0)
    if abs(x) > MAX_NATIVE_COORDINATE or abs(y) > MAX_NATIVE_COORDINATE:
        raise ValueError("Native image geometry exceeds coordinate bounds")
    return x, y
