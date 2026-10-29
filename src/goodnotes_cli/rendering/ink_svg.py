"""Stroke sampling, width outlines and SVG assembly for native page objects."""

from __future__ import annotations

import math
from collections.abc import Mapping

from ..native.records import DecodedNativeObject, Ink, NativeImage, NativePath
from .svg import (
    PatternRasterBudget,
    RasterImage,
    native_image_element,
    native_path_element,
    native_pattern_element,
)

# Render policy is independent of compressed-record decoding budgets.
_MAX_STROKE_WIDTH = 100
_MAX_SAMPLED_POINTS = 100_000
_MAX_QUADRATIC_STEPS = 32
_QUADRATIC_CURVATURE_STEP = 0.4  # Native units, bounds quadratic flattening work.
_DIRECTION_EPSILON = 1e-9  # Scaled point distance below which tangents are degenerate.
_MITER_LIMIT = 4.0


def _unit_direction(
    start: tuple[float, float], end: tuple[float, float]
) -> tuple[float, float] | None:
    if any(not math.isfinite(value) for value in (*start, *end)):
        raise ValueError("Native stroke contains a nonfinite point")
    dx, dy = end[0] - start[0], end[1] - start[1]
    length = math.hypot(dx, dy)
    if not math.isfinite(length):
        raise ValueError("Native stroke segment exceeds the supported range")
    if length <= _DIRECTION_EPSILON:
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
            or width > _MAX_STROKE_WIDTH
        ):
            raise ValueError("Native point width is outside the supported range")
        scaled_point = (point[0] * scale, point[1] * scale)
        scaled_width = width * scale
        if any(not math.isfinite(value) for value in (*scaled_point, scaled_width)):
            raise ValueError("Native width profile exceeds the supported range")
        if samples and math.dist(samples[-1][0], scaled_point) <= _DIRECTION_EPSILON:
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
            if length <= _DIRECTION_EPSILON:
                normal = after_normal
                miter = 1.0
            else:
                normal = (combined[0] / length, combined[1] / length)
                projection = abs(normal[0] * after_normal[0] + normal[1] * after_normal[1])
                miter = min(_MITER_LIMIT, 1 / max(projection, 1 / _MITER_LIMIT))
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
    if end_radius > _DIRECTION_EPSILON:
        path.append(
            f"A {end_radius:.6f} {end_radius:.6f} 0 0 0 {right[-1][0]:.6f} {right[-1][1]:.6f}"
        )
    else:
        path.append(f"L {right[-1][0]:.6f} {right[-1][1]:.6f}")
    path.extend(f"L {x:.6f} {y:.6f}" for x, y in reversed(right[:-1]))
    start_radius = radii[0]
    if start_radius > _DIRECTION_EPSILON:
        path.append(
            f"A {start_radius:.6f} {start_radius:.6f} 0 0 0 {left[0][0]:.6f} {left[0][1]:.6f}"
        )
    else:
        path.append(f"L {left[0][0]:.6f} {left[0][1]:.6f}")
    path.append("Z")
    return " ".join(path)


def _quadratic_value(start: float, control: float, end: float, t: float) -> float:
    value = (1 - t) ** 2 * start + 2 * t * (1 - t) * control + t * t * end
    # A quadratic stays inside its controls' bounds. Roundoff at a validated
    # coordinate or width limit must not turn that valid sample into an error.
    return min(max(start, control, end), max(min(start, control, end), value))


