"""Conservative rendering of native pen/highlighter geometry from copied records.

The UUID namespace mapping and the supported TPL ink schemas were established
from copied data. This is an approximate reconstruction, not Goodnotes' renderer.
Unknown objects, version conflicts and decoding failures remain visible in the
coverage metadata rather than being reported as an empty or complete page.
"""

from __future__ import annotations

import math
import struct
from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, cast
from uuid import UUID

from ._vendor.compression import decode_apple_lz4
from ._vendor.tpl import TplImage, decode_tpl
from ._vendor.wire import Message, WireType, decode_message
from .models import Page
from .native import NativeError, SnapshotProtocol, read_page_records
from .native_assets import AttachmentSandbox, NativeImageData, load_native_image
from .native_render import RasterImage, native_image_element, native_path_element

_INK_FORMATS = {"vuA(v)A(S(uu))A(S(uuuu))", "vuA(v)A(S(uu))A(S(uuuu))vA(f)"}
_NATIVE_PATH_FORMAT = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
_VARIABLE_BRUSH_FORMAT = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"
_COORDINATE_SCALE = 72 / 132
_MAX_PAGE_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_PAGE_IMAGE_PIXELS = 25_000_000
_UnsupportedReason = Literal[
    "unsupported-native-image-field-6",
    "image-binding-incomplete",
    "unsupported-native-geometry-envelope",
    "unsupported-native-geometry-field-6",
    "unsupported-native-geometry-point-count",
]


class _UnsupportedNativeVariant(ValueError):
    """A recognized but unsupported variant with a static public reason code."""

    def __init__(self, reason: _UnsupportedReason) -> None:
        super().__init__()
        self.reason = reason


@dataclass(frozen=True)
class NativeOverlay:
    svg: bytes
    metadata: dict[str, object]


@dataclass(frozen=True)
class Ink:
    points: tuple[tuple[float, float], ...]
    width: float
    color: str
    alpha: float
    dash: tuple[float, ...] = ()
    widths: tuple[float, ...] = ()


@dataclass(frozen=True)
class NativePathCommand:
    op: Literal["M", "C", "A", "Z"]
    values: tuple[float, ...] = ()


@dataclass(frozen=True)
class NativePath:
    commands: tuple[NativePathCommand, ...]
    fill_color: str | None
    fill_alpha: float
    fill_rule: Literal["nonzero"] = "nonzero"
    stroke_color: str | None = None
    stroke_width: float = 0.0
    stroke_alpha: float = 1.0


@dataclass(frozen=True)
class NativeImage:
    attachment_id: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class DecodedNativeObject:
    actor: int
    sequence: int
    logical_id: str
    payload: Ink | NativePath | NativeImage
    part_index: int = 0


