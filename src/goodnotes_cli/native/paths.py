"""Compile stored filled-path arrays and pattern bindings into drawing commands."""

from __future__ import annotations

import math
from dataclasses import replace
from enum import IntEnum
from typing import cast

from .._vendor.wire import Message, decode_message
from .records import NativePath, NativePathCommand, NativePattern, _DecodeBudget, _NativeDecodeIssue
from .values import (
    MAX_PEN_WIDTH,
    DrawingField,
    NativeObjectType,
    _decode_tpl_payload,
    _fixed,
    _fixed_point,
    _native_float,
    _one,
    _rgb_alpha,
    _tpl_float,
    _uuid,
)

_NATIVE_PATH_FORMAT = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"


class _NativePathCode(IntEnum):
    MOVE = 0
    QUADRATIC = 1
    CUBIC = 2
    CIRCULAR_ARC = 3
    ELLIPTICAL_ARC = 6


def _arc_end(
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    end_angle: float,
    rotation: float,
) -> tuple[float, float]:
    cosine, sine = math.cos(rotation), math.sin(rotation)
    ellipse_x = radius_x * math.cos(end_angle)
    ellipse_y = radius_y * math.sin(end_angle)
    return (
        center_x + ellipse_x * cosine - ellipse_y * sine,
        center_y + ellipse_x * sine + ellipse_y * cosine,
    )


def _arc_commands(
    center_x: float,
    center_y: float,
    radius_x: float,
    radius_y: float,
    start_angle: float,
    end_angle: float,
    rotation: float,
    budget: _DecodeBudget,
) -> list[NativePathCommand]:
    """Compile stored parametric arcs to their bounded cubic control stream.

    The source path appends these controls directly after its current point.
    An SVG endpoint arc would infer a different center when that point differs
    from the parametric start, as it can after source editing and smoothing.
    """
    delta = (start_angle - end_angle) % math.tau
    if min(delta, math.tau - delta) <= 1e-6:
        raise _NativeDecodeIssue("unsupported-native-path", "Ambiguous native full-circle arc")
    # Every emitted cubic spans at most a quarter turn. The source export can
    # partition a near-quarter boundary differently before float32 rounding.
    count = max(1, math.ceil(delta / (math.pi / 2)))
    budget.charge(point_count=count * 3)
    step = -delta / count
    factor = 4 / 3 * math.tan(step / 4)
    cosine, sine = math.cos(rotation), math.sin(rotation)

    def tangent(angle: float) -> tuple[float, float]:
        x, y = -radius_x * math.sin(angle), radius_y * math.cos(angle)
        return x * cosine - y * sine, x * sine + y * cosine

    result = []
    for index in range(count):
        first_angle = start_angle + index * step
        last_angle = start_angle + (index + 1) * step
        first = _arc_end(center_x, center_y, radius_x, radius_y, first_angle, rotation)
        last = _arc_end(center_x, center_y, radius_x, radius_y, last_angle, rotation)
        first_tangent, last_tangent = tangent(first_angle), tangent(last_angle)
        coordinates = (
            first[0] + factor * first_tangent[0],
            first[1] + factor * first_tangent[1],
            last[0] - factor * last_tangent[0],
            last[1] - factor * last_tangent[1],
            *last,
        )
        result.append(
            NativePathCommand(
                "C",
                tuple(
                    _native_float(
                        value, label="arc control coordinate", reason="unsupported-native-path"
                    )
                    for value in coordinates
                ),
            )
        )
    return result


