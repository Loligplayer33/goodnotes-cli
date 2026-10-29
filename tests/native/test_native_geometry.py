"""Generated tests for shared native shape geometry and style decoding."""

from __future__ import annotations

import math

import pytest

from goodnotes_cli._vendor.wire import Message, decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native.geometry import _decode_native_fill, _decode_native_shape
from goodnotes_cli.native.records import _DecodeBudget
from tests.support.native_oracle import _ellipse_body, field


def point(x: float, y: float) -> bytes:
    return field(1, x) + field(2, y)


def shape(
    kind: int,
    content: bytes,
    *,
    offset: bytes = b"",
    extra: bytes = b"",
    style: bytes = b"\x10\x01",
) -> Message:
    auxiliary = field(kind, content) + field(5, style) + field(15, 2.0) + extra
    template = decode_message(_ellipse_body())
    body = b"".join(item.raw for item in template.fields if item.number not in {6, 9})
    return decode_message(body + field(9, auxiliary) + (field(6, offset) if offset else b""))


def geometry(kind: int, size: tuple[float, float], angle: float) -> bytes:
    return field(1, point(10.0, 20.0)) + field(2, point(*size)) + field(3, angle)


@pytest.mark.parametrize("offset", [(0.0, 0.0), (4.0, 5.0), (-8.0, -3.0)])
@pytest.mark.parametrize(
    "points",
    [[(0.0, 0.0), (2.0, 1.0), (1.0, 3.0)], [(0.0, 0.0), (0.0, 0.0), (2.0, 1.0), (0.0, 0.0)]],
)
def test_polyline_preserves_points_and_does_not_add_closure(
    offset: tuple[float, float], points: list[tuple[float, float]]
) -> None:
    stream = b"".join(field(1, point(x, y)) for x, y in points)
    path = _decode_native_shape(shape(1, stream, offset=point(*offset)), _DecodeBudget())
    assert [c.op for c in path.commands] == ["M", *(["L"] * (len(points) - 1))]
    assert [c.values for c in path.commands] == [(x + offset[0], y + offset[1]) for x, y in points]
    assert path.stroke_color == "#1a4d80" and path.stroke_alpha == 0.75 and path.stroke_width == 2.0


@pytest.mark.parametrize("offset", [(0.0, 0.0), (4.0, 5.0), (-8.0, -3.0)])
def test_quadratic_preserves_control_point(offset: tuple[float, float]) -> None:
    points = [(0.0, 1.0), (2.0, 4.0), (5.0, 6.0)]
    payload = b"".join(field(i, point(*p)) for i, p in enumerate(points, 1))
    commands = _decode_native_shape(
        shape(2, payload, offset=point(*offset)), _DecodeBudget()
    ).commands
    expected = [(x + offset[0], y + offset[1]) for x, y in points]
    assert [(c.op, c.values) for c in commands] == [
        ("M", expected[0]),
        ("Q", (*expected[1], *expected[2])),
    ]


@pytest.mark.parametrize("kind", [3, 4])
@pytest.mark.parametrize("angle", [0.0, math.pi / 2, -math.pi / 2, 0.37])
@pytest.mark.parametrize("size", [(4.0, 2.0), (1.5, 6.0)])
@pytest.mark.parametrize("offset", [(0.0, 0.0), (-3.0, 5.0)])
def test_rotated_geometry_and_translation(
    kind: int, angle: float, size: tuple[float, float], offset: tuple[float, float]
) -> None:
    path = _decode_native_shape(
        shape(kind, geometry(kind, size, angle), offset=point(*offset)), _DecodeBudget()
    )

    # Angles are stored as float32; leave room for quantization in analytic coordinates.
    def transform(x: float, y: float) -> tuple[float, float]:
        return (
            10 + offset[0] + x * math.cos(angle) - y * math.sin(angle),
            20 + offset[1] + x * math.sin(angle) + y * math.cos(angle),
        )

    if kind == 3:
        assert [c.op for c in path.commands] == ["M", "L", "L", "L", "Z"]
        x, y = size[0] / 2, size[1] / 2
        for command, (px, py) in zip(
            path.commands[:4], [(-x, -y), (x, -y), (x, y), (-x, y)], strict=True
        ):
            assert command.values == pytest.approx(transform(px, py), abs=1e-5)
    else:
        assert [c.op for c in path.commands] == ["M", "C", "C", "C", "C"]
        expected = [
            transform(size[0], 0),
            transform(0, size[1]),
            transform(-size[0], 0),
            transform(0, -size[1]),
            transform(size[0], 0),
        ]
        actual = [path.commands[0].values, *[c.values[-2:] for c in path.commands[1:]]]
        for actual_point, expected_point in zip(actual, expected, strict=True):
            assert actual_point == pytest.approx(expected_point, abs=1e-5)


