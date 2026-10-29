from __future__ import annotations

import math
from typing import cast

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native.ink import _decode_variable_brush, decode_ink
from goodnotes_cli.native.objects import decode_native_objects
from goodnotes_cli.native.paths import _decode_native_path
from goodnotes_cli.native.records import DecodedNativeObject, Ink, NativePath, _DecodeBudget
from goodnotes_cli.rendering.ink_svg import _svg
from tests.support.native_oracle import (
    FORMAT,
    LOGICAL_ID,
    ROOT_ID,
    _brush_body,
    field,
    framed,
    native_path_body,
    native_path_tpl,
    native_records,
    tpl,
    variable_brush_tpl,
)

_Point = tuple[float, float]
_Section = tuple[_Point, ...]


def _ink_body(
    sections: tuple[_Section, ...],
    *,
    translation: _Point = (0.0, 0.0),
    dash: tuple[float, ...] = (),
    flags: list[int] | None = None,
) -> bytes:
    commands = [code for section in sections for code in [0, *([1] * (len(section) // 2))]]
    payload = tpl(
        version=2,
        width=4,
        format_string=FORMAT + "vA(f)",
        starts=[section[0] for section in sections],
        segments=[
            (section[index], section[index + 1])
            for section in sections
            for index in range(1, len(section), 2)
        ],
        flags=commands if flags is None else flags,
        dash=dash,
    )
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(payload))
        + field(6, field(1, float(translation[0])) + field(2, float(translation[1])))
    )


def _raster(objects: list[Ink] | list[DecodedNativeObject], scale: float = 1) -> fitz.Pixmap:
    svg = _svg(objects, 140 * scale, 100 * scale, scale)
    with fitz.open(stream=svg, filetype="svg") as vector:
        with fitz.open(stream=vector.convert_to_pdf()) as document:
            return document[0].get_pixmap(alpha=False)


@pytest.mark.parametrize(
    "sections",
    [
        (((10, 20), (20, 0), (30, 20)), ((80, 20), (90, 0), (100, 20))),
        (((10, 20),), ((80, 20), (90, 0), (100, 20)), ((55, 60),)),
        (
            ((10, 20), (20, 0), (30, 20), (30, 20), (30, 20)),
            ((80, 20), (80, 20), (80, 20)),
            ((55, 60),),
        ),
    ],
)
@pytest.mark.parametrize("translation", [(0.0, 0.0), (7.0, -3.0)])
@pytest.mark.parametrize("dash", [(), (6.0, 2.0)])
def test_version_two_commands_preserve_each_section_and_its_style(
    sections: tuple[_Section, ...], translation: _Point, dash: tuple[float, ...]
) -> None:
    inks = decode_ink(decode_message(_ink_body(sections, translation=translation, dash=dash)))
    assert [ink.points for ink in inks] == [
        tuple((x + translation[0], y + translation[1]) for x, y in section) for section in sections
    ]
    assert all(ink.dash == dash and ink.width == 4 for ink in inks)
    assert all(ink.quadratic for ink in inks if len(ink.points) > 1)


@pytest.mark.parametrize("scale", [0.5, 1.0, 2.0])
def test_disjoint_quadratics_and_dot_leave_no_connecting_ink(scale: float) -> None:
    sections: tuple[_Section, ...] = (
        ((10, 20), (20, 20), (30, 20)),
        ((80, 20), (90, 20), (100, 20)),
        ((55, 60),),
    )
    inks = decode_ink(decode_message(_ink_body(sections)))
    pixmap = _raster(inks, scale)
    for x, y in [(20, 20), (90, 20), (55, 60)]:
        assert min(pixmap.pixel(round(x * scale), round(y * scale))[:3]) < 250
    for x, y in [(55, 20), (65, 40), (55, 40)]:
        assert pixmap.pixel(round(x * scale), round(y * scale))[:3] == (255, 255, 255)


@pytest.mark.parametrize("flags", [[1, 0, 1, 0], [0, 1, 2, 0], [0, 1, 0], [0, 0, 0, 1]])
@pytest.mark.parametrize("brush", [False, True])
def test_invalid_command_stream_discards_the_whole_object(flags: list[int], brush: bool) -> None:
    if brush:
        payload = variable_brush_tpl(
            starts=[(10, 20, 0, 1, 1), (80, 20, 0, 1, 1)],
            samples=[
                (0, 20, 20, 0, 1, 2, 30, 20, 0, 1, 4),
                (0, 90, 20, 0, 1, 2, 100, 20, 0, 1, 4),
            ],
            flags=flags,
        )
        body = _brush_body(payload)
    else:
        body = _ink_body(
            (((10, 20), (20, 20), (30, 20)), ((80, 20), (90, 20), (100, 20))), flags=flags
        )
    objects, metadata = decode_native_objects(
        native_records(type_code=25 if brush else 24, body=body), ROOT_ID
    )
    assert objects == []
    assert metadata["rendered_objects"] == 0
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize("brush", [False, True])
def test_multirun_ink_point_budget_includes_each_start_and_control(brush: bool) -> None:
    if brush:
        payload = variable_brush_tpl(
            starts=[(10, 20, 0, 1, 1), (80, 20, 0, 1, 1)],
            samples=[
                (0, 20, 20, 0, 1, 2, 30, 20, 0, 1, 4),
                (0, 90, 20, 0, 1, 2, 100, 20, 0, 1, 4),
            ],
            flags=[0, 1, 0, 1],
        )
        body = decode_message(_brush_body(payload))
        decode = _decode_variable_brush
    else:
        body = decode_message(
            _ink_body((((10, 20), (20, 20), (30, 20)), ((80, 20), (90, 20), (100, 20))))
        )
        decode = decode_ink
    assert len(decode(body, _DecodeBudget(points_left=6))) == 2
    with pytest.raises(NativeError, match="point budget"):
        decode(body, _DecodeBudget(points_left=5))


@pytest.mark.parametrize("translation", [(0.0, 0.0), (5.0, -7.0)])
@pytest.mark.parametrize("scale", [0.5, 1.0, 2.0])
def test_brush_runs_keep_pressure_translation_and_raster_gaps(
    translation: _Point, scale: float
) -> None:
    payload = variable_brush_tpl(
        width=8,
        starts=[(10, 20, 0, 1, 1), (80, 20, 0, 1, 4), (55, 60, 0, 1, 2)],
        samples=[
            (0, 20, 20, 0, 1, 2, 30, 20, 0, 1, 4),
            (0, 90, 20, 0, 1, 2, 100, 20, 0, 1, 1),
        ],
        flags=[0, 1, 0, 1, 0],
    )
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(
                payload,
                extra=field(6, field(1, float(translation[0])) + field(2, float(translation[1]))),
            ),
        ),
        ROOT_ID,
    )
    assert metadata["unsupported_objects"] == 0
    inks = [cast(Ink, obj.payload) for obj in objects]
    assert [ink.widths for ink in inks] == [(2, 4, 8), (8, 4, 2), (4,)]
    assert [ink.points[0] for ink in inks] == [
        (x + translation[0], y + translation[1]) for x, y in [(10, 20), (80, 20), (55, 60)]
    ]
    pixmap = _raster(objects, scale)
    for x, y in [(20, 20), (90, 20), (55, 60)]:
        assert (
            min(
                pixmap.pixel(
                    round((x + translation[0]) * scale), round((y + translation[1]) * scale)
                )[:3]
            )
            < 250
        )
    assert pixmap.pixel(round((55 + translation[0]) * scale), round((20 + translation[1]) * scale))[
        :3
    ] == (255, 255, 255)