def _decode_native_path(
    body: Message, budget: _DecodeBudget, *, patterned: bool = False
) -> NativePath:
    """Decode the observed version-2 filled native CGPath arrays."""
    if any(item.number not in {1, 2, 3, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported native path body field")
    if _one(body, DrawingField.VARIANT, int) != 1 or _one(body, DrawingField.OBJECT_TYPE, int) != (
        NativeObjectType.PATTERN_FILL if patterned else NativeObjectType.STANDARD
    ):
        raise ValueError("Unsupported native path envelope version")
    metadata = _one(body, DrawingField.METADATA, bytes)
    for number in (6, 9):
        if _one(body, number, bytes, required=False) not in {None, b""}:
            raise ValueError("Unsupported native path transform or auxiliary geometry")
    for number in (7, 15):
        _one(body, number, bytes, required=False)

    image = _decode_tpl_payload(
        body,
        budget,
        expected_format=_NATIVE_PATH_FORMAT,
        schema_error="Unsupported native path TPL schema",
    )
    if len(image.values) != 11:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported native path TPL schema"
        )
    values = image.values
    if values[0] != 2:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported native path TPL version"
        )

    flags = cast(list[int], values[1])
    sample_metadata = cast(list[int], values[2])
    segment_metadata = cast(list[int], values[3])
    subpath_counts = cast(list[int], values[4])
    command_codes = cast(list[int], values[5])
    move_values = cast(list[int], values[6])
    quadratic_values = cast(list[int], values[7])
    cubic_values = cast(list[int], values[8])
    arc_values = cast(list[int], values[9])
    arc_directions = cast(list[int], values[10])
    subpath_count = len(subpath_counts)
    if not subpath_counts or not command_codes or any(count < 0 for count in subpath_counts):
        raise _NativeDecodeIssue(
            "unsupported-native-path", "Invalid native path subpath command counts"
        )
    if len(command_codes) != sum(subpath_counts):
        raise _NativeDecodeIssue(
            "unsupported-native-path", "Native path command count does not match subpaths"
        )
    circle_variant = (
        len(sample_metadata) == 3
        and len(segment_metadata) == 6 * subpath_count
        and flags == [0, *([1] * subpath_count)]
    )
    ellipse_variant = (
        len(sample_metadata) == 4
        and len(segment_metadata) == 9 * subpath_count
        and flags == [4, *([5] * subpath_count)]
    )
    if not (circle_variant or ellipse_variant):
        raise ValueError("Unsupported native path version-2 flags or sample layout")
    if not patterned and metadata not in {b"", b"\x0a\x00"}:
        raise ValueError("Unsupported native path metadata")

    arc_code = _NativePathCode.CIRCULAR_ARC if circle_variant else _NativePathCode.ELLIPTICAL_ARC
    allowed_commands = {
        _NativePathCode.MOVE,
        _NativePathCode.QUADRATIC,
        _NativePathCode.CUBIC,
        arc_code,
    }
    if any(code not in allowed_commands for code in command_codes):
        raise _NativeDecodeIssue("unsupported-native-path", "Unsupported native path command")
    move_count = command_codes.count(_NativePathCode.MOVE)
    quadratic_count = command_codes.count(_NativePathCode.QUADRATIC)
    cubic_count = command_codes.count(_NativePathCode.CUBIC)
    arc_count = command_codes.count(arc_code)
    if (
        len(move_values) != move_count * 2
        or len(quadratic_values) != quadratic_count * 4
        or len(cubic_values) != cubic_count * 6
        or len(arc_values) != arc_count * (5 if circle_variant else 7)
        or len(arc_directions) != arc_count
        or any(direction != 1 for direction in arc_directions)
    ):
        raise _NativeDecodeIssue(
            "unsupported-native-path", "Native path coordinate arrays do not match command stream"
        )
    budget.charge(point_count=move_count + quadratic_count * 2 + cubic_count * 3)

    # The sample arrays are retained by the source format but are redundant to
    # the exact move/cubic/arc path arrays. Validate their floats and shape,
    # without using them to invent additional centerline geometry.
    for raw in [*sample_metadata, *segment_metadata]:
        _tpl_float(raw, label="path sample metadata")
    if _tpl_float(sample_metadata[2], label="path sample radius") <= 0:
        raise ValueError("Invalid native path sample radius")

    move_index = quadratic_index = cubic_index = arc_index = command_index = 0
    decoded: list[NativePathCommand] = []
    for count in subpath_counts:
        if count == 0:
            continue
        current_point: tuple[float, float] | None = None
        subpath_codes = command_codes[command_index : command_index + count]
        command_index += count
        if (
            not subpath_codes
            or subpath_codes[0] != _NativePathCode.MOVE
            or _NativePathCode.MOVE in subpath_codes[1:]
        ):
            raise _NativeDecodeIssue(
                "unsupported-native-path", "Native filled subpath must begin with one move command"
            )
        for code in subpath_codes:
            if code == _NativePathCode.MOVE:
                x = _tpl_float(move_values[move_index], label="path coordinate")
                y = _tpl_float(move_values[move_index + 1], label="path coordinate")
                move_index += 2
                current_point = (x, y)
                decoded.append(NativePathCommand("M", (x, y)))
            elif code == _NativePathCode.QUADRATIC:
                coordinates = tuple(
                    _tpl_float(value, label="path coordinate")
                    for value in quadratic_values[quadratic_index : quadratic_index + 4]
                )
                quadratic_index += 4
                current_point = (coordinates[2], coordinates[3])
                decoded.append(NativePathCommand("Q", coordinates))
            elif code == _NativePathCode.CUBIC:
                coordinates = tuple(
                    _tpl_float(value, label="path coordinate")
                    for value in cubic_values[cubic_index : cubic_index + 6]
                )
                cubic_index += 6
                current_point = (coordinates[4], coordinates[5])
                decoded.append(NativePathCommand("C", coordinates))
            else:
                arity = 5 if circle_variant else 7
                arc = tuple(
                    _tpl_float(value, label="path arc", reason="unsupported-native-path")
                    for value in arc_values[arc_index : arc_index + arity]
                )
                arc_index += arity
                if circle_variant:
                    center_x, center_y, radius, start_angle, end_angle = arc
                    if radius <= 0:
                        raise ValueError("Invalid native circular arc radius")
                    radius_x = radius_y = radius
                    rotation = 0.0
                else:
                    center_x, center_y, radius_x, radius_y, start_angle, end_angle, rotation = arc
                    if radius_x <= 0 or radius_y <= 0:
                        raise ValueError("Unsupported native elliptical arc geometry")
                if current_point is None:
                    raise _NativeDecodeIssue(
                        "unsupported-native-path", "Native path arc has no preceding point"
                    )
                arc_commands = _arc_commands(
                    center_x, center_y, radius_x, radius_y, start_angle, end_angle, rotation, budget
                )
                decoded.extend(arc_commands)
                current_point = cast(tuple[float, float], arc_commands[-1].values[-2:])
        decoded.append(NativePathCommand("Z"))

    if (
        move_index != len(move_values)
        or quadratic_index != len(quadratic_values)
        or cubic_index != len(cubic_values)
        or arc_index != len(arc_values)
    ):
        raise ValueError("Unconsumed native path coordinates")
    color, alpha = _rgb_alpha(body, required=True)
    return NativePath(tuple(decoded), fill_rule="nonzero", fill_color=color, fill_alpha=alpha)