def _quadratic_width_samples(
    ink: Ink,
) -> tuple[tuple[tuple[float, float], ...], tuple[float, ...]]:
    """Flatten quadratic positions and diameters within a bounded point count."""
    points, widths = ink.points, ink.widths
    if len(points) != len(widths) or len(points) % 2 != 1:
        raise ValueError("Native quadratic width profile does not match its point stream")
    for point, width in zip(points, widths, strict=True):
        if (
            any(not math.isfinite(value) or abs(value) > 100_000 for value in point)
            or not math.isfinite(width)
            or not 0 <= width <= _MAX_STROKE_WIDTH
        ):
            raise ValueError("Native quadratic width sample is outside the supported range")
    sampled_points: list[tuple[float, float]] = [points[0]]
    sampled_widths = [widths[0]]
    for index in range(1, len(points), 2):
        start, control, end = points[index - 1 : index + 2]
        start_width, control_width, end_width = widths[index - 1 : index + 2]
        curvature = max(
            math.hypot(start[0] - 2 * control[0] + end[0], start[1] - 2 * control[1] + end[1]),
            abs(start_width - 2 * control_width + end_width),
        )
        if not math.isfinite(curvature):
            raise ValueError("Native quadratic stroke contains a nonfinite point")
        steps = max(
            1,
            min(_MAX_QUADRATIC_STEPS, math.ceil(math.sqrt(curvature / _QUADRATIC_CURVATURE_STEP))),
        )
        if len(sampled_points) + steps > _MAX_SAMPLED_POINTS:
            raise ValueError("Native quadratic stroke exceeds its sampling budget")
        for step in range(1, steps + 1):
            t = step / steps
            x, y = (_quadratic_value(start[axis], control[axis], end[axis], t) for axis in (0, 1))
            sampled_points.append((x, y))
            sampled_widths.append(_quadratic_value(start_width, control_width, end_width, t))
    return tuple(sampled_points), tuple(sampled_widths)


def _svg(
    items: list[DecodedNativeObject] | list[Ink],
    width: float,
    height: float,
    scale: float,
    *,
    image_data: Mapping[str, RasterImage] | None = None,
) -> bytes:
    pattern_budget = PatternRasterBudget()
    if image_data is not None:
        for raster in {id(value): value for value in image_data.values()}.values():
            pattern_budget.charge(
                byte_count=len(raster.data), pixel_count=raster.width * raster.height
            )
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.6f}pt" '
        f'height="{height:.6f}pt" viewBox="0 0 {width:.6f} {height:.6f}">'
    ]
    for item in items:
        payload = item.payload if isinstance(item, DecodedNativeObject) else item
        if isinstance(payload, NativePath):
            if payload.pattern is None:
                elements.append(native_path_element(payload, scale))
            else:
                raster = (
                    image_data.get(payload.pattern.attachment_id)
                    if image_data is not None
                    else None
                )
                if raster is None:
                    raise ValueError(
                        "Native pattern bytes were not resolved from the copied snapshot"
                    )
                elements.append(
                    native_pattern_element(payload, raster, scale, budget=pattern_budget)
                )
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
            sampled_points, sampled_widths = (
                _quadratic_width_samples(ink) if ink.quadratic else (points, ink.widths)
            )
            outline = _variable_width_outline(sampled_points, sampled_widths, scale)
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
            if ink.quadratic:
                if len(points) % 2 != 1:
                    raise ValueError("Native quadratic stroke has an incomplete segment")
                x, y = points[0]
                commands = [f"M {x * scale:.6f} {y * scale:.6f}"]
                for index in range(1, len(points), 2):
                    control, end = points[index : index + 2]
                    commands.append(
                        f"Q {control[0] * scale:.6f} {control[1] * scale:.6f} "
                        f"{end[0] * scale:.6f} {end[1] * scale:.6f}"
                    )
                geometry = f'<path d="{" ".join(commands)}"'
            else:
                coordinates = " ".join(f"{x * scale:.6f},{y * scale:.6f}" for x, y in points)
                geometry = f'<polyline points="{coordinates}"'
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
                f'{geometry} fill="none" '
                f'stroke="{ink.color}" stroke-opacity="{ink.alpha:.6f}" '
                f'stroke-width="{ink.width * scale:.6f}" stroke-linecap="round" '
                f'stroke-linejoin="round"{dash}/>'
            )
    elements.append("</svg>")
    return "\n".join(elements).encode()
