"""Bounded SVG drawing and raster composition for decoded native objects."""

from __future__ import annotations

import base64
import math
import re
from dataclasses import dataclass, replace
from typing import Protocol

import pymupdf as fitz

from ..native.records import NativeImage, NativePath

_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_PATH_ARGUMENTS = {"M": 2, "L": 2, "Q": 4, "C": 6, "A": 7, "Z": 0}
_MAX_SVG_COORDINATE = 100_000.0
_MAX_PATH_COMMANDS = 100_000
_MAX_ENCODED_RASTER_BYTES = 16 * 1024 * 1024
_MAX_STROKE_WIDTH = 100
_MAX_PAGE_RASTER_BYTES = 16 * 1024 * 1024
_MAX_PAGE_RASTER_PIXELS = 25_000_000
_MAX_PATTERN_TILES = 100_000
OBSERVED_PATTERN_PIXELS_PER_NATIVE_UNIT = 6.0


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
    if not math.isfinite(value) or abs(value) > _MAX_SVG_COORDINATE:
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
        if not math.isfinite(stroke_width) or not 0 < stroke_width <= _MAX_STROKE_WIDTH:
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
                or rx > _MAX_SVG_COORDINATE
                or ry > _MAX_SVG_COORDINATE
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
    if any(not math.isfinite(value) or abs(value) > _MAX_SVG_COORDINATE for value in bounds):
        raise ValueError("Native image bounds contain an invalid coordinate")
    if image.width <= 0 or image.height <= 0:
        raise ValueError("Native image bounds must be positive")
    rotation = image.rotation
    if not math.isfinite(rotation):
        raise ValueError("Native image rotation must be finite")
    # Normalize only for SVG output. The decoder checks the stored angle against
    # the independent axis-aligned rectangle before it reaches the renderer.
    rotation = math.atan2(math.sin(rotation), math.cos(rotation))
    center_x, center_y = image.x + image.width / 2, image.y + image.height / 2
    cosine, sine = math.cos(rotation), math.sin(rotation)
    for dx in (-image.width / 2, image.width / 2):
        for dy in (-image.height / 2, image.height / 2):
            for value in (
                center_x + dx,
                center_y + dy,
                center_x + dx * cosine - dy * sine,
                center_y + dx * sine + dy * cosine,
            ):
                _number(value)
                _number(value * scale)
    transform = ""
    if rotation:
        transform = (
            f' transform="rotate({_number(math.degrees(rotation))} '
            f'{_number(center_x * scale)} {_number(center_y * scale)})"'
        )
    if not raster.data or len(raster.data) > _MAX_ENCODED_RASTER_BYTES:
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
        f'preserveAspectRatio="none"{transform} href="data:{raster.mime};base64,{encoded}"/>'
    )


@dataclass(frozen=True)
class _PatternRaster:
    data: bytes
    mime: str
    width: int
    height: int


@dataclass
class PatternRasterBudget:
    bytes_left: int = _MAX_PAGE_RASTER_BYTES
    pixels_left: int = _MAX_PAGE_RASTER_PIXELS

    def charge(self, *, byte_count: int = 0, pixel_count: int = 0) -> None:
        self.bytes_left -= byte_count
        self.pixels_left -= pixel_count
        if self.bytes_left < 0 or self.pixels_left < 0:
            raise ValueError("Native pattern composition exceeds its page budget")