@pytest.mark.parametrize(
    ("color", "expected_color", "expected_alpha"),
    [
        (b"", "#000000", 1.0),
        (field(1, 0.5) + field(4, 0.0), "#800000", 0.0),
        (field(2, 1.0) + field(4, 1.0), "#00ff00", 1.0),
    ],
)
def test_fill_sparse_rgba_and_translation(
    color: bytes, expected_color: str, expected_alpha: float
) -> None:
    body = decode_message(
        field(4, field(4, geometry(4, (3.0, 2.0), math.pi / 2)))
        + field(6, point(1.0, -2.0))
        + field(7, color)
        + field(18, 24)
    )
    path = _decode_native_fill(body, _DecodeBudget())
    assert path.commands[0].values == pytest.approx((11.0, 21.0), abs=1e-5)
    assert path.fill_color == expected_color and path.fill_alpha == expected_alpha
    assert path.stroke_color is None


@pytest.mark.parametrize(
    "auxiliary,offset",
    [
        (field(1, field(1, point(1.0, 2.0)) + field(1, point(3.0, 4.0))) + field(2, b""), b""),
        (field(1, field(1, point(1.0, 2.0)) + field(1, point(3.0, 4.0))) * 2, b""),
        (field(1, field(1, point(1.0, 2.0)) + field(1, 7)), b""),
        (field(1, field(1, point(1.0, 2.0)) + field(1, point(3.0, 4.0))), field(1, 1.0)),
        (field(1, field(1, point(99999.0, 0.0)) + field(1, point(1.0, 2.0))), point(2.0, 0.0)),
        (field(3, field(1, point(math.nan, 0.0)) + field(2, point(2.0, 3.0)) + field(3, 0.0)), b""),
    ],
)
def test_malformed_or_ambiguous_geometry_rejected(auxiliary: bytes, offset: bytes) -> None:
    template = decode_message(_ellipse_body())
    base = b"".join(item.raw for item in template.fields if item.number not in {6, 9})
    body = decode_message(
        base
        + field(9, auxiliary + field(5, b"\x10\x01") + field(15, 2.0))
        + (field(6, offset) if offset else b"")
    )
    with pytest.raises(ValueError):
        _decode_native_shape(body, _DecodeBudget())


def test_unknown_visibility_and_point_budget_fail_atomically() -> None:
    stream = b"".join(field(1, point(float(i), 0.0)) for i in range(3))
    with pytest.raises(NativeError):
        _decode_native_shape(shape(1, stream), _DecodeBudget(points_left=2))
    with pytest.raises(ValueError):
        _decode_native_shape(shape(1, stream, style=b"\x10\x00"), _DecodeBudget())


@pytest.mark.parametrize(
    ("kind", "content", "points_required"),
    [
        (1, field(1, point(1.0, 1.0)) + field(1, point(2.0, 2.0)), 2),
        (2, field(1, point(1.0, 1.0)) + field(2, point(2.0, 2.0)) + field(3, point(3.0, 1.0)), 3),
        (3, geometry(3, (2.0, 3.0), 0.0), 4),
        (4, geometry(4, (2.0, 3.0), 0.0), 13),
    ],
)
def test_geometry_budget_exact_boundary(kind: int, content: bytes, points_required: int) -> None:
    _decode_native_shape(shape(kind, content), _DecodeBudget(points_left=points_required))
    with pytest.raises(NativeError):
        _decode_native_shape(shape(kind, content), _DecodeBudget(points_left=points_required - 1))


@pytest.mark.parametrize(
    "bad_color", [field(1, -0.5), field(4, 1.1), field(1, 1), field(5, 1.0), field(1, 1.0) * 2]
)
def test_fill_rejects_invalid_rgba(bad_color: bytes) -> None:
    body = decode_message(
        field(4, field(4, geometry(4, (3.0, 2.0), 0.0))) + field(7, bad_color) + field(18, 24)
    )
    with pytest.raises(ValueError):
        _decode_native_fill(body, _DecodeBudget())
