"""Shared shape geometry, separate fill objects and validated image transforms."""

from __future__ import annotations

import math
from enum import IntEnum
from typing import cast

from .._vendor.wire import Message, WireType, decode_message
from .records import (
    NativeImage,
    NativePath,
    NativePathCommand,
    _DecodeBudget,
    _NativeDecodeIssue,
    _UnsupportedNativeVariant,
)
from .values import (
    MAX_NATIVE_COORDINATE,
    MAX_OPAQUE_METADATA_BYTES,
    MAX_PEN_WIDTH,
    MIN_PEN_WIDTH,
    DrawingField,
    NativeObjectType,
    _decode_tpl_payload,
    _fixed,
    _fixed_point,
    _native_float,
    _native_translation,
    _one,
    _rgb_alpha,
    _tpl_float,
    _uuid,
)


class _ShapeKind(IntEnum):
    POLYLINE = 1
    QUADRATIC = 2
    RECTANGLE = 3
    ELLIPSE = 4


def _native_shape_style(body: Message, budget: _DecodeBudget) -> tuple[float, str, float]:
    if _one(body, DrawingField.OBJECT_TYPE, int) != NativeObjectType.STANDARD:
        raise ValueError("Unsupported native shape body type")
    for number in (7, 15):
        _one(body, number, bytes, required=False)
    image = _decode_tpl_payload(
        body,
        budget,
        expected_format="vuA(v)A(S(uu))A(S(uuuu))vA(f)",
        schema_error="Unsupported native shape TPL schema",
    )
    values = image.values
    if len(values) != 7 or values[0] != 2 or values[5] != 1:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported native shape TPL version"
        )
    if any(values[index] for index in (2, 3, 4, 6)):
        raise ValueError("Native shape has additional ink or dash geometry")
    width = _tpl_float(cast(int, values[1]), label="shape width")
    if not MIN_PEN_WIDTH <= width <= MAX_PEN_WIDTH:
        raise ValueError("Invalid native shape width")
    color, alpha = _rgb_alpha(body, required=True)
    return width, color, alpha


def _shape_geometry(
    auxiliary: Message, budget: _DecodeBudget, translation: tuple[float, float]
) -> tuple[NativePathCommand, ...]:
    """Decode a single recognized geometry envelope into bounded page coordinates."""
    kinds = [field.number for field in auxiliary.fields if field.number in set(_ShapeKind)]
    if len(kinds) != 1:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    kind = kinds[0]
    geometry = decode_message(cast(bytes, _one(auxiliary, kind, bytes)))
    dx, dy = translation

    def translated(point: tuple[float, float]) -> tuple[float, float]:
        return (
            _native_float(point[0] + dx, label="shape coordinate"),
            _native_float(point[1] + dy, label="shape coordinate"),
        )

    if kind == _ShapeKind.POLYLINE:
        if any(
            field.number != 1 or not isinstance(field.value, bytes) for field in geometry.fields
        ):
            raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
        if len(geometry.fields) < 2:
            raise _UnsupportedNativeVariant("unsupported-native-geometry-point-count")
        budget.charge(point_count=len(geometry.fields))
        points = [translated(_fixed_point(cast(bytes, field.value))) for field in geometry.fields]
        return (NativePathCommand("M", points[0]),) + tuple(
            NativePathCommand("L", point) for point in points[1:]
        )
    if kind == _ShapeKind.QUADRATIC:
        if len(geometry.fields) != 3 or {field.number for field in geometry.fields} != {1, 2, 3}:
            raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
        budget.charge(point_count=3)
        points = [
            translated(_fixed_point(cast(bytes, _one(geometry, n, bytes)))) for n in (1, 2, 3)
        ]
        return (NativePathCommand("M", points[0]), NativePathCommand("Q", (*points[1], *points[2])))
    if len(geometry.fields) != 3 or {field.number for field in geometry.fields} != {1, 2, 3}:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    center = _fixed_point(cast(bytes, _one(geometry, 1, bytes)))
    size_x, size_y = _fixed_point(cast(bytes, _one(geometry, 2, bytes)))
    angle = _fixed(geometry, 3, 0.0)
    if size_x <= 0 or size_y <= 0 or abs(angle) > math.tau:
        raise ValueError("Invalid native shape dimensions or rotation")
    cosine, sine = math.cos(angle), math.sin(angle)

    def transform(x: float, y: float) -> tuple[float, float]:
        return translated((center[0] + x * cosine - y * sine, center[1] + x * sine + y * cosine))

    if kind == _ShapeKind.RECTANGLE:
        budget.charge(point_count=4)
        x, y = size_x / 2, size_y / 2
        corners = [transform(-x, -y), transform(x, -y), transform(x, y), transform(-x, y)]
        return (
            (NativePathCommand("M", corners[0]),)
            + tuple(NativePathCommand("L", point) for point in corners[1:])
            + (NativePathCommand("Z"),)
        )
    # One move and four cubics, each with two controls and an endpoint.
    budget.charge(point_count=1 + 4 * 3)
    kappa = 4 * (math.sqrt(2) - 1) / 3
    segments = (
        ((size_x, kappa * size_y), (kappa * size_x, size_y), (0.0, size_y)),
        ((-kappa * size_x, size_y), (-size_x, kappa * size_y), (-size_x, 0.0)),
        ((-size_x, -kappa * size_y), (-kappa * size_x, -size_y), (0.0, -size_y)),
        ((kappa * size_x, -size_y), (size_x, -kappa * size_y), (size_x, 0.0)),
    )
    commands = [NativePathCommand("M", transform(size_x, 0.0))]
    commands.extend(
        NativePathCommand("C", tuple(value for point in segment for value in transform(*point)))
        for segment in segments
    )
    return tuple(commands)