def native_pattern_element(
    path: NativePath,
    raster: RasterImage,
    scale: float,
    *,
    budget: PatternRasterBudget | None = None,
) -> str:
    """Rasterize a bounded repeating texture clipped by the shared path geometry.

    MuPDF's SVG reader does not implement pattern paints or clip paths. Compile
    the path to an alpha mask instead. Six pixels per native unit is observed
    texture density, independent of PNG DPI, and retains partial fidelity.
    """
    pattern = path.pattern
    if pattern is None or path.fill_color is None or path.stroke_color is not None:
        raise ValueError("Native pattern requires a filled path and texture identity")
    native_path_element(path, scale)
    # Validate the resource before any mask allocation, including encoded size.
    native_image_element(NativeImage(pattern.attachment_id, 0, 0, 1, 1), raster, 1)
    xs: list[float] = []
    ys: list[float] = []
    current = subpath_start = (0.0, 0.0)
    for command in path.commands:
        if command.op == "A":
            rx, ry, rotation = command.values[:3]
            x, y = command.values[-2:]
            radius = 0.0
            if rx and ry:
                angle = math.radians(rotation)
                cosine, sine = math.cos(angle), math.sin(angle)
                dx, dy = (current[0] - x) / 2, (current[1] - y) / 2
                enlargement = max(
                    1.0,
                    math.hypot((dx * cosine + dy * sine) / rx, (-dx * sine + dy * cosine) / ry),
                )
                radius = 2 * max(rx, ry) * enlargement
                if not math.isfinite(radius):
                    raise ValueError("Native pattern arc exceeds its geometry budget")
            xs.extend((x - radius, x + radius))
            ys.extend((y - radius, y + radius))
        else:
            xs.extend(command.values[::2])
            ys.extend(command.values[1::2])
        if command.op == "M":
            current = subpath_start = (command.values[-2], command.values[-1])
        elif command.op == "Z":
            current = subpath_start
        elif command.values:
            current = (command.values[-2], command.values[-1])
    if not xs or not ys:
        raise ValueError("Native pattern path has no bounded geometry")
    density = OBSERVED_PATTERN_PIXELS_PER_NATIVE_UNIT
    left, top = math.floor(min(xs) * density), math.floor(min(ys) * density)
    right, bottom = math.ceil(max(xs) * density), math.ceil(max(ys) * density)
    width, height = right - left, bottom - top
    if width <= 0 or height <= 0 or width * height > _MAX_PAGE_RASTER_PIXELS:
        raise ValueError("Native pattern mask exceeds its pixel budget")
    budget = budget if budget is not None else PatternRasterBudget()
    budget.charge(pixel_count=width * height)
    columns = math.ceil(width / raster.width) + 2
    rows = math.ceil(height / raster.height) + 2
    if columns * rows > _MAX_PATTERN_TILES:
        raise ValueError("Native pattern repetition exceeds its render budget")
    origin_x = pattern.x * density
    origin_y = pattern.y * density
    _number(pattern.x * scale)
    _number(pattern.y * scale)
    mask_path = native_path_element(replace(path, fill_color="#ffffff", fill_alpha=1.0), 1)
    mask_svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}pt" height="{height}pt" '
        f'viewBox="{_number(left / density)} {_number(top / density)} '
        f'{_number(width / density)} {_number(height / density)}">{mask_path}</svg>'
    ).encode()
    try:
        source = fitz.Pixmap(raster.data)
        if (source.width, source.height) != (raster.width, raster.height):
            raise ValueError("Native pattern resource dimensions changed")
        if source.colorspace is None or source.colorspace.n != 3:
            source = fitz.Pixmap(fitz.csRGB, source)
        with fitz.open(stream=mask_svg, filetype="svg") as vector:
            with fitz.open(stream=vector.convert_to_pdf()) as document:
                mask = document[0].get_pixmap(alpha=True)
        if (mask.width, mask.height) != (width, height):
            raise ValueError("Native pattern mask dimensions changed")
        source_samples, mask_samples = source.samples_mv, mask.samples_mv
        output = bytearray(width * height * 4)
        phase_x = math.floor(left + 0.5 - origin_x)
        phase_y = math.floor(top + 0.5 - origin_y)
        for y in range(height):
            source_row = ((y + phase_y) % source.height) * source.width * source.n
            for x in range(width):
                target = (y * width + x) * 4
                coverage = mask_samples[target + 3] * path.fill_alpha
                if not coverage:
                    continue
                offset = source_row + ((x + phase_x) % source.width) * source.n
                for channel in range(3):
                    output[target + channel] = round(
                        source_samples[offset + channel] * coverage / 255
                    )
                alpha = source_samples[offset + 3] if source.alpha else 255
                output[target + 3] = round(alpha * coverage / 255)
        del mask_samples, mask
        composed = fitz.Pixmap(fitz.csRGB, width, height, output, True)
        del output
        png = composed.tobytes("png")
    except (RuntimeError, fitz.FileDataError) as exc:
        raise ValueError("Native pattern raster composition failed") from exc
    if len(png) > _MAX_ENCODED_RASTER_BYTES:
        raise ValueError("Native pattern output exceeds its encoded-byte budget")
    budget.charge(byte_count=len(png))
    return native_image_element(
        NativeImage(
            pattern.attachment_id, left / density, top / density, width / density, height / density
        ),
        _PatternRaster(png, "image/png", width, height),
        scale,
    )
