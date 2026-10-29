from __future__ import annotations

import base64
import math
import re
from dataclasses import replace

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.native.paths import _decode_native_pattern_path
from goodnotes_cli.native.records import (
    DecodedNativeObject,
    NativePath,
    NativePathCommand,
    NativePattern,
    _DecodeBudget,
)
from goodnotes_cli.rendering.ink_svg import _svg
from goodnotes_cli.rendering.svg import PatternRasterBudget, native_pattern_element
from tests.support.native_oracle import (
    LOGICAL_ID,
    RESOURCE_ID,
    _pattern_body,
    _pattern_metadata,
    _texture,
    field,
)


def _path(*, alpha: float = 1.0) -> NativePath:
    return NativePath(
        (
            NativePathCommand("M", (2, 2)),
            NativePathCommand("L", (10, 2)),
            NativePathCommand("L", (10, 10)),
            NativePathCommand("L", (2, 10)),
            NativePathCommand("Z"),
        ),
        "#00ff00",
        alpha,
        pattern=NativePattern(RESOURCE_ID, 3, 2),
    )


def test_pattern_adapter_reuses_curve_geometry_and_preserves_anchor_and_opacity() -> None:
    path = _decode_native_pattern_path(
        decode_message(_pattern_body(_pattern_metadata())), _DecodeBudget()
    )
    assert path.pattern == NativePattern(RESOURCE_ID, 7, 8)
    assert [command.op for command in path.commands] == ["M", "Q", "Z"]
    assert path.fill_alpha == 0.5


@pytest.mark.parametrize(
    "metadata",
    [
        b"",
        _pattern_metadata() + field(3, b""),
        _pattern_metadata(resource=b"malformed"),
        _pattern_metadata(scale=2.0),
        _pattern_metadata(scale=math.nan),
        _pattern_metadata(width=0.0),
        _pattern_metadata(width=101.0),
        field(2, field(1, RESOURCE_ID.encode()) + field(3, b"")),
    ],
)
def test_pattern_adapter_rejects_unknown_or_malformed_metadata(metadata: bytes) -> None:
    with pytest.raises(ValueError):
        _decode_native_pattern_path(decode_message(_pattern_body(metadata)), _DecodeBudget())


def test_pattern_svg_repeats_original_colors_and_clips_to_path() -> None:
    path = _path()
    item = DecodedNativeObject(1, 1, LOGICAL_ID, path)
    svg = _svg([item], 12, 12, 1, image_data={RESOURCE_ID: _texture()})
    assert b'<image x="2.000000" y="2.000000" width="8.000000" height="8.000000"' in svg
    with fitz.open(stream=svg, filetype="svg") as vector:
        with fitz.open(stream=vector.convert_to_pdf()) as document:
            image = document[0].get_pixmap(matrix=fitz.Matrix(4, 4))
    assert image.pixel(13, 21)[:3] == (255, 0, 0)
    assert image.pixel(17, 21)[:3] == (0, 0, 255)
    assert image.pixel(5, 21)[:3] == (255, 255, 255)


def test_pattern_scale_preserves_alpha() -> None:
    element = native_pattern_element(_path(alpha=0.5), _texture(), 2)
    assert '<image x="4.000000" y="4.000000" width="16.000000" height="16.000000"' in element
    encoded = re.search(r"base64,([^\"]+)", element)
    assert encoded is not None
    raster = fitz.Pixmap(base64.b64decode(encoded.group(1)))
    assert raster.pixel(8, 8) == (128, 0, 0, 128)


def test_pattern_requires_resolved_raster_and_bounds_repetition_work() -> None:
    item = DecodedNativeObject(1, 1, LOGICAL_ID, _path())
    with pytest.raises(ValueError, match="not resolved"):
        _svg([item], 12, 12, 1)
    oversized = replace(
        _path(),
        commands=(NativePathCommand("M", (0, 0)), NativePathCommand("L", (10_000, 10_000))),
    )
    with pytest.raises(ValueError, match="pixel budget"):
        native_pattern_element(oversized, _texture(), 1)
    with pytest.raises(ValueError, match="bounds|dimensions"):
        native_pattern_element(_path(), replace(_texture(), width=0), 1)


def test_pattern_composition_obeys_shared_page_budget() -> None:
    budget = PatternRasterBudget(pixels_left=48 * 48)
    native_pattern_element(_path(), _texture(), 1, budget=budget)
    with pytest.raises(ValueError, match="page budget"):
        native_pattern_element(_path(), _texture(), 1, budget=budget)


def test_pattern_multiplies_texture_alpha_and_path_opacity_once() -> None:
    element = native_pattern_element(_path(alpha=0.5), _texture(alpha=128), 1)
    encoded = re.search(r'base64,([^"]+)', element)
    assert encoded is not None
    raster = fitz.Pixmap(base64.b64decode(encoded.group(1)))
    assert raster.pixel(8, 8) == (64, 0, 0, 64)


def test_pattern_mask_preserves_nonzero_holes_and_fractional_bounds() -> None:
    path = replace(
        _path(),
        pattern=NativePattern(RESOURCE_ID, 3.25, 2.25),
        commands=(
            NativePathCommand("M", (2.125, 2.125)),
            NativePathCommand("L", (10.375, 2.125)),
            NativePathCommand("L", (10.375, 10.375)),
            NativePathCommand("L", (2.125, 10.375)),
            NativePathCommand("Z"),
            NativePathCommand("M", (4, 4)),
            NativePathCommand("L", (4, 8)),
            NativePathCommand("L", (8, 8)),
            NativePathCommand("L", (8, 4)),
            NativePathCommand("Z"),
        ),
    )
    item = DecodedNativeObject(1, 1, LOGICAL_ID, path)
    svg = _svg([item], 12, 12, 1, image_data={RESOURCE_ID: _texture()})
    assert b'x="2.000000" y="2.000000" width="8.500000" height="8.500000"' in svg
    with fitz.open(stream=svg, filetype="svg") as vector:
        with fitz.open(stream=vector.convert_to_pdf()) as document:
            image = document[0].get_pixmap(matrix=fitz.Matrix(4, 4))
    assert image.pixel(13, 13)[:3] == (255, 0, 0)
    assert image.pixel(24, 24)[:3] == (255, 255, 255)
    assert image.pixel(7, 24)[:3] == (255, 255, 255)


@pytest.mark.parametrize("offset", [20_000.0, 99_990.0, -100_002.0])
def test_pattern_mask_keeps_valid_global_coordinates_at_high_texture_density(offset: float) -> None:
    path = replace(
        _path(),
        commands=tuple(
            NativePathCommand(
                command.op,
                tuple(value + offset for value in command.values),
            )
            for command in _path().commands
        ),
        pattern=NativePattern(RESOURCE_ID, offset + 3, offset + 2),
    )
    element = native_pattern_element(path, _texture(), 72 / 132)
    encoded = re.search(r'base64,([^"]+)', element)
    assert encoded is not None
    raster = fitz.Pixmap(base64.b64decode(encoded.group(1)))
    assert (raster.width, raster.height) == (48, 48)
    assert raster.pixel(8, 8) == (255, 0, 0, 255)