def _decode_native_shape(body: Message, budget: _DecodeBudget) -> NativePath:
    """Decode a stroked shape using its geometry, translation and shared style."""
    if any(item.number not in {1, 2, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported native shape body field")
    translation = _native_translation(
        body, unsupported_reason="unsupported-native-geometry-field-6", require_pair=True
    )
    if _one(body, DrawingField.METADATA, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported native shape transform or metadata")
    width, color, alpha = _native_shape_style(body, budget)
    auxiliary = decode_message(cast(bytes, _one(body, DrawingField.GEOMETRY, bytes)))
    numbers = {item.number for item in auxiliary.fields}
    if (
        len(auxiliary.fields) != 3
        or len(numbers & set(_ShapeKind)) != 1
        or numbers - set(_ShapeKind) != {5, 15}
    ):
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    if _one(auxiliary, 5, bytes) != b"\x10\x01":
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Uncertain native shape visibility or style"
        )
    auxiliary_width = _fixed(auxiliary, 15, 0.0)
    if not math.isclose(auxiliary_width, width, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("Native shape width fields disagree")
    commands = _shape_geometry(auxiliary, budget, translation)
    return NativePath(
        commands,
        fill_color=None,
        fill_alpha=0.0,
        stroke_color=color,
        stroke_width=width,
        stroke_alpha=alpha,
    )


def _decode_native_fill(body: Message, budget: _DecodeBudget) -> NativePath:
    """Decode the observed filled-ellipse item, preserving its separate draw order."""
    if any(field.number not in {1, 2, 3, 4, 5, 6, 7, 15, 18} for field in body.fields):
        raise ValueError("Unsupported native fill body field")
    if _one(body, 18, int) != NativeObjectType.STANDARD:
        raise ValueError("Unsupported native fill version")
    if (bounds := _one(body, 2, bytes, required=False)) is not None:
        _native_rect(cast(bytes, bounds))
    if (outline := _one(body, 5, bytes, required=False)) is not None:
        _uuid(outline)
    for number in (3, 15):
        value = _one(body, number, bytes, required=False)
        if isinstance(value, bytes) and len(value) > MAX_OPAQUE_METADATA_BYTES:
            raise ValueError("Native fill metadata exceeds its byte budget")
    translation = _native_translation(
        body, unsupported_reason="unsupported-native-geometry-field-6", require_pair=True
    )
    auxiliary = decode_message(cast(bytes, _one(body, 4, bytes)))
    if len(auxiliary.fields) != 1 or auxiliary.fields[0].number != 4:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    color, alpha = _rgb_alpha(body, required=True, field_number=7)
    return NativePath(
        _shape_geometry(auxiliary, budget, translation), fill_color=color, fill_alpha=alpha
    )


def _native_rect(payload: bytes) -> tuple[float, float, float, float]:
    """Validate axis-aligned bounds shared by image and fill envelopes."""
    message = decode_message(payload)
    if len(message.fields) != 2 or {item.number for item in message.fields} != {1, 2}:
        raise ValueError("Invalid native rectangle")
    origin_payload = cast(bytes, _one(message, 1, bytes))
    size_payload = cast(bytes, _one(message, 2, bytes))
    x, y = _fixed_point(origin_payload)
    width, height = _fixed_point(size_payload)
    if width <= 0 or height <= 0 or width > MAX_NATIVE_COORDINATE or height > MAX_NATIVE_COORDINATE:
        raise ValueError("Invalid native geometry dimensions")
    if abs(x + width) > MAX_NATIVE_COORDINATE or abs(y + height) > MAX_NATIVE_COORDINATE:
        raise ValueError("Native rectangle exceeds coordinate bounds")
    return x, y, width, height


def _decode_native_image(descriptor: Message, item: Message, logical_id: str) -> NativeImage:
    """Decode an image reference and native-unit bounds without opening media."""
    if any(field.number != 1 for field in item.fields):
        raise ValueError("Unsupported native image item envelope")
    payload = cast(bytes, _one(item, 1, bytes))
    body = decode_message(payload)
    if any(field.number not in {1, 2, 3, 4, 5, 6, 15, 18} for field in body.fields):
        raise ValueError("Unsupported native image body field")
    if _uuid(_one(body, 1, bytes)) != logical_id:
        raise _NativeDecodeIssue("image-binding-invalid", "Native image logical identity mismatch")
    original_resource = _uuid(_one(body, 4, bytes))
    field_6 = body.by_number(6)
    if field_6 and (
        len(field_6) != 1
        or field_6[0].wire_type != WireType.VARINT
        or not isinstance(field_6[0].value, int)
        or field_6[0].value != 1
    ):
        raise _UnsupportedNativeVariant("unsupported-native-image-field-6")
    if _one(body, 18, int) != NativeObjectType.STANDARD:
        raise ValueError("Unsupported native image version or transform")
    for number in (5, 15):
        opaque = cast(bytes, _one(body, number, bytes))
        if len(opaque) > MAX_OPAQUE_METADATA_BYTES:
            raise ValueError("Native image metadata exceeds its byte budget")

    bounds_x, bounds_y, bounds_width, bounds_height = _native_rect(
        cast(bytes, _one(body, 2, bytes))
    )
    geometry = decode_message(cast(bytes, _one(body, 3, bytes)))
    if any(field.number not in {1, 2, 3} for field in geometry.fields):
        raise ValueError("Unsupported native image local geometry")
    center_x, center_y = _fixed_point(cast(bytes, _one(geometry, 1, bytes)))
    width, height = _fixed_point(cast(bytes, _one(geometry, 2, bytes)))
    rotation = _fixed(geometry, 3, 0.0)
    if width <= 0 or height <= 0:
        raise ValueError("Invalid native geometry dimensions")
    x, y = center_x - width / 2, center_y - height / 2
    cosine, sine = math.cos(rotation), math.sin(rotation)
    corners = [
        (center_x + dx * cosine - dy * sine, center_y + dx * sine + dy * cosine)
        for dx in (-width / 2, width / 2)
        for dy in (-height / 2, height / 2)
    ]
    for value in (x, y, x + width, y + height, *(value for point in corners for value in point)):
        _native_float(value, label="image coordinate")
    left = min(point[0] for point in corners)
    top = min(point[1] for point in corners)
    right = max(point[0] for point in corners)
    bottom = max(point[1] for point in corners)

    def float32_ulp(value: float) -> float:
        if value == 0:
            return 2.0**-149
        return max(2.0**-149, 2.0 ** (math.frexp(abs(value))[1] - 24))

    # Both rectangles and the angle use float32 storage. Account for rounding
    # propagated through rotation, but never accept a broad geometry mismatch.
    coordinate_size = max(
        abs(bounds_x),
        abs(bounds_y),
        bounds_width,
        bounds_height,
        abs(center_x),
        abs(center_y),
        width,
        height,
    )
    tolerance = max(
        1e-4,
        min(0.05, 4 * float32_ulp(coordinate_size) + (width + height) * float32_ulp(rotation)),
    )
    if any(
        abs(actual - expected) > tolerance
        for actual, expected in zip(
            (bounds_x, bounds_y, bounds_x + bounds_width, bounds_y + bounds_height),
            (left, top, right, bottom),
            strict=True,
        )
    ):
        raise ValueError("Native image center and top-left bounds disagree")

    descriptor_resource = _one(descriptor, 4, bytes, required=False)
    descriptor_attachment = _one(descriptor, 7, bytes, required=False)
    if descriptor_resource is None and descriptor_attachment is None:
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is None:
        if _uuid(descriptor_resource) != original_resource:
            raise _NativeDecodeIssue(
                "image-binding-invalid", "Native image resource identity mismatch"
            )
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is not None:
        if _uuid(descriptor_resource) != original_resource:
            raise _NativeDecodeIssue(
                "image-binding-invalid", "Native image resource identity mismatch"
            )
        attachment_id = _uuid(descriptor_attachment)
    else:
        raise _UnsupportedNativeVariant("image-binding-incomplete")
    return NativeImage(attachment_id, x, y, width, height, rotation)
