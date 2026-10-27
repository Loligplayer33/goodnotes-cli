from __future__ import annotations

import math
from typing import cast

from .._vendor.wire import Message, decode_message
from ..native.records import (
    NativePath,
    NativePathCommand,
    _DecodeBudget,
    _NativeDecodeIssue,
    _UnsupportedNativeVariant,
)
from ..native.values import (
    _decode_tpl_payload,
    _fixed,
    _native_float,
    _native_translation,
    _one,
    _rgb_alpha,
    _tpl_float,
)

_NATIVE_PATH_FORMAT = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"


def _native_path_color(body: Message) -> tuple[str, float]:
    payload = cast(bytes, _one(body, 4, bytes))
    message = decode_message(payload)
    if len(message.fields) != 4 or {item.number for item in message.fields} != {1, 2, 3, 4}:
        raise ValueError("Native filled path requires explicit RGBA channels")
    return _rgb_alpha(body, required=True)


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


def _decode_native_path(body: Message, budget: _DecodeBudget) -> NativePath:
    """Decode the observed version-2 filled native CGPath arrays."""
    if any(item.number not in {1, 2, 3, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported native path body field")
    if _one(body, 3, int) != 1 or _one(body, 21, int) != 24:
        raise ValueError("Unsupported native path envelope version")
    metadata = _one(body, 20, bytes)
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
    unused_values = cast(list[int], values[7])
    cubic_values = cast(list[int], values[8])
    arc_values = cast(list[int], values[9])
    arc_directions = cast(list[int], values[10])
    subpath_count = len(subpath_counts)
    if not subpath_counts or any(count < 1 for count in subpath_counts):
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
    if metadata != (b"" if circle_variant else b"\x0a\x00"):
        raise ValueError("Unsupported native path metadata")
    if unused_values:
        raise ValueError("Unsupported native path auxiliary values")

    allowed_commands = {0, 2, 3} if circle_variant else {0, 2, 6}
    if any(code not in allowed_commands for code in command_codes):
        raise _NativeDecodeIssue("unsupported-native-path", "Unsupported native path command")
    move_count = command_codes.count(0)
    cubic_count = command_codes.count(2)
    arc_code = 3 if circle_variant else 6
    arc_count = command_codes.count(arc_code)
    if (
        len(move_values) != move_count * 2
        or len(cubic_values) != cubic_count * 6
        or len(arc_values) != arc_count * (5 if circle_variant else 7)
        or len(arc_directions) != arc_count
        or any(direction != 1 for direction in arc_directions)
    ):
        raise _NativeDecodeIssue(
            "unsupported-native-path", "Native path coordinate arrays do not match command stream"
        )
    budget.charge(point_count=move_count + cubic_count * 3 + arc_count * 2)

    # The sample arrays are retained by the source format but are redundant to
    # the exact move/cubic/arc path arrays. Validate their floats and shape,
    # without using them to invent additional centerline geometry.
    for raw in [*sample_metadata, *segment_metadata]:
        _tpl_float(raw, label="path sample metadata")
    if circle_variant and _tpl_float(sample_metadata[2], label="path sample radius") <= 0:
        raise ValueError("Invalid native path sample radius")
    if ellipse_variant and _tpl_float(sample_metadata[2], label="path sample radius") <= 0:
        raise ValueError("Invalid native path sample radius")

    move_index = cubic_index = arc_index = command_index = 0
    decoded: list[NativePathCommand] = []
    for count in subpath_counts:
        current_point: tuple[float, float] | None = None
        subpath_codes = command_codes[command_index : command_index + count]
        command_index += count
        if not subpath_codes or subpath_codes[0] != 0 or 0 in subpath_codes[1:]:
            raise _NativeDecodeIssue(
                "unsupported-native-path", "Native filled subpath must begin with one move command"
            )
        for code in subpath_codes:
            if code == 0:
                x = _tpl_float(move_values[move_index], label="path coordinate")
                y = _tpl_float(move_values[move_index + 1], label="path coordinate")
                move_index += 2
                current_point = (x, y)
                decoded.append(NativePathCommand("M", (x, y)))
            elif code == 2:
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
                    if radius_x <= 0 or radius_y <= 0 or abs(rotation) > 1e-6:
                        raise ValueError("Unsupported native elliptical arc geometry")
                start_x, start_y = _arc_end(
                    center_x, center_y, radius_x, radius_y, start_angle, rotation
                )
                _native_float(start_x, label="path arc start", reason="unsupported-native-path")
                _native_float(start_y, label="path arc start", reason="unsupported-native-path")
                # SVG endpoint arcs use the preceding point to choose their
                # center, and may enlarge their radii for a distant point.
                # Only the observed near-continuous native paths are supported.
                # Ellipse angle metadata has a larger established discrepancy
                # than circular metadata, while its points remain on the ellipse.
                start_tolerance = 0.001 + max(radius_x, radius_y) * (
                    0.001 if circle_variant else 0.02
                )
                if current_point is None:
                    raise _NativeDecodeIssue(
                        "unsupported-native-path", "Native path arc has no preceding point"
                    )
                radial_residual = abs(
                    ((current_point[0] - center_x) / radius_x) ** 2
                    + ((current_point[1] - center_y) / radius_y) ** 2
                    - 1.0
                )
                if (
                    math.dist(current_point, (start_x, start_y)) > start_tolerance
                    or radial_residual > 0.005
                ):
                    raise _NativeDecodeIssue(
                        "unsupported-native-path", "Unsupported native path arc start discontinuity"
                    )
                delta = (start_angle - end_angle) % math.tau
                # Identical endpoints can mean a full revolution or no arc.
                # The observed schema does not distinguish those cases.
                if min(delta, math.tau - delta) <= 1e-6:
                    raise ValueError("Ambiguous native full-circle arc")
                large_arc = 1.0 if delta > math.pi else 0.0
                end_x, end_y = _arc_end(center_x, center_y, radius_x, radius_y, end_angle, rotation)
                _native_float(end_x, label="path arc endpoint", reason="unsupported-native-path")
                _native_float(end_y, label="path arc endpoint", reason="unsupported-native-path")
                current_point = (end_x, end_y)
                decoded.append(
                    NativePathCommand(
                        "A",
                        (
                            radius_x,
                            radius_y,
                            math.degrees(rotation),
                            large_arc,
                            0.0,
                            end_x,
                            end_y,
                        ),
                    )
                )
        decoded.append(NativePathCommand("Z"))

    if (
        move_index != len(move_values)
        or cubic_index != len(cubic_values)
        or arc_index != len(arc_values)
    ):
        raise ValueError("Unconsumed native path coordinates")
    color, alpha = _native_path_color(body)
    return NativePath(tuple(decoded), fill_rule="nonzero", fill_color=color, fill_alpha=alpha)


def _fixed_point(payload: bytes) -> tuple[float, float]:
    message = decode_message(payload)
    if len(message.fields) != 2 or {item.number for item in message.fields} != {1, 2}:
        raise ValueError("Invalid native image point or size")
    x = _fixed(message, 1, 0.0)
    y = _fixed(message, 2, 0.0)
    if abs(x) > 100_000 or abs(y) > 100_000:
        raise ValueError("Native image geometry exceeds coordinate bounds")
    return x, y


def _native_shape_style(body: Message, budget: _DecodeBudget) -> tuple[float, str, float]:
    if _one(body, 21, int) != 24:
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
    width = _tpl_float(cast(int, values[1]), label="ellipse width")
    if not 0.05 <= width <= 100:
        raise ValueError("Invalid native shape width")
    color, alpha = _rgb_alpha(body, required=True)
    return width, color, alpha


def _decode_native_ellipse(
    body: Message,
    budget: _DecodeBudget,
    *,
    auxiliary: Message | None = None,
) -> NativePath:
    """Decode the observed outlined ellipse stored beside an empty ink TPL."""
    if any(item.number not in {1, 2, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported native ellipse body field")
    if _one(body, 6, bytes, required=False) not in {None, b""}:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-field-6")
    if _one(body, 20, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported native ellipse transform or metadata")
    width, color, alpha = _native_shape_style(body, budget)

    auxiliary = auxiliary or decode_message(cast(bytes, _one(body, 9, bytes)))
    if len(auxiliary.fields) != 3 or {f.number for f in auxiliary.fields} != {4, 5, 15}:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    if _one(auxiliary, 5, bytes) != b"\x10\x01":
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Uncertain native ellipse visibility or style"
        )
    shape = decode_message(cast(bytes, _one(auxiliary, 4, bytes)))
    if len(shape.fields) != 3 or {f.number for f in shape.fields} != {1, 2, 3}:
        raise ValueError("Unsupported native ellipse geometry fields")
    center_x, center_y = _fixed_point(cast(bytes, _one(shape, 1, bytes)))
    radius_x, radius_y = _fixed_point(cast(bytes, _one(shape, 2, bytes)))
    rotation = _fixed(shape, 3, 0.0)
    if radius_x <= 0 or radius_y <= 0 or abs(rotation) > math.tau:
        raise ValueError("Invalid native ellipse radii or rotation")
    auxiliary_width = _fixed(auxiliary, 15, 0.0)
    if not math.isclose(auxiliary_width, width, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("Native ellipse width fields disagree")
    budget.charge(point_count=13)
    cosine, sine = math.cos(rotation), math.sin(rotation)

    def transformed(x: float, y: float) -> tuple[float, float]:
        return (
            _native_float(center_x + x * cosine - y * sine, label="ellipse coordinate"),
            _native_float(center_y + x * sine + y * cosine, label="ellipse coordinate"),
        )

    kappa = 4 * (math.sqrt(2) - 1) / 3
    segments = (
        ((radius_x, kappa * radius_y), (kappa * radius_x, radius_y), (0.0, radius_y)),
        ((-kappa * radius_x, radius_y), (-radius_x, kappa * radius_y), (-radius_x, 0.0)),
        ((-radius_x, -kappa * radius_y), (-kappa * radius_x, -radius_y), (0.0, -radius_y)),
        ((kappa * radius_x, -radius_y), (radius_x, -kappa * radius_y), (radius_x, 0.0)),
    )
    commands = [NativePathCommand("M", transformed(radius_x, 0.0))]
    for segment in segments:
        commands.append(
            NativePathCommand(
                "C", tuple(value for point in segment for value in transformed(*point))
            )
        )
    return NativePath(
        tuple(commands),
        fill_color=None,
        fill_alpha=0.0,
        stroke_color=color,
        stroke_width=width,
        stroke_alpha=alpha,
    )


def _decode_native_two_point_shape(
    body: Message, auxiliary: Message, budget: _DecodeBudget
) -> NativePath:
    """Decode an observed open two-point shape path without closing or filling it."""
    if any(item.number not in {1, 2, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported native shape body field")
    dx, dy = _native_translation(
        body,
        unsupported_reason="unsupported-native-geometry-field-6",
        require_pair=True,
    )
    if _one(body, 20, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported native shape transform or metadata")
    width, color, alpha = _native_shape_style(body, budget)
    if len(auxiliary.fields) != 3 or {item.number for item in auxiliary.fields} != {1, 5, 15}:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    if _one(auxiliary, 5, bytes) != b"\x10\x01":
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Uncertain native shape visibility or style"
        )
    auxiliary_width = _fixed(auxiliary, 15, 0.0)
    if not math.isclose(auxiliary_width, width, rel_tol=1e-6, abs_tol=1e-6):
        raise ValueError("Native shape width fields disagree")

    point_stream = decode_message(cast(bytes, _one(auxiliary, 1, bytes)))
    if any(item.number != 1 or not isinstance(item.value, bytes) for item in point_stream.fields):
        raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
    if len(point_stream.fields) != 2:
        raise _UnsupportedNativeVariant("unsupported-native-geometry-point-count")
    points = [_fixed_point(cast(bytes, item.value)) for item in point_stream.fields]
    budget.charge(point_count=len(points))
    translated = [
        (
            _native_float(x + dx, label="native shape coordinate"),
            _native_float(y + dy, label="native shape coordinate"),
        )
        for x, y in points
    ]
    start, end = translated
    delta_x = end[0] - start[0]
    delta_y = end[1] - start[1]
    commands = (
        NativePathCommand("M", start),
        NativePathCommand(
            "C",
            (
                start[0] + delta_x / 3,
                start[1] + delta_y / 3,
                start[0] + 2 * delta_x / 3,
                start[1] + 2 * delta_y / 3,
                end[0],
                end[1],
            ),
        ),
    )
    return NativePath(
        commands,
        fill_color=None,
        fill_alpha=0.0,
        stroke_color=color,
        stroke_width=width,
        stroke_alpha=alpha,
    )


def _decode_native_shape(body: Message, budget: _DecodeBudget) -> NativePath:
    """Dispatch only observed shape geometry envelopes."""
    auxiliary = decode_message(cast(bytes, _one(body, 9, bytes)))
    numbers = {item.number for item in auxiliary.fields}
    if len(auxiliary.fields) == 3 and numbers == {1, 5, 15}:
        return _decode_native_two_point_shape(body, auxiliary, budget)
    if len(auxiliary.fields) == 3 and numbers == {4, 5, 15}:
        return _decode_native_ellipse(body, budget, auxiliary=auxiliary)
    raise _UnsupportedNativeVariant("unsupported-native-geometry-envelope")
