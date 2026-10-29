"""Filled circular, elliptical, and quadratic native paths."""

from __future__ import annotations

import math

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native.objects import decode_native_objects
from goodnotes_cli.native.paths import _decode_native_path
from goodnotes_cli.native.records import NativePath, _DecodeBudget
from goodnotes_cli.rendering.ink_svg import _svg
from tests.support.native_oracle import (
    ROOT_ID,
    field,
    native_path_body,
    native_path_tpl,
    native_records,
)


def _decoded_path(payload: bytes, *, ellipse: bool = False) -> NativePath:
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    assert len(objects) == 1
    assert isinstance(objects[0].payload, NativePath)
    return objects[0].payload


def test_native_circular_path_preserves_cubic_controls_arc_and_closed_subpaths() -> None:
    path = _decoded_path(
        native_path_tpl(
            command_groups=[
                [(0, (12, 10)), (3, (10, 10, 2, 0, math.pi / 2)), (2, (9, 13, 8, 11, 12, 10))],
                [(0, (30, 40)), (2, (31, 41, 32, 42, 30, 40))],
            ]
        )
    )
    assert [command.op for command in path.commands] == [
        "M",
        "C",
        "C",
        "C",
        "C",
        "Z",
        "M",
        "C",
        "Z",
    ]
    assert path.commands[1].values[:2] == pytest.approx(
        (12, 10 - 8 / 3 * math.tan(math.pi / 8)), abs=1e-6
    )
    assert path.commands[3].values[-2:] == pytest.approx((10, 12), abs=1e-6)
    assert path.commands[4].values == (9, 13, 8, 11, 12, 10)
    assert path.fill_color == "#4080bf"
    assert path.fill_alpha == 1
    assert path.stroke_color is None


def test_native_elliptical_arc_uses_distinct_radii_and_its_observed_metadata() -> None:
    path = _decoded_path(
        native_path_tpl(
            ellipse=True,
            command_groups=[
                [(0, (24, 30)), (6, (20, 30, 4, 2, 0, math.pi / 2, 0))],
            ],
        ),
        ellipse=True,
    )
    assert path.commands[-2].values[-2:] == pytest.approx((20, 32), abs=1e-6)
    assert path.commands[-1].op == "Z"


@pytest.mark.parametrize("ellipse", [False, True])
def test_native_path_empty_metadata_forms_are_independent_of_sample_layout(ellipse: bool) -> None:
    payload = native_path_tpl(ellipse=ellipse, command_groups=[[(0, (20, 30))]])
    body = native_path_body(payload, metadata=b"" if ellipse else b"\x0a\x00")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert len(objects) == 1
    assert metadata["unsupported_objects"] == 0