def _decode_native_pattern_path(body: Message, budget: _DecodeBudget) -> NativePath:
    """Adapt the observed scale-one texture envelope to the shared path stream.

    Texture density is an observed rendering convention, not the PNG's declared
    physical resolution. Callers retain partial coverage for this reconstruction.
    """
    metadata = decode_message(cast(bytes, _one(body, DrawingField.METADATA, bytes)))
    if len(metadata.fields) != 1 or metadata.fields[0].number != 2:
        raise ValueError("Unsupported native pattern metadata")
    pattern = decode_message(cast(bytes, _one(metadata, 2, bytes)))
    if len(pattern.fields) != 4 or {field.number for field in pattern.fields} != {1, 3, 4, 5}:
        raise ValueError("Unsupported native pattern fields")
    attachment_id = _uuid(_one(pattern, 1, bytes))
    x, y = _fixed_point(cast(bytes, _one(pattern, 3, bytes)))
    width = _fixed(pattern, 4, 0.0)
    if not 0 < width <= MAX_PEN_WIDTH or _fixed(pattern, 5, 0.0) != 1.0:
        raise ValueError("Unsupported native pattern width or scale")
    origin_y = _native_float(y - width, label="pattern origin")
    path = _decode_native_path(body, budget, patterned=True)
    return replace(path, pattern=NativePattern(attachment_id, x, origin_y))