@pytest.mark.parametrize("ellipse", [False, True])
@pytest.mark.parametrize("empty_positions", [False, True])
def test_filled_quadratics_preserve_controls_and_empty_subpath_groups(
    ellipse: bool, empty_positions: bool
) -> None:
    groups: list[list[tuple[int, tuple[float, ...]]]] = [
        [(0, (10, 40)), (1, (20, 0, 30, 40))],
        [(0, (80, 40)), (1, (90, 0, 100, 40))],
    ]
    if empty_positions:
        groups = [[], groups[0], [], groups[1], []]
    body = native_path_body(native_path_tpl(command_groups=groups, ellipse=ellipse))
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    path = objects[0].payload
    assert isinstance(path, NativePath)
    assert [command.op for command in path.commands] == ["M", "Q", "Z", "M", "Q", "Z"]
    assert path.commands[1].values == (20, 0, 30, 40)
    pixmap = _raster(objects)
    assert pixmap.pixel(20, 30)[:3] != (255, 255, 255)
    assert pixmap.pixel(90, 30)[:3] != (255, 255, 255)
    assert pixmap.pixel(20, 5)[:3] == (255, 255, 255)
    assert pixmap.pixel(55, 30)[:3] == (255, 255, 255)


@pytest.mark.parametrize(
    "coordinates", [(20, 0, 30), (20, 0, 30, 40, 50), (math.nan, 0, 30, 40), (100001, 0, 30, 40)]
)
def test_invalid_quadratic_arrays_never_return_a_valid_subpath_prefix(
    coordinates: tuple[float, ...],
) -> None:
    payload = native_path_tpl(
        command_groups=[
            [(0, (10, 40)), (1, (20, 0, 30, 40))],
            [(0, (80, 40)), (1, coordinates)],
        ]
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_quadratic_point_budget_counts_empty_groups_as_zero() -> None:
    payload = native_path_tpl(command_groups=[[], [(0, (10, 40)), (1, (20, 0, 30, 40))], []])
    body = decode_message(native_path_body(payload))
    path = _decode_native_path(body, _DecodeBudget(points_left=3))
    assert [command.op for command in path.commands] == ["M", "Q", "Z"]
    with pytest.raises(NativeError, match="point budget"):
        _decode_native_path(body, _DecodeBudget(points_left=2))


@pytest.mark.parametrize("rotation", [-1.1, 0.43, 2.8])
def test_rotated_elliptical_arc_matches_analytic_endpoint_and_midpoint(
    rotation: float,
) -> None:
    cosine, sine = math.cos(rotation), math.sin(rotation)

    def point(x: float, y: float) -> _Point:
        return (60 + x * cosine - y * sine, 60 + x * sine + y * cosine)

    payload = native_path_tpl(
        ellipse=True,
        command_groups=[
            [(0, point(30, 0)), (6, (60, 60, 30, 8, 0, -1.5, rotation))],
        ],
    )
    path = _decode_native_path(decode_message(native_path_body(payload)), _DecodeBudget())
    assert [command.op for command in path.commands] == ["M", "C", "Z"]
    start = path.commands[0].values
    control1_x, control1_y, control2_x, control2_y, end_x, end_y = path.commands[1].values
    assert (end_x, end_y) == pytest.approx(point(30 * math.cos(-1.5), 8 * math.sin(-1.5)), abs=3e-6)
    midpoint = (
        (start[0] + 3 * control1_x + 3 * control2_x + end_x) / 8,
        (start[1] + 3 * control1_y + 3 * control2_y + end_y) / 8,
    )
    assert midpoint == pytest.approx(point(30 * math.cos(-0.75), 8 * math.sin(-0.75)), abs=3e-6)
    # The terminal tangent is perpendicular to the analytic ellipse normal.
    tangent_x, tangent_y = end_x - control2_x, end_y - control2_y
    local_x = tangent_x * cosine + tangent_y * sine
    local_y = -tangent_x * sine + tangent_y * cosine
    assert local_x * math.cos(-1.5) / 30 + local_y * math.sin(-1.5) / 8 == pytest.approx(
        0, abs=3e-6
    )
    body = decode_message(native_path_body(payload))
    _decode_native_path(body, _DecodeBudget(points_left=4))
    with pytest.raises(NativeError, match="point budget"):
        _decode_native_path(body, _DecodeBudget(points_left=3))


@pytest.mark.parametrize("rotation", [0.0, 0.4, -0.7])
def test_variable_brush_auxiliary_ellipse_uses_shared_geometry_and_aggregate_budget(
    rotation: float,
) -> None:
    payload = variable_brush_tpl(
        width=8,
        starts=[(10, 20, 0, 1, 1), (80, 20, 0, 1, 4)],
        samples=[
            (0, 20, 20, 0, 1, 2, 30, 20, 0, 1, 4),
            (0, 90, 20, 0, 1, 2, 100, 20, 0, 1, 1),
        ],
        flags=[0, 1, 0, 1],
    )
    geometry = (
        field(1, field(1, 40.0) + field(2, 50.0))
        + field(2, field(1, 18.0) + field(2, 6.0))
        + field(3, rotation)
    )
    body = decode_message(
        _brush_body(
            payload,
            extra=field(9, field(4, geometry) + field(15, 8.0))
            + field(6, field(1, 5.0) + field(2, -7.0)),
        )
    )
    result = _decode_variable_brush(body, _DecodeBudget(points_left=19))
    assert len(result) == 1
    path = result[0]
    assert isinstance(path, NativePath)
    assert [command.op for command in path.commands] == ["M", "C", "C", "C", "C"]
    assert path.commands[0].values == pytest.approx(
        (45 + 18 * math.cos(rotation), 43 + 18 * math.sin(rotation)), abs=1e-6
    )
    assert (path.stroke_color, path.stroke_width, path.stroke_alpha) == ("#336699", 8, 0.5)
    assert path.fill_color is None
    with pytest.raises(NativeError, match="point budget"):
        _decode_variable_brush(body, _DecodeBudget(points_left=18))


@pytest.mark.parametrize("brush", [False, True])
@pytest.mark.parametrize("coordinate", [math.inf, math.nan, 100001.0])
def test_invalid_later_run_coordinate_rejects_all_runs(brush: bool, coordinate: float) -> None:
    if brush:
        payload = variable_brush_tpl(
            starts=[(10, 20, 0, 1, 1), (80, 20, 0, 1, 1)],
            samples=[
                (0, 20, 20, 0, 1, 2, 30, 20, 0, 1, 4),
                (0, coordinate, 20, 0, 1, 2, 100, 20, 0, 1, 4),
            ],
            flags=[0, 1, 0, 1],
        )
        body = _brush_body(payload)
    else:
        body = _ink_body(
            (
                ((10, 20), (20, 20), (30, 20)),
                ((80, 20), (coordinate, 20), (100, 20)),
            )
        )
    objects, metadata = decode_native_objects(
        native_records(type_code=25 if brush else 24, body=body), ROOT_ID
    )
    assert objects == []
    assert metadata["rendered_objects"] == 0
    assert metadata["unsupported_objects"] == 1
