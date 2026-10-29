"""Observed ink and variable-brush command streams, including edited subpaths."""

from __future__ import annotations

import math
from enum import IntEnum
from typing import TypeVar, cast

from .._vendor.wire import Message, decode_message
from .geometry import _shape_geometry
from .records import Ink, NativePath, _DecodeBudget, _NativeDecodeIssue
from .values import (
    MAX_PEN_WIDTH,
    MIN_PEN_WIDTH,
    DrawingField,
    NativeObjectType,
    _decode_tpl_payload,
    _fixed,
    _float,
    _native_float,
    _native_translation,
    _one,
    _point,
    _rgb_alpha,
    _tpl_float,
)

_INK_FORMATS = {"vuA(v)A(S(uu))A(S(uuuu))", "vuA(v)A(S(uu))A(S(uuuu))vA(f)"}


_VARIABLE_BRUSH_FORMAT = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"


class InkCommand(IntEnum):
    MOVE = 0
    QUADRATIC = 1


_LEGACY_TPL_VERSION = 0
_QUADRATIC_TPL_VERSION = 2
_MAX_DASH_LENGTH = 10_000
_LEGACY_POINT_SEPARATION = 1e-3  # Native units; legacy duplicate-sample suppression only.


_Sample = TypeVar("_Sample")


def _command_groups(
    commands: list[int],
    starts: list[_Sample],
    segments: list[tuple[_Sample, _Sample]],
    *,
    reason: str = "unsupported-native-path",
) -> list[list[_Sample]]:
    """Consume the version-2 move/quadratic stream over separate sample arrays."""
    if (
        len(commands) != len(starts) + len(segments)
        or commands.count(InkCommand.MOVE) != len(starts)
        or commands.count(InkCommand.QUADRATIC) != len(segments)
        or (commands and commands[0] != InkCommand.MOVE)
    ):
        raise _NativeDecodeIssue(reason, "Native ink commands do not match their coordinate arrays")
    paths: list[list[_Sample]] = []
    start_index = segment_index = 0
    for command in commands:
        if command == InkCommand.MOVE:
            paths.append([starts[start_index]])
            start_index += 1
        else:
            paths[-1].extend(segments[segment_index])
            segment_index += 1
    return paths


