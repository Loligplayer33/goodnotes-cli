from __future__ import annotations

import math
import sqlite3
import struct
import zlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.wire import Message, decode_message
from goodnotes_cli.native.assets import NativeImageData, load_native_image
from goodnotes_cli.native.geometry import _decode_native_image
from goodnotes_cli.native.records import NativeImage
from goodnotes_cli.rendering.svg import native_image_element
from tests.support.native_oracle import field

_LOGICAL_ID = "00000000-0000-4000-8000-000000000001"
_RESOURCE_ID = "00000000-0000-4000-8000-000000000002"
_ATTACHMENT_ID = "00000000-0000-4000-8000-000000000003"


def _point(x: float, y: float) -> bytes:
    return field(1, float(x)) + field(2, float(y))


def _rectangle(x: float, y: float, width: float, height: float) -> bytes:
    return field(1, _point(x, y)) + field(2, _point(width, height))


def _messages(
    *,
    center: tuple[float, float] = (40.0, 50.0),
    size: tuple[float, float] = (20.0, 10.0),
    rotation: float | None = 0.0,
    bounds_offset: tuple[float, float] = (0.0, 0.0),
    geometry_extra: bytes = b"",
    geometry_override: bytes | None = None,
    bounds_override: bytes | None = None,
    binding: str = "attachment",
) -> tuple[Message, Message]:
    angle = rotation or 0.0
    half_width = (abs(size[0] * math.cos(angle)) + abs(size[1] * math.sin(angle))) / 2
    half_height = (abs(size[0] * math.sin(angle)) + abs(size[1] * math.cos(angle))) / 2
    bounds = _rectangle(
        center[0] - half_width + bounds_offset[0],
        center[1] - half_height + bounds_offset[1],
        half_width * 2,
        half_height * 2,
    )
    geometry = (
        _rectangle(*center, *size)
        + (field(3, float(rotation)) if rotation is not None else b"")
        + geometry_extra
    )
    body = (
        field(1, _LOGICAL_ID.encode())
        + field(2, bounds if bounds_override is None else bounds_override)
        + field(3, geometry if geometry_override is None else geometry_override)
        + field(4, _RESOURCE_ID.encode())
        + field(5, b"generated")
        + field(15, b"generated")
        + field(18, 24)
    )
    descriptor = b""
    if binding in {"attachment", "resource", "mismatch"}:
        descriptor += field(4, (_ATTACHMENT_ID if binding == "mismatch" else _RESOURCE_ID).encode())
    if binding in {"attachment", "incomplete"}:
        descriptor += field(7, _ATTACHMENT_ID.encode())
    return decode_message(descriptor), decode_message(field(1, body))


def _decode(**options: Any) -> NativeImage:
    descriptor, item = _messages(**options)
    return _decode_native_image(descriptor, item, _LOGICAL_ID)


@pytest.mark.parametrize(
    "rotation", [None, 0.0, 0.37, -0.83, math.pi / 2, math.pi, 8 * math.tau + 0.4]
)
@pytest.mark.parametrize("center", [(40.0, 50.0), (-80.25, -60.5), (0.0, 0.0)])
def test_image_local_geometry_matches_rotated_axis_aligned_bounds(
    rotation: float | None, center: tuple[float, float]
) -> None:
    image = _decode(center=center, rotation=rotation)
    assert (image.x, image.y, image.width, image.height) == (
        *[center[0] - 10, center[1] - 5],
        20,
        10,
    )
    assert image.rotation == pytest.approx(rotation or 0.0)


def test_float32_rounding_is_allowed_without_hiding_mismatched_bounds() -> None:
    options = {"center": (72000.13, -61000.27), "size": (13000.3, 9700.7), "rotation": 0.713}
    image = _decode(**options)
    assert image.width == pytest.approx(13000.3, abs=0.001)
    with pytest.raises(ValueError, match="bounds disagree"):
        _decode(**options, bounds_offset=(0.2, 0.0))


@pytest.mark.parametrize(
    "geometry",
    [
        _rectangle(40, 50, 20, 10) + field(3, 1),
        _rectangle(40, 50, 20, 10) + field(3, b"rotation"),
        _rectangle(40, 50, 20, 10) + field(3, math.nan),
        _rectangle(40, 50, 20, 10) + field(3, math.inf),
        _rectangle(40, 50, 20, 10) + field(3, 0.0) + field(3, 0.0),
        _rectangle(40, 50, 20, 10) + field(4, 1),
        _rectangle(40, 50, 0, 10),
        _rectangle(40, 50, -20, 10),
        _rectangle(40, 50, math.inf, 10),
        field(1, _point(40, 50)) + field(2, field(1, 20) + field(2, 10)),
        _rectangle(40, 50, 20, 10) + field(1, _point(40, 50)),
    ],
)
def test_invalid_local_geometry_is_rejected(geometry: bytes) -> None:
    with pytest.raises(ValueError):
        _decode(geometry_override=geometry)


