from __future__ import annotations

import math
from typing import cast

from .._vendor.wire import Message
from ..native.records import Ink, _DecodeBudget, _NativeDecodeIssue
from ..native.values import (
    _decode_tpl_payload,
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
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported variable brush TPL schema"
        )
    values = image.values
    if values[0] != 1:
        raise _NativeDecodeIssue(
            "unsupported-annotation-schema", "Unsupported variable brush TPL version"
        )
    width = _tpl_float(cast(int, values[1]), label="brush width")
    if not 0.05 <= width <= 100:
        raise ValueError("Invalid native brush width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, ...]], values[3])
    samples = cast(list[tuple[int, ...]], values[4])
    if len(starts) != 1 or len(starts[0]) != 5 or not samples:
        raise ValueError("Invalid variable brush start or sample stream")
    if len(flags) != len(samples) + 1 or flags != [0, *([1] * len(samples))]:
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Uncertain variable brush visibility pattern"
        )
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
        raise _NativeDecodeIssue("unsupported-annotation-schema", "Unsupported ink TPL version")
    width = _float(cast(int, values[1]))
    if not math.isfinite(width) or not 0.05 <= width <= 100:
        raise ValueError("Invalid native pen width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, int]], values[3])
    segments = cast(list[tuple[int, int, int, int]], values[4])
    budget.charge(point_count=len(starts) + len(segments) * 2)
    if any(value not in {0, 1} for value in flags):
        raise _NativeDecodeIssue("visibility-uncertain", "Unsupported native visibility flags")
    if flags and len(flags) != len(segments) + 1:
        raise _NativeDecodeIssue(
            "visibility-uncertain", "Native visibility flags do not match segments"
        )
    if any(value == 0 for value in flags[1:]):
        raise _NativeDecodeIssue("visibility-uncertain", "Uncertain native ink visibility pattern")
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
