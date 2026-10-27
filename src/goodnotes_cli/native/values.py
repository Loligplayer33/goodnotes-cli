from __future__ import annotations

import math
import struct
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
    if not all(math.isfinite(value) and abs(value) <= 100_000 for value in point):
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
    if abs(dx) > 100_000 or abs(dy) > 100_000:
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
        max_output=4 * 1024 * 1024,
        charge_block=lambda size: budget.charge(byte_count=size),
    )
    if consumed != len(payload):
        raise ValueError("Unconsumed compressed native annotation bytes")
    format_end = decompressed.find(b"\0", 8, 256)
    if format_end < 0:
        raise ValueError("Missing native annotation TPL format")
    if expected_format is not None:
        expected = {expected_format} if isinstance(expected_format, str) else expected_format
        try:
            found_format = decompressed[8:format_end].decode("ascii")
        except UnicodeDecodeError as error:
            raise ValueError("Unsupported native annotation TPL format encoding") from error
        if found_format not in expected:
            raise _NativeDecodeIssue("unsupported-annotation-schema", schema_error)
    return decode_tpl(decompressed)


def _native_float(
    value: float, *, label: str, limit: float = 100_000, reason: str = "decode-failed"
) -> float:
    result = value
    if not math.isfinite(result) or abs(result) > limit:
        raise _NativeDecodeIssue(reason, f"Invalid native {label}")
    return result


def _tpl_float(
    value: int, *, label: str, limit: float = 100_000, reason: str = "decode-failed"
) -> float:
    return _native_float(_float(value), label=label, limit=limit, reason=reason)


def _rgb_alpha(body: Message, *, required: bool = False) -> tuple[str, float]:
    payload = _one(body, 4, bytes, required=required)
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