@pytest.mark.parametrize(
    "bounds",
    [
        _rectangle(30, 45, 20, 10) + field(3, 0.0),
        _rectangle(30, 45, 20, 10) + field(1, _point(30, 45)),
        _rectangle(30, 45, 0, 10),
        _rectangle(30, 45, 20.02, 10),
        _rectangle(99999, 45, 20, 10),
        _rectangle(30, math.nan, 20, 10),
    ],
)
def test_invalid_axis_aligned_bounds_are_rejected(bounds: bytes) -> None:
    with pytest.raises(ValueError):
        _decode(bounds_override=bounds)


@pytest.mark.parametrize("binding", ["attachment", "resource", "direct"])
def test_rotated_image_preserves_attachment_binding(binding: str) -> None:
    image = _decode(rotation=0.4, binding=binding)
    assert image.attachment_id == (_ATTACHMENT_ID if binding == "attachment" else _RESOURCE_ID)


@pytest.mark.parametrize("binding", ["mismatch", "incomplete"])
def test_rotated_image_rejects_unresolved_attachment_binding(binding: str) -> None:
    with pytest.raises(ValueError):
        _decode(rotation=0.4, binding=binding)


def test_image_decoder_rejects_local_corners_outside_coordinate_limit() -> None:
    with pytest.raises(ValueError, match="image coordinate"):
        _decode(center=(80000.0, 0.0), size=(50000.0, 1000.0), rotation=math.pi / 2)


def _alpha_png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\xff\0\0\xff\0\0\xff\0"))
        + chunk(b"IEND", b"")
    )


@dataclass(frozen=True)
class _Sandbox:
    root: Path
    library_dir: Path
    attachments_dir: Path
    projection_path: Path


@pytest.mark.parametrize("scale", [0.5, 1.0, 2.0])
@pytest.mark.parametrize("rotation", [math.pi / 2, -math.pi / 2, 0.4])
def test_loaded_alpha_image_rotates_around_center_at_each_scale(
    tmp_path: Path, rotation: float, scale: float
) -> None:
    library = tmp_path / "Library"
    attachments = library / "Attachments"
    attachments.mkdir(parents=True)
    (attachments / "generated.png").write_bytes(_alpha_png())
    projection = library / "projection.sqlite"
    with sqlite3.connect(projection) as connection:
        connection.execute(
            "CREATE TABLE attachments(id TEXT, path TEXT, available INTEGER, document_id TEXT)"
        )
        connection.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?)",
            (_ATTACHMENT_ID, "generated.png", 1, "generated-document"),
        )
    raster = load_native_image(
        _Sandbox(tmp_path, library, attachments, projection), "generated-document", _ATTACHMENT_ID
    )
    image = _decode(center=(40.0, 40.0), size=(40.0, 20.0), rotation=rotation)
    element = native_image_element(image, raster, scale)
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{80 * scale}" '
        f'height="{80 * scale}">{element}</svg>'
    )
    with fitz.open(stream=svg.encode(), filetype="svg") as document:
        with fitz.open(stream=document.convert_to_pdf()) as pdf:
            pixmap = pdf[0].get_pixmap(alpha=False)
    # The opaque left half rotates with the image. The transparent right half
    # keeps the white page visible, including when the geometry is scaled.
    for offset, expected in [(-10, (255, 0, 0)), (10, (255, 255, 255))]:
        x = round((40 + offset * math.cos(rotation)) * scale)
        y = round((40 + offset * math.sin(rotation)) * scale)
        assert pixmap.pixel(x, y)[:3] == expected
    assert raster.integrity == "scanlines-checked"


@pytest.mark.parametrize("scale", [0.0, -1.0, math.nan, math.inf, 100000.0])
def test_image_renderer_rejects_invalid_or_unbounded_scale(scale: float) -> None:
    raster = NativeImageData(_alpha_png(), "image/png", 2, 1)
    with pytest.raises(ValueError):
        native_image_element(NativeImage(_ATTACHMENT_ID, 10, 10, 20, 10, 0.4), raster, scale)


@pytest.mark.parametrize(
    "image",
    [
        NativeImage(_ATTACHMENT_ID, 10, 10, 20, 10, math.nan),
        NativeImage(_ATTACHMENT_ID, 10, 10, 20, 10, math.inf),
        NativeImage(_ATTACHMENT_ID, 99990, 10, 20, 10),
        NativeImage(_ATTACHMENT_ID, 99950, 10, 50, 90, math.pi / 4),
        NativeImage(_ATTACHMENT_ID, -100001, 10, 20, 10),
    ],
)
def test_image_renderer_rejects_invalid_transformed_coordinates(image: NativeImage) -> None:
    with pytest.raises(ValueError):
        native_image_element(image, NativeImageData(_alpha_png(), "image/png", 2, 1), 1)


def test_many_complete_turns_preserve_the_same_svg_rotation() -> None:
    raster = NativeImageData(_alpha_png(), "image/png", 2, 1)
    image = NativeImage(_ATTACHMENT_ID, 10, 10, 20, 10, 0.4)
    assert native_image_element(image, raster, 1) == native_image_element(
        replace(image, rotation=1000 * math.tau + 0.4), raster, 1
    )