def _segment_paths(
    starts: list[tuple[float, float]],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
    *,
    quadratic: bool = False,
    commands: list[int] | None = None,
) -> list[list[tuple[float, float]]]:
    """Decode only start/segment relationships established by observed records."""
    if commands is not None:
        return _command_groups(commands, starts, segments)
    if not segments:
        return [[point] for point in starts]

    # One start plus the ordered four-u sample stream is the observed stroke
    # representation. Its tuples are not endpoint-connected graph edges.
    # Multiple starts may denote additional subpaths, but their relationship
    # to this stream is not established; do not infer boundaries by proximity.
    if len(starts) > 1:
        raise ValueError("Ambiguous native ink starts and segments relationship")
    if quadratic:
        if not starts:
            raise ValueError("Quadratic native ink has no start point")
        # Version 2 stores one quadratic control and endpoint per tuple.
        # Repeated controls and endpoints are meaningful and must survive.
        return [[starts[0], *(point for segment in segments for point in segment)]]

    def ordered_path(
        initial: tuple[float, float] | None,
        segment_slice: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []

        def append(point: tuple[float, float]) -> None:
            if not points or math.dist(points[-1], point) >= _LEGACY_POINT_SEPARATION:
                points.append(point)

        if initial is not None:
            append(initial)
        for segment_start, segment_end in segment_slice:
            append(segment_start)
            append(segment_end)
        return points

    if len(starts) == 1:
        return [ordered_path(starts[0], segments)]

    # With no explicit path starts, only a single stored segment has an
    # unambiguous interpretation. Several segments could be one sampled
    # path or multiple paths; do not guess at their relationship.
    if len(segments) > 1:
        raise ValueError("Ambiguous native ink segments without start markers")
    return [ordered_path(None, segments)]


def decode_ink(body: Message, budget: _DecodeBudget | None = None) -> list[Ink]:
    """Decode the observed legacy pen schemas, preserving colour and lasso moves."""
    if any(item.number not in {1, 2, 3, 4, 5, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported ink body field")
    # These envelope fields were present alongside the supported ink schema.
    # Clocks in7/15 remain opaque. New style/visibility fields must not silently
    # acquire guessed semantics.
    for number in (7, 15):
        _one(body, number, bytes, required=False)
    if _one(body, DrawingField.VARIANT, int, required=False) not in {None, 5}:
        raise ValueError("Unsupported ink body variant")
    if _one(body, DrawingField.STYLE, int, required=False) not in {None, 1}:
        raise ValueError("Unsupported ink style flag")
    if _one(body, DrawingField.GEOMETRY, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary geometry")
    if _one(body, DrawingField.METADATA, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary body field")
    if _one(body, DrawingField.OBJECT_TYPE, int, required=False) not in {
        None,
        NativeObjectType.STANDARD,
    }:
        raise ValueError("Unsupported ink body type")
    budget = budget or _DecodeBudget()
    image = _decode_tpl_payload(
        body, budget, expected_format=_INK_FORMATS, schema_error="Unsupported ink TPL schema"
    )
    values = image.values
    if values[0] not in {_LEGACY_TPL_VERSION, _QUADRATIC_TPL_VERSION} or (
        len(values) == 7 and (values[0], values[5]) not in {(0, 0), (2, 1)}
    ):
        raise _NativeDecodeIssue("unsupported-annotation-schema", "Unsupported ink TPL version")
    width = _float(cast(int, values[1]))
    if not math.isfinite(width) or not MIN_PEN_WIDTH <= width <= MAX_PEN_WIDTH:
        raise ValueError("Invalid native pen width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, int]], values[3])
    segments = cast(list[tuple[int, int, int, int]], values[4])
    budget.charge(point_count=len(starts) + len(segments) * 2)
    if any(value not in {0, 1} for value in flags):
        raise _NativeDecodeIssue("visibility-uncertain", "Unsupported native visibility flags")
    command_stream = values[0] == _QUADRATIC_TPL_VERSION and bool(flags)
    if not command_stream and flags and len(flags) != len(segments) + 1:
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Native visibility flags do not match segments"
        )
    if not command_stream and any(value == 0 for value in flags[1:]):
        raise _NativeDecodeIssue("visibility-uncertain", "Uncertain native ink visibility pattern")
    color, alpha = _rgb_alpha(body)
    dx, dy = _native_translation(body, unsupported_message="Unsupported ink transform field")
    start_points = [_point(*start) for start in starts]
    segment_points = [
        (_point(segment[0], segment[1]), _point(segment[2], segment[3])) for segment in segments
    ]
    quadratic = values[0] == _QUADRATIC_TPL_VERSION and bool(segments)
    groups = _segment_paths(
        start_points,
        segment_points,
        quadratic=quadratic,
        commands=flags if command_stream else None,
    )
    dash: tuple[float, ...] = ()
    if len(values) == 7:
        candidate = cast(list[float], values[6])
        if candidate:
            if any(
                not math.isfinite(value) or value < 0 or value > _MAX_DASH_LENGTH
                for value in candidate
            ):
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
            quadratic=quadratic,
        )
        for group in groups
        if group
    ]
    if not result:
        raise ValueError("Ink has no decoded points")
    return result


def _decode_variable_brush(body: Message, budget: _DecodeBudget) -> list[Ink | NativePath]:
    """Decode the observed version-1 variable-width brush path."""
    if any(item.number not in {1, 2, 3, 4, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported variable brush body field")
    if (
        _one(body, DrawingField.VARIANT, int) != 5
        or _one(body, DrawingField.OBJECT_TYPE, int) != NativeObjectType.VARIABLE_BRUSH
    ):
        raise ValueError("Unsupported variable brush envelope version")
    for number in (20,):
        if _one(body, number, bytes, required=False) not in {None, b""}:
            raise ValueError("Unsupported variable brush auxiliary geometry")
    for number in (7, 15):
        _one(body, number, bytes, required=False)
    translation = _native_translation(body)

    image = _decode_tpl_payload(
        body,
        budget,
        expected_format=_VARIABLE_BRUSH_FORMAT,
        schema_error="Unsupported variable brush TPL schema",
    )
    if len(image.values) != 10:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported variable brush TPL schema"
        )
    values = image.values
    if values[0] != 1:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported variable brush TPL version"
        )
    width = _tpl_float(cast(int, values[1]), label="brush width")
    if not MIN_PEN_WIDTH <= width <= MAX_PEN_WIDTH:
        raise ValueError("Invalid native brush width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, ...]], values[3])
    samples = cast(list[tuple[int, ...]], values[4])
    if not starts or any(len(start) != 5 for start in starts):
        raise ValueError("Invalid variable brush start or sample stream")
    if any(values[index] for index in range(5, 10)):
        raise ValueError("Unsupported variable brush auxiliary arrays")
    if any(len(sample) != 11 for sample in samples):
        raise ValueError("Invalid variable brush sample width")
    budget.charge(point_count=len(starts) + len(samples) * 2)
    # The first sample word is opaque metadata, not a float coordinate.
    for row in [*starts, *(sample[1:] for sample in samples)]:
        for raw in row:
            _tpl_float(raw, label="brush sample")

    color, alpha = _rgb_alpha(body)
    auxiliary_payload = _one(body, DrawingField.GEOMETRY, bytes, required=False)
    if auxiliary_payload not in {None, b""}:
        auxiliary = decode_message(cast(bytes, auxiliary_payload))
        numbers = {field.number for field in auxiliary.fields}
        if len(auxiliary.fields) != 2 or len(numbers & {1, 2, 3, 4}) != 1 or 15 not in numbers:
            raise ValueError("Unsupported variable brush auxiliary geometry")
        if not math.isclose(_fixed(auxiliary, 15, 0.0), width, rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("Native shape width fields disagree")
        _command_groups(
            flags, starts, [(s[:5], s[6:]) for s in samples], reason="visibility-uncertain"
        )
        return [
            NativePath(
                _shape_geometry(auxiliary, budget, translation),
                fill_color=None,
                fill_alpha=0.0,
                stroke_color=color,
                stroke_width=width,
                stroke_alpha=alpha,
            )
        ]

    def sample_point(x: int, y: int, diameter: int) -> tuple[float, float, float]:
        point = _point(x, y)
        value = _tpl_float(diameter, label="brush sample width")
        if not 0 <= value <= MAX_PEN_WIDTH:
            raise ValueError("Invalid native brush sample width")
        return (
            _native_float(point[0] + translation[0], label="brush coordinate"),
            _native_float(point[1] + translation[1], label="brush coordinate"),
            value,
        )

    initial = [sample_point(x, y, diameter) for x, y, _, _, diameter in starts]
    segments = []
    for _opaque, cx, cy, _, _, control_width, ex, ey, _, _, end_width in samples:
        # Each sample stores an opaque tag, then control and endpoint records.
        segments.append((sample_point(cx, cy, control_width), sample_point(ex, ey, end_width)))
    groups = _command_groups(flags, initial, segments, reason="visibility-uncertain")

    # The sample dynamics alone are not a diameter. Retain the configured
    # width and relative variation without guessing the app's pressure curve.
    peak = max(point[2] for group in groups for point in group)
    return [
        Ink(
            tuple((x, y) for x, y, _ in group),
            width,
            color,
            alpha,
            widths=tuple(width * (value / peak) if peak else 0.0 for _, _, value in group),
            quadratic=True,
        )
        for group in groups
    ]
