"""Fixed-width and variable-width ink with independent generated payloads."""

from __future__ import annotations

import math

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native import values as values_module
from goodnotes_cli.native.ink import _decode_variable_brush, decode_ink
from goodnotes_cli.native.objects import decode_native_objects, decode_native_records
from goodnotes_cli.native.records import Ink, _DecodeBudget
from goodnotes_cli.rendering import ink_svg as svg_module
from goodnotes_cli.rendering.ink_svg import _svg
from tests.support.native_oracle import (
    FORMAT,
    LOGICAL_ID,
    ROOT_ID,
    _brush_body,
    field,
    framed,
    native_records,
    tpl,
    variable_brush_tpl,
)


@pytest.mark.parametrize("compressed", [False, True])
def test_both_apple_block_encodings_and_dot(compressed: bool) -> None:
    payload = framed(tpl(points=[(25.0, 35.0)]), compressed=compressed)
    body = decode_message(field(2, payload))
    ink = decode_ink(body)[0]
    assert ink.points == ((25.0, 35.0),)
    assert b"<circle" in svg_module._svg([ink], 72, 72, 72 / 132)


def test_nontrivial_visibility_pattern_is_explicitly_unsupported() -> None:
    body = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl(flags=[0, 1, 0])))
    inks, metadata = decode_native_records(native_records(body=body), ROOT_ID)
    assert not inks
    assert metadata["coverage"] == "partial"
    assert metadata["unsupported_reasons"] == {"visibility-uncertain": 1}


def test_observed_leading_zero_then_visible_segments_is_supported() -> None:
    body = decode_message(field(2, framed(tpl(flags=[0, 1, 1]))))
    inks = decode_ink(body)
    assert len(inks) == 1 and inks[0].points == ((10.0, 20.0), (40.0, 20.0), (40.0, 50.0))


@pytest.mark.parametrize("translation", [(0.0, 0.0), (7.0, -3.0)])
@pytest.mark.parametrize("dash", [(), (4.0, 2.0)])
def test_version_two_ink_renders_quadratic_controls_and_preserves_repeated_points(
    translation: tuple[float, float], dash: tuple[float, ...]
) -> None:
    payload = tpl(
        version=2,
        format_string=FORMAT + "vA(f)",
        starts=[(10, 50)],
        segments=[((50, 10), (90, 50)), ((90, 50), (90, 50))],
        dash=dash,
    )
    body = decode_message(
        field(2, framed(payload)) + field(6, field(1, translation[0]) + field(2, translation[1]))
    )
    ink = decode_ink(body)[0]
    assert ink.quadratic is True
    assert ink.points[-3:] == ((90 + translation[0], 50 + translation[1]),) * 3
    assert ink.dash == dash
    # The quadratic midpoint is (50, 30), not the control at (50, 10).
    with fitz.open(stream=_svg([ink], 110, 70, 1), filetype="svg") as svg:
        with fitz.open(stream=svg.convert_to_pdf()) as pdf:
            pixmap = pdf[0].get_pixmap(alpha=False)
    x, y = int(50 + translation[0]), int(30 + translation[1])
    assert min(pixmap.pixel(x, y)[:3]) < 128
    assert pixmap.pixel(x, y - 20)[:3] == (255, 255, 255)


def test_version_two_quadratic_ink_requires_a_start_point() -> None:
    body = decode_message(
        field(2, framed(tpl(version=2, starts=[], segments=[((10, 20), (30, 40))])))
    )
    with pytest.raises(ValueError, match="commands do not match"):
        decode_ink(body)


def test_exact_start_to_segment_matches_do_not_guess_multi_start_paths() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (80.0, 90.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_multiple_start_dots_are_not_joined() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (80.0, 90.0)],
                    segments=[],
                )
            ),
        )
    )
    inks = decode_ink(body)
    svg = svg_module._svg(inks, 72, 72, 72 / 132)
    assert [ink.points for ink in inks] == [((10.0, 20.0),), ((80.0, 90.0),)]
    assert svg.count(b"<circle") == 2 and b"<polyline" not in svg


def test_unmatched_multiple_starts_with_segments_are_rejected() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(5.0, 6.0), (7.0, 8.0)],
                    segments=[((10.0, 20.0), (40.0, 20.0))],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_single_start_keeps_ordered_segment_samples_in_one_stroke() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((44.0, 20.0), (70.0, 20.0)),
                    ],
                )
            ),
        )
    )
    inks = decode_ink(body)
    assert len(inks) == 1
    assert inks[0].points == (
        (10.0, 20.0),
        (40.0, 20.0),
        (44.0, 20.0),
        (70.0, 20.0),
    )


def test_single_start_preserves_explicitly_ordered_retraced_segments() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((40.0, 20.0), (50.0, 20.0)),
                        ((50.0, 20.0), (40.0, 20.0)),
                        ((40.0, 20.0), (40.0, 30.0)),
                    ],
                )
            ),
        )
    )
    inks = decode_ink(body)
    assert len(inks) == 1
    assert inks[0].points == (
        (10.0, 20.0),
        (40.0, 20.0),
        (50.0, 20.0),
        (40.0, 20.0),
        (40.0, 30.0),
    )


def test_multiple_segments_without_start_markers_are_rejected_as_ambiguous() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="segments without start markers"):
        decode_ink(body)


