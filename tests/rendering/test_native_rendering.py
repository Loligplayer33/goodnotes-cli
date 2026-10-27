from __future__ import annotations

import base64
import math
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pymupdf as fitz
import pytest

from goodnotes_cli.models import Page
from goodnotes_cli.native import NativeError
from goodnotes_cli.native.assets import NativeImageData
from goodnotes_cli.native.records import (
    DecodedNativeObject,
    Ink,
    NativeImage,
    NativePath,
    NativePathCommand,
)
from goodnotes_cli.rendering import overlay as annotation_module
from goodnotes_cli.rendering.ink_svg import _svg, _variable_width_outline
from goodnotes_cli.rendering.overlay import _record_image_failures, _resolve_native_images
from goodnotes_cli.rendering.svg import native_image_element, native_path_element


@dataclass(frozen=True)
class PathCommandFixture:
    op: str
    values: tuple[float, ...]


@dataclass(frozen=True)
class NativePathFixture:
    commands: tuple[PathCommandFixture, ...]
    fill_rule: str
    fill_color: str | None
    fill_alpha: float
    stroke_color: str | None = None
    stroke_alpha: float = 0
    stroke_width: float = 0
    dash: tuple[float, ...] = ()
    line_cap: str = "round"
    line_join: str = "round"


@dataclass(frozen=True)
class NativeImageFixture:
    attachment_id: str
    x: float
    y: float
    width: float
    height: float


@dataclass(frozen=True)
class RasterFixture:
    data: bytes
    mime: str
    width: int
    height: int


@dataclass(frozen=True)
class ImageSandbox:
    root: Path
    library_dir: Path
    attachments_dir: Path
    projection_path: Path


def _dark_pixels_at_x(svg: bytes, x: int) -> int:
    with fitz.open(stream=svg, filetype="svg") as vector:
        with fitz.open(stream=vector.convert_to_pdf()) as document:
            pixmap = document[0].get_pixmap(alpha=False)
    return sum(pixmap.pixel(x, y)[:3] == (0, 0, 0) for y in range(pixmap.height))


def _png_fixture() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


def test_variable_width_outline_changes_stroke_width_and_keeps_rounded_caps() -> None:
    ink = Ink(
        points=((5, 10), (20, 10), (35, 10)),
        width=2,
        color="#000000",
        alpha=1,
        widths=(2, 10, 2),
    )

    svg = _svg([ink], width=40, height=20, scale=1)

    assert b"<path " in svg
    assert b"<polyline " not in svg
    assert _dark_pixels_at_x(svg, 20) >= 4 * _dark_pixels_at_x(svg, 5)


def test_variable_width_outline_rejects_misaligned_or_nonfinite_widths() -> None:
    with pytest.raises(ValueError, match="does not match"):
        _variable_width_outline(((0, 0), (10, 0)), (2,), 1)
    with pytest.raises(ValueError, match="supported range"):
        _variable_width_outline(((0, 0), (10, 0)), (2, math.inf), 1)
    with pytest.raises(ValueError, match="finite and positive"):
        _variable_width_outline(((0, 0),), (2,), math.inf)


def test_variable_width_outline_coalesces_repeated_samples() -> None:
    path = _variable_width_outline(((0, 0), (0, 0), (10, 0)), (2, 6, 2), 1)

    assert path.count("M ") == 1
    assert path.count("A ") == 2


def test_native_path_commands_are_scaled_and_styles_are_explicit() -> None:
    path = NativePathFixture(
        commands=(
            PathCommandFixture("M", (10, 20)),
            PathCommandFixture("C", (10, 30, 20, 30, 20, 20)),
            PathCommandFixture("A", (5, 5, 0, 0, 1, 30, 20)),
            PathCommandFixture("Z", ()),
        ),
        fill_rule="nonzero",
        fill_color="#ef514a",
        fill_alpha=0.9,
    )

    element = native_path_element(path, scale=0.5)

    assert 'd="M 5.000000 10.000000 C ' in element
    assert "A 2.500000 2.500000 0.000000 0 1 15.000000 10.000000" in element
    assert 'fill="#ef514a"' in element
    assert 'fill-opacity="0.900000"' in element
    assert 'fill-rule="nonzero"' in element


