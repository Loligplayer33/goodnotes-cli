"""Bounded SVG primitives for reconstructed native annotation payloads."""

from __future__ import annotations

import base64
import math
import re
from typing import Protocol

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_PATH_ARGUMENTS = {"M": 2, "C": 6, "A": 7, "Z": 0}
_MAX_COORDINATE = 100_000.0
_MAX_PATH_COMMANDS = 100_000
_MAX_IMAGE_BYTES = 16 * 1024 * 1024


class PathCommand(Protocol):
    @property
    def op(self) -> str: ...

    @property
    def values(self) -> tuple[float, ...]: ...


class NativePath(Protocol):
    @property
    def commands(self) -> tuple[PathCommand, ...]: ...

    @property
    def fill_rule(self) -> str: ...

    @property
    def fill_color(self) -> str | None: ...

    @property
    def fill_alpha(self) -> float: ...

    @property
    def stroke_color(self) -> str | None: ...

    @property
    def stroke_width(self) -> float: ...

    @property
    def stroke_alpha(self) -> float: ...


class NativeImage(Protocol):
    @property
    def attachment_id(self) -> str: ...

    @property
    def x(self) -> float: ...

    @property
    def y(self) -> float: ...

    @property
    def width(self) -> float: ...

    @property
    def height(self) -> float: ...


class RasterImage(Protocol):
    @property
    def data(self) -> bytes: ...

    @property
    def mime(self) -> str: ...

    @property
    def width(self) -> int: ...

    @property
    def height(self) -> int: ...


def _number(value: float) -> str:
    if not math.isfinite(value) or abs(value) > _MAX_COORDINATE:
        raise ValueError("Native path contains an invalid coordinate")
    return f"{value:.6f}"


def _color(value: str | None) -> str:
    if value is None or not _COLOR.fullmatch(value):
        raise ValueError("Native path contains an invalid color")
    return value


def _alpha(value: float) -> str:
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError("Native path contains invalid opacity")
    return f"{value:.6f}"


def native_path_element(path: NativePath, scale: float) -> str:
    """Translate a validated typed path into one injection-safe SVG path."""
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Native path scale must be finite and positive")
    if not path.commands or len(path.commands) > _MAX_PATH_COMMANDS:
        raise ValueError("Native path command count is outside the supported range")
    if path.fill_rule != "nonzero":
        raise ValueError("Native path fill rule is unsupported")
    fill_alpha = _alpha(path.fill_alpha)
    if path.fill_color is None:
        fill_attributes = 'fill="none"'
    else:
        fill_attributes = f'fill="{_color(path.fill_color)}" fill-opacity="{fill_alpha}"'

    if path.stroke_color is None:
        if path.stroke_width != 0:
            raise ValueError("Native path has a stroke width without a stroke color")
        stroke_attributes = ""
    else:
        stroke_width = path.stroke_width
        if not math.isfinite(stroke_width) or not 0 < stroke_width <= 100:
            raise ValueError("Native path has an invalid stroke width")
        stroke_attributes = (
            f' stroke="{_color(path.stroke_color)}"'
            f' stroke-width="{_number(stroke_width * scale)}"'
            f' stroke-opacity="{_alpha(path.stroke_alpha)}"'
            ' stroke-linecap="round" stroke-linejoin="round"'
        )

    commands: list[str] = []
    for index, command in enumerate(path.commands):
        op = command.op
        values = command.values
        if op not in _PATH_ARGUMENTS or len(values) != _PATH_ARGUMENTS[op]:
            raise ValueError("Native path has an unsupported command")
        if index == 0 and op != "M":
            raise ValueError("Native path must begin with a move command")
        if op == "A":
            rx, ry, rotation, large_arc, sweep, x, y = values
            if (
                not math.isfinite(rx)
                or not math.isfinite(ry)
                or rx < 0
                or ry < 0
                or rx > _MAX_COORDINATE
                or ry > _MAX_COORDINATE
                or not math.isfinite(rotation)
                or large_arc not in {0, 1}
                or sweep not in {0, 1}
            ):
                raise ValueError("Native path arc parameters are invalid")
            rendered = (
                _number(rx * scale),
                _number(ry * scale),
                _number(rotation),
                str(int(large_arc)),
                str(int(sweep)),
                _number(x * scale),
                _number(y * scale),
            )
        else:
            rendered = tuple(_number(value * scale) for value in values)
        commands.append(op if not rendered else f"{op} {' '.join(rendered)}")

    path_data = " ".join(commands)
    return f'<path d="{path_data}" fill-rule="nonzero" {fill_attributes}{stroke_attributes}/>'


def native_image_element(image: NativeImage, raster: RasterImage, scale: float) -> str:
    """Embed a verified PNG/JPEG byte payload in SVG at its native page bounds."""
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError("Native image scale must be finite and positive")
    if not image.attachment_id:
        raise ValueError("Native image has no copied attachment identity")
    bounds = (image.x, image.y, image.width, image.height)
    if any(not math.isfinite(value) or abs(value) > _MAX_COORDINATE for value in bounds):
        raise ValueError("Native image bounds contain an invalid coordinate")
    if image.width <= 0 or image.height <= 0:
        raise ValueError("Native image bounds must be positive")
    if not raster.data or len(raster.data) > _MAX_IMAGE_BYTES:
        raise ValueError("Native image payload exceeds the per-image byte limit")
    if raster.mime == "image/png":
        expected = b"\x89PNG\r\n\x1a\n"
    elif raster.mime == "image/jpeg":
        expected = b"\xff\xd8\xff"
    else:
        raise ValueError("Native image format is unsupported")
    if not raster.data.startswith(expected):
        raise ValueError("Native image bytes do not match the verified MIME type")
    if (
        isinstance(raster.width, bool)
        or not isinstance(raster.width, int)
        or isinstance(raster.height, bool)
        or not isinstance(raster.height, int)
        or raster.width <= 0
        or raster.height <= 0
        or raster.width * raster.height > 25_000_000
    ):
        raise ValueError("Native image dimensions exceed the per-image pixel limit")
    encoded = base64.b64encode(raster.data).decode("ascii")
    return (
        f'<image x="{_number(image.x * scale)}" y="{_number(image.y * scale)}" '
        f'width="{_number(image.width * scale)}" height="{_number(image.height * scale)}" '
        f'preserveAspectRatio="none" href="data:{raster.mime};base64,{encoded}"/>'
    )