def test_conflicting_start_markers_with_segments_are_rejected() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (10.0005, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_nonempty_native_dash_pattern_is_preserved() -> None:
    body = decode_message(field(2, framed(tpl(format_string=FORMAT + "vA(f)", dash=(4.5, 2.25)))))
    ink = decode_ink(body)[0]
    assert ink.dash == (4.5, 2.25)
    assert b'stroke-dasharray="2.454545 1.227273"' in svg_module._svg([ink], 72, 72, 72 / 132)


@pytest.mark.parametrize("dash", [(-1.0, 2.0), (math.nan, 2.0)])
def test_invalid_nonempty_native_dash_pattern_is_rejected(dash: tuple[float, ...]) -> None:
    body = decode_message(field(2, framed(tpl(format_string=FORMAT + "vA(f)", dash=dash))))
    with pytest.raises(ValueError, match="Unsupported native dash pattern"):
        decode_ink(body)


def test_ink_rejects_unsupported_format_before_generic_tpl_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        values_module, "decode_tpl", lambda *_: pytest.fail("unsupported generic parse")
    )
    body = decode_message(field(2, framed(tpl(format_string="A(A(u))"))))
    with pytest.raises(ValueError, match="Unsupported ink TPL"):
        decode_ink(body)


def test_long_native_segment_is_preserved_instead_of_split_into_dots() -> None:
    body = decode_message(field(2, framed(tpl(points=[(10.0, 20.0), (500.0, 20.0)]))))
    inks = decode_ink(body)
    assert len(inks) == 1 and inks[0].points == ((10.0, 20.0), (500.0, 20.0))


def test_variable_brush_preserves_curve_and_relative_width_dynamics() -> None:
    payload = variable_brush_tpl(samples=[(0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 4)], width=8)
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(payload),
        ),
        ROOT_ID,
    )
    assert metadata["unsupported_objects"] == 0
    ink = objects[0].payload
    assert isinstance(ink, Ink)
    assert ink.points == ((1, 1), (2, 3), (5, 6))
    assert ink.widths == (2, 4, 8)
    assert ink.quadratic is True
    assert ink.alpha == 0.5
    assert ink.color == "#336699"


@pytest.mark.parametrize("width", [0.1, 1.0, 6.0, 25.0, 100.0])
def test_variable_brush_retains_configured_width_and_zero_pressure(width: float) -> None:
    payload = variable_brush_tpl(
        width=width,
        start=(5, 30, 0, 1, 0),
        samples=[(0, 25, 10, 0, 1, 1, 45, 30, 0, 1, 2)],
    )
    objects, metadata = decode_native_objects(
        native_records(type_code=25, body=_brush_body(payload)), ROOT_ID
    )
    assert metadata["unsupported_objects"] == 0
    ink = objects[0].payload
    assert isinstance(ink, Ink)
    assert ink.widths == pytest.approx((0, width / 2, width))


def test_zero_pressure_brush_is_supported_without_adding_a_nominal_width_dot() -> None:
    payload = variable_brush_tpl(
        start=(10, 10, 0, 1, 0),
        samples=[(0, 10, 10, 0, 1, 0, 10, 10, 0, 1, 0)],
    )
    objects, metadata = decode_native_objects(
        native_records(type_code=25, body=_brush_body(payload)), ROOT_ID
    )
    assert metadata["unsupported_objects"] == 0
    with fitz.open(stream=_svg(objects, 20, 20, 1), filetype="svg") as svg:
        with fitz.open(stream=svg.convert_to_pdf()) as pdf:
            pixels = pdf[0].get_pixmap(alpha=False).samples
    assert set(pixels) == {255}


@pytest.mark.parametrize(
    "sample",
    [
        (0, math.inf, 3, 0, 1, 2, 5, 6, 0, 1, 4),
        (0, 2, 3, 0, 1, -1, 5, 6, 0, 1, 4),
        (0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 101),
        (0, 2, 3, math.nan, 1, 2, 5, 6, 0, 1, 4),
    ],
)
def test_variable_brush_rejects_invalid_sample_without_returning_a_prefix(
    sample: tuple[float, ...],
) -> None:
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(variable_brush_tpl(samples=[sample])),
        ),
        ROOT_ID,
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_variable_brush_commands_and_aggregate_point_budget_are_enforced() -> None:
    samples: list[tuple[float, ...]] = [(0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 4)]
    hidden = variable_brush_tpl(samples=samples, flags=[0, 0])
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(hidden),
        ),
        ROOT_ID,
    )
    assert objects == []
    assert metadata["unsupported_reasons"] == {"visibility-uncertain": 1}
    body = decode_message(_brush_body(variable_brush_tpl(samples=samples)))
    _decode_variable_brush(body, _DecodeBudget(points_left=3))
    with pytest.raises(NativeError, match="point budget"):
        _decode_variable_brush(body, _DecodeBudget(points_left=2))


def test_ink_translation_cannot_move_valid_points_beyond_decoder_bounds() -> None:
    body = decode_message(field(2, framed(tpl(points=[(99999.0, 10.0)]))) + field(6, field(1, 2.0)))
    with pytest.raises(ValueError, match="translated ink coordinate"):
        decode_ink(body)