def test_native_path_supports_stroke_only_geometry_with_rounded_joins() -> None:
    path = NativePathFixture(
        commands=(
            PathCommandFixture("M", (10, 10)),
            PathCommandFixture("C", (10, 20, 20, 20, 20, 10)),
            PathCommandFixture("C", (20, 0, 10, 0, 10, 10)),
        ),
        fill_rule="nonzero",
        fill_color=None,
        fill_alpha=0,
        stroke_color="#00ff00",
        stroke_alpha=0.75,
        stroke_width=4,
    )

    element = native_path_element(path, scale=0.5)

    assert 'fill="none"' in element
    assert 'stroke="#00ff00"' in element
    assert 'stroke-width="2.000000"' in element
    assert 'stroke-opacity="0.750000"' in element
    assert 'stroke-linecap="round" stroke-linejoin="round"' in element


def test_native_stroke_path_renders_as_a_green_outline() -> None:
    path = NativePath(
        commands=(
            NativePathCommand("M", (20, 10)),
            NativePathCommand("C", (25.5, 10, 30, 14.5, 30, 20)),
            NativePathCommand("C", (30, 25.5, 25.5, 30, 20, 30)),
            NativePathCommand("C", (14.5, 30, 10, 25.5, 10, 20)),
            NativePathCommand("C", (10, 14.5, 14.5, 10, 20, 10)),
        ),
        fill_rule="nonzero",
        fill_color=None,
        fill_alpha=0,
        stroke_color="#00ff00",
        stroke_alpha=1,
        stroke_width=2,
    )
    object_fixture = DecodedNativeObject(1, 1, "generated-path", path)
    svg = _svg([object_fixture], width=40, height=40, scale=1)

    with fitz.open(stream=svg, filetype="svg") as vector:
        pdf = vector.convert_to_pdf()
    with fitz.open(stream=pdf, filetype="pdf") as document:
        pixel = document[0].get_pixmap(alpha=False).pixel(20, 10)[:3]

    assert pixel == (0, 255, 0)


def test_native_path_rejects_invalid_svg_values() -> None:
    path = NativePathFixture(
        commands=(PathCommandFixture("M", (0, 0)), PathCommandFixture("Z", ())),
        fill_rule="nonzero",
        fill_color='"/><script/>',
        fill_alpha=1,
    )

    with pytest.raises(ValueError, match="invalid color"):
        native_path_element(path, scale=1)


def test_native_image_is_embedded_as_verified_png_data_uri() -> None:
    image = NativeImageFixture("generated-image-id", 10, 20, 30, 40)
    raster = RasterFixture(_png_fixture(), "image/png", 1, 1)

    element = native_image_element(image, raster, scale=0.5)

    assert 'x="5.000000" y="10.000000" width="15.000000" height="20.000000"' in element
    assert 'preserveAspectRatio="none"' in element
    assert f'href="data:image/png;base64,{base64.b64encode(raster.data).decode()}"' in element


def test_native_image_rejects_mismatched_mime_and_excessive_pixels() -> None:
    image = NativeImageFixture("generated-image-id", 0, 0, 10, 10)
    with pytest.raises(ValueError, match="MIME type"):
        native_image_element(image, RasterFixture(b"not-png", "image/png", 1, 1), 1)
    with pytest.raises(ValueError, match="pixel limit"):
        native_image_element(image, RasterFixture(_png_fixture(), "image/png", 5001, 5000), 1)


def _image_objects(*attachment_ids: str) -> list[DecodedNativeObject]:
    return [
        DecodedNativeObject(
            actor=1,
            sequence=index,
            logical_id=f"generated-logical-{index}",
            payload=NativeImage(attachment_id, 0, 0, 10, 10),
        )
        for index, attachment_id in enumerate(attachment_ids)
    ]


def _image_page() -> Page:
    return Page(
        id="00000000-0000-4000-8000-000000000100",
        document_id="generated-document",
        number=1,
        position="1",
        width=440,
        height=330,
    )