@pytest.mark.parametrize("end_angle", [0.0, math.tau, -math.tau])
def test_native_full_revolution_is_rejected_instead_of_becoming_an_empty_svg_arc(
    end_angle: float,
) -> None:
    payload = native_path_tpl(
        command_groups=[
            [(0, (12, 10)), (3, (10, 10, 2, 0, end_angle))],
        ]
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize(
    "commands",
    [
        [(2, (1, 2, 3, 4, 5, 6))],
        [(0, (1, 2)), (0, (3, 4))],
        [(0, (1, 2)), (8, ())],
        [(0, (1, 2)), (2, (3, 4, 5, 6))],
        [(0, (1, 2)), (3, (1, 2, -1, 0, 1))],
        [(0, (math.nan, 2))],
        [(0, (100001, 2))],
    ],
)
def test_malformed_native_path_never_returns_a_partial_prefix(
    commands: list[tuple[int, tuple[float, ...]]],
) -> None:
    payload = native_path_tpl(command_groups=[commands])
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["rendered_objects"] == 0
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize("flags,directions", [([0, 0], [1]), ([0, 1], [0]), ([0, 1], [])])
def test_native_path_rejects_unknown_visibility_and_arc_direction(
    flags: list[int],
    directions: list[int],
) -> None:
    payload = native_path_tpl(
        command_groups=[[(0, (12, 10)), (3, (10, 10, 2, 0, 1))]],
        flags=flags,
        arc_directions=directions,
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_native_path_point_budget_counts_controls_before_returning_any_geometry() -> None:
    payload = native_path_tpl(
        command_groups=[
            [(0, (12, 10)), (3, (10, 10, 2, 0, 1)), (2, (1, 2, 3, 4, 5, 6))],
        ]
    )
    body = decode_message(native_path_body(payload))
    _decode_native_path(body, _DecodeBudget(points_left=16))
    with pytest.raises(NativeError, match="point budget"):
        _decode_native_path(body, _DecodeBudget(points_left=15))


@pytest.mark.parametrize("ellipse", [False, True])
@pytest.mark.parametrize("preceding", ["move", "cubic", "arc"])
def test_native_arc_uses_stored_controls_even_after_a_discontinuous_start(
    ellipse: bool,
    preceding: str,
) -> None:
    first = (0, (1000.0, 1000.0))
    commands: list[tuple[int, tuple[float, ...]]] = [first]
    if preceding == "cubic":
        commands.append((2, (900, 900, 950, 950, 1000, 1000)))
    elif preceding == "arc":
        commands = [
            (0, (1002, 1000)),
            (6, (1000, 1000, 2, 3, 0, math.pi / 2, 0))
            if ellipse
            else (3, (1000, 1000, 2, 0, math.pi / 2)),
        ]
    commands.append(
        (6, (10, 10, 2, 3, 0, math.pi / 2, 0)) if ellipse else (3, (10, 10, 2, 0, math.pi / 2))
    )
    payload = native_path_tpl(command_groups=[commands], ellipse=ellipse)
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert metadata["unsupported_reasons"] == {}
    path = objects[0].payload
    assert isinstance(path, NativePath)
    assert all(command.op != "A" for command in path.commands)
    assert path.commands[-2].values[-2:] == pytest.approx((10, 13 if ellipse else 12), abs=1e-5)
    assert max(abs(value) for command in path.commands[-4:-1] for value in command.values) < 20


@pytest.mark.parametrize("ellipse", [False, True])
def test_native_arc_does_not_infer_its_center_from_an_opposite_current_point(ellipse: bool) -> None:
    commands: list[tuple[int, tuple[float, ...]]] = [
        (0, (8.0, 10.0)),
        (6, (10, 10, 2, 3, 0, math.pi / 2, 0)) if ellipse else (3, (10, 10, 2, 0, math.pi / 2)),
    ]
    payload = native_path_tpl(command_groups=[commands], ellipse=ellipse)
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert metadata["unsupported_reasons"] == {}
    path = objects[0].payload
    assert isinstance(path, NativePath)
    assert path.commands[0].values == (8, 10)
    assert path.commands[-2].values[-2:] == pytest.approx((10, 13 if ellipse else 12), abs=1e-5)


def test_native_arc_preserves_valid_start_at_coordinate_limit() -> None:
    path = _decoded_path(
        native_path_tpl(
            command_groups=[
                [
                    (0, (100000.0, 10.0)),
                    (3, (99998, 10, 2, 0, math.pi / 2)),
                ]
            ]
        )
    )
    assert path.commands[0].values == (100000, 10)
    assert path.commands[-2].values[-2:] == pytest.approx((99998, 12), abs=1e-6)


def test_circular_cap_retains_small_stored_point_discrepancy() -> None:
    path = _decoded_path(
        native_path_tpl(command_groups=[[(0, (29.984, 20)), (3, (20, 20, 10, 0, math.pi / 2))]])
    )
    assert path.commands[-2].values[-2:] == pytest.approx((20, 30), abs=1e-6)


@pytest.mark.parametrize(
    "color,expected",
    [
        (field(1, 1.0) + field(4, 1.0), (255, 0, 0)),
        (field(2, 1.0) + field(4, 1.0), (0, 255, 0)),
        (field(3, 1.0) + field(4, 1.0), (0, 0, 255)),
        (field(4, 1.0), (0, 0, 0)),
    ],
)
def test_filled_paths_render_proto_colors_with_omitted_zero_channels(
    color: bytes, expected: tuple[int, int, int]
) -> None:
    payload = native_path_tpl(
        command_groups=[[(0, (10, 10)), (2, (30, 10, 30, 30, 10, 30)), (2, (0, 30, 0, 10, 10, 10))]]
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload, color=color)), ROOT_ID
    )
    assert metadata["unsupported_objects"] == 0
    with fitz.open(stream=_svg(objects, 40, 40, 1), filetype="svg") as svg:
        with fitz.open(stream=svg.convert_to_pdf()) as pdf:
            pixel = pdf[0].get_pixmap(alpha=False).pixel(10, 20)[:3]
    assert pixel == expected


def test_ellipse_arc_retains_small_observed_parametric_angle_discrepancy() -> None:
    # Float32 path points can lie on the ellipse while its angle marker differs
    # slightly. This stays within the supported ellipse envelope.
    path = _decoded_path(
        native_path_tpl(
            ellipse=True,
            command_groups=[
                [
                    (0, (10 + 2 * math.cos(0.1), 20 + 30 * math.sin(0.1))),
                    (6, (10, 20, 2, 30, 0.11, math.pi / 2, 0)),
                ]
            ],
        ),
        ellipse=True,
    )
    assert path.commands[-2].values[-2:] == pytest.approx((10, 50), abs=1e-5)