@dataclass
class _DecodeBudget:
    bytes_left: int = 16 * 1024 * 1024
    points_left: int = 100_000

    def charge(self, *, byte_count: int = 0, point_count: int = 0) -> None:
        self.bytes_left -= byte_count
        self.points_left -= point_count
        if self.bytes_left < 0 or self.points_left < 0:
            raise NativeError(
                "native_data_unavailable",
                "Native page decoding exceeds its decompressed-byte or point budget.",
                operation="decode_native_page",
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
    if budget.bytes_left <= 0:
        budget.charge(byte_count=1)
    decompressed, consumed = decode_apple_lz4(
        payload, max_output=min(4 * 1024 * 1024, budget.bytes_left)
    )
    budget.charge(byte_count=len(decompressed))
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
            raise ValueError(schema_error)
    return decode_tpl(decompressed)


def _native_float(value: float, *, label: str, limit: float = 100_000) -> float:
    result = value
    if not math.isfinite(result) or abs(result) > limit:
        raise ValueError(f"Invalid native {label}")
    return result


def _tpl_float(value: int, *, label: str, limit: float = 100_000) -> float:
    return _native_float(_float(value), label=label, limit=limit)


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


def _decode_variable_brush(body: Message, budget: _DecodeBudget) -> list[Ink]:
    """Decode the observed version-1 variable-width brush path."""
    if any(item.number not in {1, 2, 3, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported variable brush body field")
    if _one(body, 3, int) != 5 or _one(body, 21, int) != 25:
        raise ValueError("Unsupported variable brush envelope version")
    for number in (6, 9, 20):
        if _one(body, number, bytes, required=False) not in {None, b""}:
            raise ValueError("Unsupported variable brush auxiliary geometry")
    for number in (7, 15):
        _one(body, number, bytes, required=False)

    image = _decode_tpl_payload(
        body,
        budget,
        expected_format=_VARIABLE_BRUSH_FORMAT,
        schema_error="Unsupported variable brush TPL schema",
    )
    if len(image.values) != 10:
        raise ValueError("Unsupported variable brush TPL schema")
    values = image.values
    if values[0] != 1:
        raise ValueError("Unsupported variable brush TPL version")
    width = _tpl_float(cast(int, values[1]), label="brush width")
    if not 0.05 <= width <= 100:
        raise ValueError("Invalid native brush width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, ...]], values[3])
    samples = cast(list[tuple[int, ...]], values[4])
    if len(starts) != 1 or len(starts[0]) != 5 or not samples:
        raise ValueError("Invalid variable brush start or sample stream")
    if len(flags) != len(samples) + 1 or flags != [0, *([1] * len(samples))]:
        raise ValueError("Uncertain variable brush visibility pattern")
    if any(values[index] for index in range(5, 10)):
        raise ValueError("Unsupported variable brush auxiliary arrays")
    if any(len(sample) != 11 for sample in samples):
        raise ValueError("Invalid variable brush sample width")
    budget.charge(point_count=1 + len(samples) * 2)
    # The first sample word is opaque metadata, not a float coordinate.
    for row in [*starts, *(sample[1:] for sample in samples)]:
        for raw in row:
            _tpl_float(raw, label="brush sample")

    start = _point(starts[0][0], starts[0][1])
    points: list[tuple[float, float]] = [start]
    widths: list[float] = [width]
    for sample in samples:
        first = _point(sample[1], sample[2])
        second = _point(sample[6], sample[7])
        first_width = _tpl_float(sample[5], label="brush sample width")
        second_width = _tpl_float(sample[10], label="brush sample width")
        if not 0.05 <= first_width <= 100 or not 0.05 <= second_width <= 100:
            raise ValueError("Invalid native brush sample width")
        points.extend((first, second))
        widths.extend((first_width, second_width))

    color, alpha = _rgb_alpha(body)
    return [Ink(tuple(points), width, color, alpha, widths=tuple(widths))]


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
        raise ValueError("Unsupported native path TPL schema")
    values = image.values
    if values[0] != 2:
        raise ValueError("Unsupported native path TPL version")

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
        raise ValueError("Invalid native path subpath command counts")
    if len(command_codes) != sum(subpath_counts):
        raise ValueError("Native path command count does not match subpaths")
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
        raise ValueError("Unsupported native path command")
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
        raise ValueError("Native path coordinate arrays do not match command stream")
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
            raise ValueError("Native filled subpath must begin with one move command")
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
                    _tpl_float(value, label="path arc")
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
                _native_float(start_x, label="path arc start")
                _native_float(start_y, label="path arc start")
                # SVG endpoint arcs use the preceding point to choose their
                # center, and may enlarge their radii for a distant point.
                # Only the observed near-continuous native paths are supported.
                # Ellipse angle metadata has a larger established discrepancy
                # than circular metadata, while its points remain on the ellipse.
                start_tolerance = 0.001 + max(radius_x, radius_y) * (
                    0.001 if circle_variant else 0.02
                )
                if current_point is None:
                    raise ValueError("Native path arc has no preceding point")
                radial_residual = abs(
                    ((current_point[0] - center_x) / radius_x) ** 2
                    + ((current_point[1] - center_y) / radius_y) ** 2
                    - 1.0
                )
                if (
                    math.dist(current_point, (start_x, start_y)) > start_tolerance
                    or radial_residual > 0.005
                ):
                    raise ValueError("Unsupported native path arc start discontinuity")
                delta = (start_angle - end_angle) % math.tau
                # Identical endpoints can mean a full revolution or no arc.
                # The observed schema does not distinguish those cases.
                if min(delta, math.tau - delta) <= 1e-6:
                    raise ValueError("Ambiguous native full-circle arc")
                large_arc = 1.0 if delta > math.pi else 0.0
                end_x, end_y = _arc_end(center_x, center_y, radius_x, radius_y, end_angle, rotation)
                _native_float(end_x, label="path arc endpoint")
                _native_float(end_y, label="path arc endpoint")
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
        raise ValueError("Unsupported native shape TPL version")
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
        raise ValueError("Uncertain native ellipse visibility or style")
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
        raise ValueError("Uncertain native shape visibility or style")
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


def _native_image_rect(payload: bytes) -> tuple[float, float, float, float]:
    message = decode_message(payload)
    if len(message.fields) != 2 or {item.number for item in message.fields} != {1, 2}:
        raise ValueError("Invalid native image rectangle")
    origin_payload = cast(bytes, _one(message, 1, bytes))
    size_payload = cast(bytes, _one(message, 2, bytes))
    x, y = _fixed_point(origin_payload)
    width, height = _fixed_point(size_payload)
    if width <= 0 or height <= 0 or width > 100_000 or height > 100_000:
        raise ValueError("Invalid native image dimensions")
    if abs(x + width) > 100_000 or abs(y + height) > 100_000:
        raise ValueError("Native image rectangle exceeds coordinate bounds")
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
        raise ValueError("Native image logical identity mismatch")
    original_resource = _uuid(_one(body, 4, bytes))
    field_6 = body.by_number(6)
    if field_6 and (
        len(field_6) != 1
        or field_6[0].wire_type != WireType.VARINT
        or not isinstance(field_6[0].value, int)
        or field_6[0].value != 1
    ):
        raise _UnsupportedNativeVariant("unsupported-native-image-field-6")
    if _one(body, 18, int) != 24:
        raise ValueError("Unsupported native image version or transform")
    for number in (5, 15):
        opaque = cast(bytes, _one(body, number, bytes))
        if len(opaque) > 64:
            raise ValueError("Native image metadata exceeds its byte budget")

    x, y, width, height = _native_image_rect(cast(bytes, _one(body, 2, bytes)))
    center_x, center_y, center_width, center_height = _native_image_rect(
        cast(bytes, _one(body, 3, bytes))
    )
    tolerance = max(1e-3, max(abs(x), abs(y), width, height) * 1e-6)
    if (
        not math.isclose(center_x, x + width / 2, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_y, y + height / 2, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_width, width, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_height, height, rel_tol=1e-6, abs_tol=tolerance)
    ):
        raise ValueError("Native image center and top-left bounds disagree")

    descriptor_resource = _one(descriptor, 4, bytes, required=False)
    descriptor_attachment = _one(descriptor, 7, bytes, required=False)
    if descriptor_resource is None and descriptor_attachment is None:
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is None:
        if _uuid(descriptor_resource) != original_resource:
            raise ValueError("Native image resource identity mismatch")
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is not None:
        if _uuid(descriptor_resource) != original_resource:
            raise ValueError("Native image resource identity mismatch")
        attachment_id = _uuid(descriptor_attachment)
    else:
        raise _UnsupportedNativeVariant("image-binding-incomplete")
    return NativeImage(attachment_id, x, y, width, height)


def _segment_paths(
    starts: list[tuple[float, float]],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Decode only start/segment relationships established by observed records."""
    if not segments:
        return [[point] for point in starts]

    # One start plus the ordered four-u sample stream is the observed stroke
    # representation. Its tuples are not endpoint-connected graph edges.
    # Multiple starts may denote additional subpaths, but their relationship
    # to this stream is not established; do not infer boundaries by proximity.
    if len(starts) > 1:
        raise ValueError("Ambiguous native ink starts and segments relationship")

    def ordered_path(
        initial: tuple[float, float] | None,
        segment_slice: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []

        def append(point: tuple[float, float]) -> None:
            if not points or math.dist(points[-1], point) >= 1e-3:
                points.append(point)

        if initial is not None:
            append(initial)
        for segment_start, segment_end in segment_slice:
            append(segment_start)
            append(segment_end)
        return points

    if len(starts) == 1:
        return [ordered_path(starts[0], segments)]

    if not starts:
        # With no explicit path starts, only a single stored segment has an
        # unambiguous interpretation. Several segments could be one sampled
        # path or multiple paths; do not guess at their relationship.
        if len(segments) > 1:
            raise ValueError("Ambiguous native ink segments without start markers")
        return [ordered_path(None, segments)]
    raise ValueError("Ambiguous native ink starts and segments relationship")


def decode_ink(body: Message, budget: _DecodeBudget | None = None) -> list[Ink]:
    """Decode the observed legacy pen schemas, preserving colour and lasso moves."""
    if any(item.number not in {1, 2, 3, 4, 5, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported ink body field")
    # These envelope fields were present alongside the supported ink schema.
    # Clocks in7/15 remain opaque. New style/visibility fields must not silently
    # acquire guessed semantics.
    for number in (7, 15):
        _one(body, number, bytes, required=False)
    if _one(body, 3, int, required=False) not in {None, 5}:
        raise ValueError("Unsupported ink body variant")
    if _one(body, 5, int, required=False) not in {None, 1}:
        raise ValueError("Unsupported ink style flag")
    if _one(body, 9, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary geometry")
    if _one(body, 20, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary body field")
    if _one(body, 21, int, required=False) not in {None, 24}:
        raise ValueError("Unsupported ink body type")
    budget = budget or _DecodeBudget()
    image = _decode_tpl_payload(
        body, budget, expected_format=_INK_FORMATS, schema_error="Unsupported ink TPL schema"
    )
    values = image.values
    if values[0] not in {0, 2} or (
        len(values) == 7 and (values[0], values[5]) not in {(0, 0), (2, 1)}
    ):
        raise ValueError("Unsupported ink TPL version")
    width = _float(cast(int, values[1]))
    if not math.isfinite(width) or not 0.05 <= width <= 100:
        raise ValueError("Invalid native pen width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, int]], values[3])
    segments = cast(list[tuple[int, int, int, int]], values[4])
    budget.charge(point_count=len(starts) + len(segments) * 2)
    if any(value not in {0, 1} for value in flags):
        raise ValueError("Unsupported native visibility flags")
    if flags and len(flags) != len(segments) + 1:
        raise ValueError("Native visibility flags do not match segments")
    if any(value == 0 for value in flags[1:]):
        raise ValueError("Uncertain native ink visibility pattern")
    color, alpha = _rgb_alpha(body)
    dx, dy = _native_translation(body, unsupported_message="Unsupported ink transform field")
    start_points = [_point(*start) for start in starts]
    segment_points = [
        (_point(segment[0], segment[1]), _point(segment[2], segment[3])) for segment in segments
    ]
    groups = _segment_paths(start_points, segment_points)
    dash: tuple[float, ...] = ()
    if len(values) == 7:
        candidate = cast(list[float], values[6])
        if candidate:
            if any(not math.isfinite(value) or value < 0 or value > 10_000 for value in candidate):
                raise ValueError("Unsupported native dash pattern")
            if any(candidate):
                dash = tuple(candidate)
    result = [
        Ink(
            tuple(
                (
                    _native_float(x + dx, label="translated ink coordinate"),
                    _native_float(y + dy, label="translated ink coordinate"),
                )
                for x, y in group
            ),
            width,
            color,
            alpha,
            dash,
        )
        for group in groups
        if group
    ]
    if not result:
        raise ValueError("Ink has no decoded points")
    return result


def _unit_direction(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[float, float] | None:
    if any(not math.isfinite(value) for value in (*start, *end)):
        raise ValueError("Native stroke contains a nonfinite point")
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if not math.isfinite(length):
        raise ValueError("Native stroke segment exceeds the supported range")
    if length <= 1e-9:
        return None
    return dx / length, dy / length


def _variable_width_outline(
    points: tuple[tuple[float, float], ...], widths: tuple[float, ...], scale: float
) -> str:
    """Build a bounded filled outline for a centerline with sampled diameters."""
    if len(points) != len(widths) or not points:
        raise ValueError("Native width profile does not match its point stream")
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Native width profile scale must be finite and positive")

    samples: list[tuple[tuple[float, float], float]] = []
    for point, width in zip(points, widths, strict=True):
        if (
            any(not math.isfinite(value) or abs(value) > 100_000 for value in point)
            or not math.isfinite(width)
            or width < 0
            or width > 100
        ):
            raise ValueError("Native point width is outside the supported range")
        scaled_point = (point[0] * scale, point[1] * scale)
        scaled_width = width * scale
        if any(not math.isfinite(value) for value in (*scaled_point, scaled_width)):
            raise ValueError("Native width profile exceeds the supported range")
        if samples and math.dist(samples[-1][0], scaled_point) <= 1e-9:
            previous_point, previous_width = samples[-1]
            samples[-1] = (previous_point, max(previous_width, scaled_width))
        else:
            samples.append((scaled_point, scaled_width))

    if len(samples) == 1:
        (x, y), diameter = samples[0]
        return (
            f"M {x:.6f} {y - diameter / 2:.6f} "
            f"A {diameter / 2:.6f} {diameter / 2:.6f} 0 1 0 "
            f"{x:.6f} {y + diameter / 2:.6f} "
            f"A {diameter / 2:.6f} {diameter / 2:.6f} 0 1 0 "
            f"{x:.6f} {y - diameter / 2:.6f} Z"
        )

    sample_points = [sample[0] for sample in samples]
    radii = [sample[1] / 2 for sample in samples]
    directions = [
        _unit_direction(sample_points[index], sample_points[index + 1])
        for index in range(len(sample_points) - 1)
    ]
    offsets: list[tuple[float, float]] = []
    for index, radius in enumerate(radii):
        before = next(
            (directions[pos] for pos in range(index - 1, -1, -1) if directions[pos]), None
        )
        after = next(
            (directions[pos] for pos in range(index, len(directions)) if directions[pos]), None
        )
        if before is None and after is None:
            normal = (0.0, 1.0)
            miter = 1.0
        elif before is None:
            assert after is not None
            normal = (-after[1], after[0])
            miter = 1.0
        elif after is None:
            normal = (-before[1], before[0])
            miter = 1.0
        else:
            before_normal = (-before[1], before[0])
            after_normal = (-after[1], after[0])
            combined = (before_normal[0] + after_normal[0], before_normal[1] + after_normal[1])
            length = math.hypot(*combined)
            if length <= 1e-9:
                normal = after_normal
                miter = 1.0
            else:
                normal = (combined[0] / length, combined[1] / length)
                projection = abs(normal[0] * after_normal[0] + normal[1] * after_normal[1])
                miter = min(4.0, 1 / max(projection, 0.25))
        offsets.append((normal[0] * radius * miter, normal[1] * radius * miter))

    left = [
        (point[0] + offset[0], point[1] + offset[1])
        for point, offset in zip(sample_points, offsets, strict=True)
    ]
    right = [
        (point[0] - offset[0], point[1] - offset[1])
        for point, offset in zip(sample_points, offsets, strict=True)
    ]
    path = [f"M {left[0][0]:.6f} {left[0][1]:.6f}"]
    path.extend(f"L {x:.6f} {y:.6f}" for x, y in left[1:])
    end_radius = radii[-1]
    if end_radius > 1e-9:
        path.append(
            f"A {end_radius:.6f} {end_radius:.6f} 0 0 0 {right[-1][0]:.6f} {right[-1][1]:.6f}"
        )
    else:
        path.append(f"L {right[-1][0]:.6f} {right[-1][1]:.6f}")
    path.extend(f"L {x:.6f} {y:.6f}" for x, y in reversed(right[:-1]))
    start_radius = radii[0]
    if start_radius > 1e-9:
        path.append(
            f"A {start_radius:.6f} {start_radius:.6f} 0 0 0 {left[0][0]:.6f} {left[0][1]:.6f}"
        )
    else:
        path.append(f"L {left[0][0]:.6f} {left[0][1]:.6f}")
    path.append("Z")
    return " ".join(path)


def _svg(
    items: list[DecodedNativeObject] | list[Ink],
    width: float,
    height: float,
    scale: float,
    *,
    image_data: Mapping[str, RasterImage] | None = None,
) -> bytes:
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.6f}pt" '
        f'height="{height:.6f}pt" viewBox="0 0 {width:.6f} {height:.6f}">'
    ]
    for item in items:
        payload = item.payload if isinstance(item, DecodedNativeObject) else item
        if isinstance(payload, NativePath):
            elements.append(native_path_element(payload, scale))
            continue
        if isinstance(payload, NativeImage):
            raster = image_data.get(payload.attachment_id) if image_data is not None else None
            if raster is None:
                raise ValueError("Native image bytes were not resolved from the copied snapshot")
            elements.append(native_image_element(payload, raster, scale))
            continue
        if not isinstance(payload, Ink):
            raise ValueError("Unsupported decoded native rendering payload")

        ink = payload
        points = ink.points
        if ink.widths and len(points) > 1 and not ink.dash:
            outline = _variable_width_outline(points, ink.widths, scale)
            elements.append(
                f'<path d="{outline}" fill="{ink.color}" fill-opacity="{ink.alpha:.6f}"/>'
            )
            continue
        if len(points) == 1:
            x, y = points[0]
            if ink.widths and len(ink.widths) != 1:
                raise ValueError("Native width profile does not match its point stream")
            diameter = ink.widths[0] if ink.widths else ink.width
            elements.append(
                f'<circle cx="{x * scale:.6f}" cy="{y * scale:.6f}" '
                f'r="{diameter * scale / 2:.6f}" fill="{ink.color}" '
                f'fill-opacity="{ink.alpha:.6f}"/>'
            )
        else:
            coordinates = " ".join(f"{x * scale:.6f},{y * scale:.6f}" for x, y in points)
            dash = (
                (
                    ' stroke-dasharray="'
                    + " ".join(f"{value * scale:.6f}" for value in ink.dash)
                    + '"'
                )
                if ink.dash
                else ""
            )
            elements.append(
                f'<polyline points="{coordinates}" fill="none" '
                f'stroke="{ink.color}" stroke-opacity="{ink.alpha:.6f}" '
                f'stroke-width="{ink.width * scale:.6f}" stroke-linecap="round" '
                f'stroke-linejoin="round"{dash}/>'
            )
    elements.append("</svg>")
    return "\n".join(elements).encode()


def _unsupported_reason(error: Exception) -> str:
    if isinstance(error, _UnsupportedNativeVariant):
        return error.reason
    message = str(error)
    if "TPL schema" in message or "TPL version" in message:
        return "unsupported-annotation-schema"
    if "visibility" in message.lower():
        return "visibility-uncertain"
    if any(part in message.lower() for part in ("command", "subpath", "path arc")):
        return "unsupported-native-path"
    if "image" in message.lower() and "identity" in message.lower():
        return "image-binding-invalid"
    return "decode-failed"


def decode_native_objects(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[DecodedNativeObject], dict[str, object]]:
    """Decode supported annotation payloads while retaining source order metadata."""
    categories: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    objects: dict[tuple[str, str], dict[str, bytes]] = defaultdict(dict)
    for key, value in records:
        try:
            root, category, storage_id, kind = key.decode("ascii").split(".")
            if root != root_uuid or kind not in {"descriptor", "item"}:
                raise ValueError("Unsupported native object key")
            _uuid(storage_id.encode())
            categories[category] += 1
            if kind in objects[(category, storage_id)]:
                raise ValueError("Duplicate native key")
            objects[(category, storage_id)][kind] = value
        except (UnicodeError, ValueError):
            reasons["invalid-record-key"] += 1
    candidates: list[tuple[int, int, str, Sequence[Ink | NativePath | NativeImage]]] = []
    budget = _DecodeBudget()
    logical_counts: Counter[str] = Counter()
    descriptor_variants: Counter[str] = Counter()
    unmodeled_descriptor_fields: set[int] = set()
    unmodeled_empty_body_fields: set[int] = set()
    unmodeled_body_fields: set[int] = set()
    unmodeled_image_fields: set[int] = set()
    unmodeled_tpl_fields: dict[str, set[int]] = defaultdict(set)
    image_body_field_6_absent = 0
    erased = deleted = 0
    for (category, storage_id), pair in objects.items():
        if category == "deleted":
            deleted += 1
            continue
        if category != "normal":
            reasons["unknown-record-category"] += 1
            continue
        try:
            if set(pair) != {"descriptor", "item"}:
                raise ValueError("Missing annotation descriptor/item pair")
            descriptor = decode_message(pair["descriptor"])
            if any(
                item.number not in {1, 2, 3, 4, 6, 7, 8, 9, 14, 16, 20, 21}
                for item in descriptor.fields
            ):
                raise ValueError("Unsupported native descriptor field")
            logical_id = _uuid(_one(descriptor, 1, bytes))
            logical_counts[logical_id] += 1
            alias = _one(descriptor, 6, bytes, required=False)
            bound_id = _uuid(alias) if alias is not None else logical_id
            if bound_id != storage_id.upper():
                raise ValueError("Annotation storage identity mismatch")
            metadata_fields = descriptor.by_number(14)
            if metadata_fields:
                if (
                    len(metadata_fields) != 1
                    or metadata_fields[0].wire_type != WireType.VARINT
                    or not isinstance(metadata_fields[0].value, int)
                    or not 0 <= metadata_fields[0].value <= (1 << 64) - 1
                ):
                    raise ValueError("Unsupported native descriptor metadata")
                unmodeled_descriptor_fields.add(14)
            is_erased = _one(descriptor, 3, int, required=False)
            if is_erased not in {None, 0, 1}:
                raise ValueError("Unknown erased flag")
            if is_erased == 1:
                erased += 1
                continue
            object_type = _one(descriptor, 16, int)
            if object_type not in {24, 25}:
                reasons["unsupported-object-type"] += 1
                continue
            if descriptor.by_number(20) or descriptor.by_number(21):
                reasons["unsupported-parent-transform"] += 1
                continue
            actor = cast(int, _one(descriptor, 8, int))
            sequence = cast(int, _one(descriptor, 9, int))
            item = decode_message(pair["item"])
            item_numbers = {field.number for field in item.fields}
            if item_numbers == {1}:
                if object_type != 24:
                    raise ValueError("Unsupported non-ink native image type")
                image_body = decode_message(cast(bytes, _one(item, 1, bytes)))
                image = _decode_native_image(descriptor, item, logical_id)
                unmodeled_image_fields.update({5, 15})
                if image_body.by_number(6):
                    unmodeled_image_fields.add(6)
                else:
                    image_body_field_6_absent += 1
                descriptor_variants[
                    "image-attachment-alias" if descriptor.by_number(7) else "direct-image-resource"
                ] += 1
                candidates.append((actor, sequence, logical_id, [image]))
            elif item_numbers == {7}:
                body = decode_message(cast(bytes, _one(item, 7, bytes)))
                if _uuid(_one(body, 1, bytes)) != logical_id:
                    raise ValueError("Annotation logical identity mismatch")
                if descriptor.by_number(4) or descriptor.by_number(7):
                    raise ValueError("Image attachment binding on a non-image object")
                body_variant = _one(body, 3, int, required=False)
                if object_type == 25:
                    payloads: list[Ink | NativePath | NativeImage] = [
                        *_decode_variable_brush(body, budget)
                    ]
                    unmodeled_body_fields.add(3)
                    unmodeled_tpl_fields["variable-brush"].update({0, 3, 4, 8, 9})
                elif body_variant == 1:
                    payloads = [_decode_native_path(body, budget)]
                    unmodeled_body_fields.add(3)
                    unmodeled_body_fields.add(20)
                    unmodeled_empty_body_fields.add(20)
                    unmodeled_tpl_fields["native-path"].update({2, 3})
                elif _one(body, 9, bytes, required=False) not in {None, b""}:
                    payloads = [_decode_native_shape(body, budget)]
                    unmodeled_body_fields.add(9)
                else:
                    payloads = [*decode_ink(body, budget)]
                    if body_variant is not None:
                        unmodeled_body_fields.add(3)
                if body.by_number(20):
                    unmodeled_empty_body_fields.add(20)
                variant = "storage-alias" if alias is not None else "direct-identity"
                descriptor_variants[variant] += 1
                candidates.append((actor, sequence, logical_id, payloads))
            else:
                raise ValueError("Unsupported native annotation item envelope")
        except (ValueError, UnicodeError, IndexError, TypeError, struct.error) as exc:
            reasons[_unsupported_reason(exc)] += 1
    duplicate_ids = {logical_id for logical_id, count in logical_counts.items() if count > 1}
    if duplicate_ids:
        reasons["ambiguous-logical-version"] += sum(
            1 for row in candidates if row[2] in duplicate_ids
        )
        candidates = [row for row in candidates if row[2] not in duplicate_ids]
    actors = {row[0] for row in candidates}
    creation_clocks = Counter((row[0], row[1]) for row in candidates)
    order_ties = any(count > 1 for count in creation_clocks.values())
    warnings = [
        "Native annotations are an approximate reconstruction. Unobserved object types, "
        "some pen dynamics, and application drawing order remain unverified.",
        "Page identity uses the observed projection UUID plus one native namespace mapping.",
    ]
    if len(actors) > 1:
        warnings.append(
            "Multiple creation actors are present. Cross-device drawing order is unknown."
        )
    if order_ties:
        warnings.append(
            "Some annotation creation clocks are equal. Their drawing order is unknown."
        )
    if deleted:
        warnings.append(
            "Deleted-category objects were excluded. Their application visibility was "
            "not independently verified."
        )
    if unmodeled_descriptor_fields:
        warnings.append(
            "Native descriptor field 14 is preserved as opaque metadata. Its effect on "
            "application rendering is unverified, so reconstruction coverage is partial."
        )
    if unmodeled_body_fields:
        warnings.append(
            "Observed native body fields "
            + ", ".join(str(number) for number in sorted(unmodeled_body_fields))
            + " include values whose independent rendering effect is unverified."
        )
    if unmodeled_image_fields:
        warnings.append(
            "Native image body fields "
            + ", ".join(str(number) for number in sorted(unmodeled_image_fields))
            + " are preserved as opaque metadata. Image bytes are resolved separately from "
            "the copied attachment store."
        )
    if image_body_field_6_absent:
        warnings.append(
            "Some decoded native image records omit body field 6; its effect on "
            "application appearance or visibility is unverified."
        )
    if unmodeled_tpl_fields:
        warnings.append(
            "Validated native template arrays "
            + ", ".join(sorted(unmodeled_tpl_fields))
            + " contain additional opaque samples that do not drive reconstructed geometry."
        )
    if reasons:
        warnings.append(
            "Some native records could not be reconstructed. Do not treat this image "
            "as complete evidence of all page annotations."
        )
    ordered_objects = [
        DecodedNativeObject(actor, sequence, logical_id, payload, part_index)
        for actor, sequence, logical_id, payloads in sorted(
            candidates, key=lambda row: (row[1], row[0], row[2])
        )
        for part_index, payload in enumerate(payloads)
    ]
    stroke_count = sum(
        1
        for native_object in ordered_objects
        if isinstance(native_object.payload, (Ink, NativePath))
    )
    image_count = sum(
        isinstance(native_object.payload, NativeImage) for native_object in ordered_objects
    )
    path_count = sum(
        isinstance(native_object.payload, NativePath) for native_object in ordered_objects
    )
    metadata: dict[str, object] = {
        "annotation_status": "native-reconstructed-approximate",
        "source": "copied-native-records",
        "coverage": (
            "partial"
            if reasons
            or deleted
            or len(actors) > 1
            or order_ties
            or unmodeled_descriptor_fields
            or unmodeled_body_fields
            or unmodeled_image_fields
            or image_body_field_6_absent
            or unmodeled_tpl_fields
            else "supported-native-records"
            if image_count or path_count
            else "supported-pen-records"
        ),
        "full_fidelity_verified": False,
        "page_mapping": "projection-uuid-plus-one-observed",
        "native_records": len(records),
        "native_objects": len(objects),
        "rendered_objects": len(candidates),
        "rendered_strokes": stroke_count,
        "rendered_paths": path_count,
        "rendered_images": image_count,
        "erased_objects": erased,
        "deleted_objects": deleted,
        "unsupported_objects": sum(reasons.values()),
        "unsupported_reasons": dict(reasons),
        "categories": dict(categories),
        "descriptor_variants": dict(descriptor_variants),
        "unmodeled_descriptor_fields": sorted(unmodeled_descriptor_fields),
        "unmodeled_empty_body_fields": sorted(unmodeled_empty_body_fields),
        "unmodeled_body_fields": sorted(unmodeled_body_fields),
        "unmodeled_image_fields": sorted(unmodeled_image_fields),
        "native_image_body_field_6_absent_records": image_body_field_6_absent,
        "unmodeled_tpl_fields": {
            name: sorted(field_numbers)
            for name, field_numbers in sorted(unmodeled_tpl_fields.items())
        },
        "unmodeled_observed_envelope_fields": [7, 15],
        "warnings": warnings,
    }
    return ordered_objects, metadata


def decode_native_records(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[Ink], dict[str, object]]:
    """Compatibility view returning only decoded pen/highlighter strokes."""
    objects, metadata = decode_native_objects(records, root_uuid)
    return [obj.payload for obj in objects if isinstance(obj.payload, Ink)], metadata


def _resolve_native_images(
    sandbox: AttachmentSandbox,
    page: Page,
    objects: list[DecodedNativeObject],
) -> tuple[list[DecodedNativeObject], dict[str, NativeImageData], Counter[str]]:
    resolved: dict[str, NativeImageData] = {}
    unavailable: set[str] = set()
    over_budget: set[str] = set()
    included: list[DecodedNativeObject] = []
    failures: Counter[str] = Counter()
    total_bytes = total_pixels = 0
    for native_object in objects:
        image = native_object.payload
        if not isinstance(image, NativeImage):
            included.append(native_object)
            continue
        attachment_id = image.attachment_id
        if attachment_id in unavailable:
            failures["native-image-unavailable"] += 1
            continue
        if attachment_id in over_budget:
            failures["native-image-budget-exceeded"] += 1
            continue
        raster = resolved.get(attachment_id)
        if raster is None:
            try:
                raster = load_native_image(sandbox, page.document_id, attachment_id)
            except NativeError:
                unavailable.add(attachment_id)
                failures["native-image-unavailable"] += 1
                continue

        next_bytes = total_bytes + len(raster.data)
        next_pixels = total_pixels + raster.width * raster.height
        if next_bytes > _MAX_PAGE_IMAGE_BYTES or next_pixels > _MAX_PAGE_IMAGE_PIXELS:
            over_budget.add(attachment_id)
            failures["native-image-budget-exceeded"] += 1
            continue
        total_bytes, total_pixels = next_bytes, next_pixels
        resolved[attachment_id] = raster
        included.append(native_object)
    return included, resolved, failures


def _record_image_failures(metadata: dict[str, object], failures: Counter[str]) -> None:
    failed_count = sum(failures.values())
    if not failed_count:
        return
    metadata["rendered_objects"] = max(
        0, cast(int, metadata.get("rendered_objects", 0)) - failed_count
    )
    metadata["rendered_images"] = max(
        0, cast(int, metadata.get("rendered_images", 0)) - failed_count
    )
    metadata["unsupported_objects"] = (
        cast(int, metadata.get("unsupported_objects", 0)) + failed_count
    )
    reasons = metadata.get("unsupported_reasons")
    updated_reasons = dict(reasons) if isinstance(reasons, dict) else {}
    for reason, count in failures.items():
        updated_reasons[reason] = int(updated_reasons.get(reason, 0)) + count
    metadata["unsupported_reasons"] = updated_reasons
    warnings = metadata.get("warnings")
    updated_warnings = list(warnings) if isinstance(warnings, list) else []
    updated_warnings.append(
        "Some copied native images were unavailable or exceeded the page image budget. "
        "Their pixels were omitted from this partial reconstruction."
    )
    metadata["warnings"] = updated_warnings
    metadata["coverage"] = "partial"


def render_native_overlay(
    sandbox: SnapshotProtocol,
    page: Page,
    *,
    width_points: float,
    height_points: float,
    record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None = None,
) -> NativeOverlay:
    if page.rotation != 0:
        raise NativeError(
            "native_data_unavailable",
            "Native rendering does not support rotated Goodnotes pages yet.",
            operation="map_native_page",
        )
    if page.width is None or page.height is None:
        raise NativeError(
            "native_data_unavailable",
            "Native rendering requires the copied page's coordinate dimensions.",
            operation="map_native_page",
        )
    dimensions = [page.width, page.height, width_points, height_points]
    if any(
        isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in dimensions
    ):
        raise NativeError(
            "native_data_unavailable",
            "Invalid native page dimensions.",
            operation="map_native_page",
        )
    scale_x, scale_y = width_points / page.width, height_points / page.height
    if abs(scale_x - _COORDINATE_SCALE) > 0.0001 or abs(scale_y - _COORDINATE_SCALE) > 0.0001:
        raise NativeError(
            "native_data_unavailable",
            "PDF and native coordinate dimensions do not match the verified scale.",
            operation="map_native_page",
        )
    try:
        root_uuid = str(UUID(int=UUID(page.id).int + 1)).upper()
    except (ValueError, AttributeError) as exc:
        raise NativeError(
            "native_data_unavailable",
            "Invalid native page UUID mapping.",
            operation="map_native_page",
        ) from exc
    records = (
        read_page_records(sandbox, root_uuid) if record_reader is None else record_reader(root_uuid)
    )
    if not records:
        raise NativeError(
            "native_data_unavailable",
            "No matching native page records are available "
            "in this copy. This does not establish that the page has no ink.",
            operation="read_native_page",
        )
    objects, metadata = decode_native_objects(records, root_uuid)
    objects, image_data, image_failures = _resolve_native_images(
        cast(AttachmentSandbox, sandbox), page, objects
    )
    _record_image_failures(metadata, image_failures)
    image_integrity = Counter(
        image_data[native_object.payload.attachment_id].integrity
        for native_object in objects
        if isinstance(native_object.payload, NativeImage)
    )
    if image_integrity:
        metadata["image_integrity"] = dict(image_integrity)
        if image_integrity["decoder-unverified"]:
            metadata["coverage"] = "partial"
            cast(list[str], metadata["warnings"]).append(
                "JPEG pixel integrity is unverified; the decoder can repair damaged scan data."
            )
    if not objects and metadata["unsupported_objects"]:
        raise NativeError(
            "native_data_unavailable",
            "The copied page has native records, but none could be rendered by the "
            "supported native renderer.",
            operation="decode_native_page",
            context={"native_annotations": metadata},
        )
    try:
        svg = _svg(
            objects,
            width_points,
            height_points,
            _COORDINATE_SCALE,
            image_data=image_data,
        )
    except (ValueError, TypeError) as exc:
        raise NativeError(
            "native_data_unavailable",
            "A decoded native object could not be rendered safely.",
            operation="render_native_page",
            context={"native_annotations": metadata},
        ) from exc
    return NativeOverlay(svg, metadata)