@pytest.mark.parametrize("integrity", ["scanlines-checked", "decoder-unverified"])
def test_native_image_integrity_survives_page_rendering(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    integrity: Literal["scanlines-checked", "decoder-unverified"],
) -> None:
    sandbox = ImageSandbox(tmp_path, tmp_path, tmp_path, tmp_path / "projection.sqlite")
    metadata: dict[str, object] = {
        "rendered_objects": 1,
        "rendered_strokes": 0,
        "rendered_images": 1,
        "unsupported_objects": 0,
        "coverage": "supported-native-records",
        "warnings": [],
    }
    monkeypatch.setattr(
        annotation_module,
        "decode_native_objects",
        lambda *_: (_image_objects("generated-image"), metadata),
    )
    monkeypatch.setattr(
        annotation_module,
        "load_native_image",
        lambda *_: NativeImageData(_png_fixture(), "image/png", 1, 1, integrity),
    )
    overlay = annotation_module.render_native_overlay(
        sandbox,
        _image_page(),
        width_points=240,
        height_points=180,
        record_reader=lambda _: [(b"generated-key", b"generated-value")],
    )

    assert overlay.metadata["image_integrity"] == {integrity: 1}
    assert b"data:image/png;base64," in overlay.svg
    if integrity == "decoder-unverified":
        assert overlay.metadata["coverage"] == "partial"
        assert "JPEG pixel integrity is unverified" in str(overlay.metadata["warnings"])
    else:
        assert overlay.metadata["warnings"] == []


def test_page_image_byte_budget_omits_overflow_and_keeps_later_small_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = ImageSandbox(tmp_path, tmp_path, tmp_path, tmp_path / "projection.sqlite")
    assets = {
        "large-a": NativeImageData(b"a" * (10 * 1024 * 1024), "image/png", 1, 1),
        "large-b": NativeImageData(b"b" * (7 * 1024 * 1024), "image/png", 1, 1),
        "small": NativeImageData(b"s", "image/png", 1, 1),
    }
    calls: list[str] = []

    def load(_sandbox: object, _document: str, attachment_id: str) -> NativeImageData:
        calls.append(attachment_id)
        return assets[attachment_id]

    monkeypatch.setattr(annotation_module, "load_native_image", load)
    objects, resolved, failures = _resolve_native_images(
        sandbox, _image_page(), _image_objects("large-a", "large-b", "small")
    )

    rendered_ids = [
        obj.payload.attachment_id for obj in objects if isinstance(obj.payload, NativeImage)
    ]
    assert rendered_ids == [
        "large-a",
        "small",
    ]
    assert set(resolved) == {"large-a", "small"}
    assert calls == ["large-a", "large-b", "small"]
    assert failures == {"native-image-budget-exceeded": 1}


def test_page_image_pixel_budget_and_unavailable_image_counts_are_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = ImageSandbox(tmp_path, tmp_path, tmp_path, tmp_path / "projection.sqlite")
    assets = {
        "first": NativeImageData(b"1", "image/png", 13_000_000, 1),
        "overflow": NativeImageData(b"2", "image/png", 13_000_000, 1),
        "last": NativeImageData(b"3", "image/png", 1_000_000, 1),
    }

    def load(_sandbox: object, _document: str, attachment_id: str) -> NativeImageData:
        if attachment_id == "missing":
            raise NativeError(
                "native_data_unavailable", "Generated missing image.", operation="test"
            )
        return assets[attachment_id]

    monkeypatch.setattr(annotation_module, "load_native_image", load)
    objects, resolved, failures = _resolve_native_images(
        sandbox,
        _image_page(),
        _image_objects("first", "overflow", "last", "missing", "missing"),
    )
    metadata: dict[str, object] = {
        "rendered_objects": 5,
        "rendered_images": 5,
        "rendered_strokes": 0,
        "unsupported_objects": 0,
        "unsupported_reasons": {},
        "warnings": [],
        "coverage": "supported-native-records",
    }
    _record_image_failures(metadata, failures)

    rendered_ids = [
        obj.payload.attachment_id for obj in objects if isinstance(obj.payload, NativeImage)
    ]
    assert rendered_ids == [
        "first",
        "last",
    ]
    assert set(resolved) == {"first", "last"}
    assert failures == {
        "native-image-budget-exceeded": 1,
        "native-image-unavailable": 2,
    }
    assert metadata["rendered_objects"] == 2
    assert metadata["rendered_images"] == 2
    assert metadata["rendered_strokes"] == 0
    assert metadata["unsupported_objects"] == 3
    assert metadata["coverage"] == "partial"
    assert metadata["unsupported_reasons"] == failures
    assert any("page image budget" in warning for warning in metadata["warnings"])
