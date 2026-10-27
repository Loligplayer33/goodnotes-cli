"""Validate and decode bounded native PNG and JPEG images."""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass
from typing import Literal

import pymupdf

from .errors import NativeError

_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_PIXELS = 25_000_000
_IMAGE_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class NativeImageData:
    """A bounded, decoded raster attachment with its actual image dimensions."""

    data: bytes
    mime: str
    width: int
    height: int
    integrity: Literal["scanlines-checked", "decoder-unverified"] = "decoder-unverified"


def _decode_native_image(data: bytes) -> NativeImageData:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        mime = "image/png"
        dimensions = _png_dimensions(data)
    elif data.startswith(b"\xff\xd8"):
        mime = "image/jpeg"
        dimensions = _jpeg_dimensions(data)
    else:
        raise _native_failure(
            "The referenced native image is not a supported PNG or JPEG.",
            operation="validate_native_image",
        )
    if dimensions is None:
        raise _native_failure(
            "The referenced native image has an invalid or incomplete header.",
            operation="validate_native_image",
        )
    width, height = dimensions
    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
        raise _native_failure(
            "The referenced native image exceeds the decoded pixel limit.",
            operation="validate_native_image",
        )
    try:
        pixmap = pymupdf.Pixmap(data)
        decoded_size = pixmap.width, pixmap.height
        del pixmap
    except (RuntimeError, ValueError, OSError, pymupdf.mupdf.FzErrorBase) as exc:
        raise _native_failure(
            "The referenced native image could not be decoded as a raster image.",
            operation="validate_native_image",
        ) from exc
    if decoded_size != dimensions or decoded_size[0] * decoded_size[1] > _MAX_IMAGE_PIXELS:
        raise _native_failure(
            "The decoded image dimensions do not match its header or exceed the pixel limit.",
            operation="validate_native_image",
        )
    integrity: Literal["scanlines-checked", "decoder-unverified"] = (
        "scanlines-checked" if mime == "image/png" else "decoder-unverified"
    )
    return NativeImageData(data, mime, width, height, integrity)


def _png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 33 or len(data) > _MAX_IMAGE_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    offset = 8
    dimensions: tuple[int, int] | None = None
    color_type: int | None = None
    bit_depth: int | None = None
    interlace: int | None = None
    palette_entries: int | None = None
    idat_chunks: list[memoryview] = []
    saw_idat = False
    ended_idat = False
    saw_iend = False
    for _ in range(100_000):
        if offset + 12 > len(data):
            return None
        length = struct.unpack_from(">I", data, offset)[0]
        kind = data[offset + 4 : offset + 8]
        end = offset + 12 + length
        if end > len(data):
            return None
        chunk_data = data[offset + 8 : offset + 8 + length]
        expected_crc = struct.unpack_from(">I", data, offset + 8 + length)[0]
        if zlib.crc32(kind + chunk_data) & 0xFFFFFFFF != expected_crc:
            return None

        if offset == 8:
            if kind != b"IHDR" or length != 13:
                return None
            (
                width,
                height,
                bit_depth_value,
                color_type_value,
                compression,
                filter_method,
                interlace_value,
            ) = struct.unpack_from(">IIBBBBB", chunk_data, 0)
            dimensions = width, height
            bit_depth = bit_depth_value
            color_type = color_type_value
            interlace = interlace_value
            if (
                width <= 0
                or height <= 0
                or compression != 0
                or filter_method != 0
                or interlace not in {0, 1}
                or not _png_bit_depth_supported(color_type, bit_depth)
            ):
                return None
            if width * height > _MAX_IMAGE_PIXELS:
                return dimensions
        elif kind == b"IHDR":
            return None

        if kind == b"PLTE":
            if saw_idat or palette_entries is not None or not length or length % 3:
                return None
            palette_entries = length // 3
            if palette_entries > 256:
                return None
        elif kind == b"IDAT":
            if ended_idat:
                return None
            saw_idat = True
            idat_chunks.append(memoryview(data)[offset + 8 : offset + 8 + length])
        elif saw_idat and kind != b"IEND":
            ended_idat = True

        if kind == b"IEND":
            if length != 0 or not saw_idat or end != len(data):
                return None
            saw_iend = True
            break

        if kind not in {b"IHDR", b"PLTE", b"IDAT"} and not (kind[0] & 0x20):
            return None

        offset = end

    if (
        dimensions is None
        or color_type is None
        or bit_depth is None
        or interlace is None
        or not saw_iend
        or not idat_chunks
        or (color_type == 3 and palette_entries is None)
        or (color_type == 3 and palette_entries is not None and palette_entries > 2**bit_depth)
    ):
        return None
    layout = _png_scanline_layout(*dimensions, color_type, bit_depth, interlace)
    if layout is None or not _png_deflate_matches_scanlines(idat_chunks, layout):
        return None
    return dimensions


def _png_bit_depth_supported(color_type: int, bit_depth: int) -> bool:
    supported = {
        0: {1, 2, 4, 8, 16},
        2: {8, 16},
        3: {1, 2, 4, 8},
        4: {8, 16},
        6: {8, 16},
    }
    return bit_depth in supported.get(color_type, set())


def _png_scanline_layout(
    width: int, height: int, color_type: int, bit_depth: int, interlace: int
) -> tuple[tuple[int, int], ...] | None:
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        return None
    bits_per_pixel = channels * bit_depth
    if interlace == 0:
        return ((1 + (width * bits_per_pixel + 7) // 8, height),)

    adam7 = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    layout: list[tuple[int, int]] = []
    for x_start, y_start, x_step, y_step in adam7:
        pass_width = max(0, (width - x_start + x_step - 1) // x_step)
        pass_height = max(0, (height - y_start + y_step - 1) // y_step)
        if pass_width and pass_height:
            row_bytes = 1 + (pass_width * bits_per_pixel + 7) // 8
            layout.append((row_bytes, pass_height))
    return tuple(layout)


def _png_deflate_matches_scanlines(
    compressed_chunks: list[memoryview], layout: tuple[tuple[int, int], ...]
) -> bool:
    """Check the bounded PNG zlib stream and each scanline filter byte."""
    if not layout:
        return False
    expected_bytes = sum(row_bytes * row_count for row_bytes, row_count in layout)
    decompressor = zlib.decompressobj()
    layout_index = 0
    row_bytes, rows_remaining = layout[0]
    row_position = 0
    decoded_bytes = 0

    def consume(output: bytes) -> bool:
        nonlocal layout_index, row_bytes, rows_remaining, row_position, decoded_bytes
        if decoded_bytes + len(output) > expected_bytes:
            return False
        decoded_bytes += len(output)
        cursor = 0
        while cursor < len(output):
            if row_position == 0:
                if output[cursor] > 4:
                    return False
                row_position = 1
                cursor += 1
            copied = min(len(output) - cursor, row_bytes - row_position)
            row_position += copied
            cursor += copied
            if row_position == row_bytes:
                row_position = 0
                rows_remaining -= 1
                if rows_remaining == 0:
                    layout_index += 1
                    if layout_index < len(layout):
                        row_bytes, rows_remaining = layout[layout_index]
        return True

    try:
        for compressed in compressed_chunks:
            if decompressor.eof:
                if compressed:
                    return False
                continue
            pending: bytes | memoryview = compressed
            while True:
                output = decompressor.decompress(pending, _IMAGE_READ_CHUNK)
                pending = decompressor.unconsumed_tail
                if not consume(output) or decompressor.unused_data:
                    return False
                if pending:
                    continue
                if len(output) == _IMAGE_READ_CHUNK:
                    pending = b""
                    continue
                break
    except zlib.error:
        return False

    return (
        decompressor.eof
        and not decompressor.unused_data
        and not decompressor.unconsumed_tail
        and decoded_bytes == expected_bytes
        and row_position == 0
        and rows_remaining == 0
        and layout_index == len(layout)
    )


_JPEG_START_OF_FRAME_MARKERS = {
    0xC0,
    0xC1,
    0xC2,
    0xC3,
    0xC5,
    0xC6,
    0xC7,
    0xC9,
    0xCA,
    0xCB,
    0xCD,
    0xCE,
    0xCF,
}


def _jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if (
        len(data) < 4
        or len(data) > _MAX_IMAGE_BYTES
        or not data.startswith(b"\xff\xd8")
        or not data.endswith(b"\xff\xd9")
    ):
        return None
    index = 2
    segments = 0
    frame_marker: int | None = None
    dimensions: tuple[int, int] | None = None
    components: dict[int, tuple[int, int]] = {}
    max_horizontal = max_vertical = 0
    sequential: set[int] = set()
    progressive_dc: set[int] = set()
    saw_scan = False

    while index < len(data) and segments < 100_000:
        if data[index] != 0xFF:
            return None
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data):
            return None
        marker = data[index]
        index += 1
        if marker == 0xD9:
            if index != len(data) or dimensions is None or not saw_scan:
                return None
            if frame_marker in {0xC0, 0xC1} and sequential != set(components):
                return None
            if frame_marker == 0xC2 and progressive_dc != set(components):
                return None
            return dimensions
        if marker == 0x01:
            continue
        if marker == 0xD8 or 0xD0 <= marker <= 0xD7 or index + 2 > len(data):
            return None
        length = struct.unpack_from(">H", data, index)[0]
        if length < 2 or index + length > len(data):
            return None
        end = index + length
        payload = data[index + 2 : end]

        if marker in _JPEG_START_OF_FRAME_MARKERS:
            if frame_marker is not None or marker not in {0xC0, 0xC1, 0xC2} or len(payload) < 9:
                return None
            precision, height, width, count = struct.unpack_from(">BHHB", payload)
            if (
                not 1 <= count <= 4
                or len(payload) != 6 + 3 * count
                or width == 0
                or height == 0
                or precision not in ({8} if marker == 0xC0 else {8, 12})
            ):
                return None
            for part in range(count):
                offset = 6 + part * 3
                component = payload[offset]
                sampling = payload[offset + 1]
                horizontal, vertical = sampling >> 4, sampling & 0x0F
                if (
                    component in components
                    or not 1 <= horizontal <= 4
                    or not 1 <= vertical <= 4
                    or payload[offset + 2] > 3
                ):
                    return None
                components[component] = horizontal, vertical
            if sum(horizontal * vertical for horizontal, vertical in components.values()) > 10:
                return None
            max_horizontal = max(value[0] for value in components.values())
            max_vertical = max(value[1] for value in components.values())
            frame_marker = marker
            dimensions = width, height
            if width * height > _MAX_IMAGE_PIXELS:
                return dimensions
        elif marker == 0xDA:
            if frame_marker is None or not payload:
                return None
            count = payload[0]
            if not 1 <= count <= len(components) or len(payload) != 1 + 2 * count + 3:
                return None
            selected = tuple(payload[1 + 2 * part] for part in range(count))
            selectors = tuple(payload[2 + 2 * part] for part in range(count))
            if (
                len(set(selected)) != len(selected)
                or any(component not in components for component in selected)
                or any(selector >> 4 > 3 or selector & 0x0F > 3 for selector in selectors)
            ):
                return None
            start, stop, approximation = payload[-3:]
            high, low = approximation >> 4, approximation & 0x0F
            if frame_marker in {0xC0, 0xC1}:
                if (start, stop, high, low) != (0, 63, 0, 0):
                    return None
                codes_per_block = 2
            else:
                if not 0 <= start <= stop <= 63 or high > 13 or low > 13:
                    return None
                if high and high != low + 1:
                    return None
                if start == 0 and stop != 0:
                    return None
                if start > 0 and count != 1:
                    return None
                codes_per_block = 1 if start == 0 else 0
            if count > 1:
                mcu_columns = (width + 8 * max_horizontal - 1) // (8 * max_horizontal)
                mcu_rows = (height + 8 * max_vertical - 1) // (8 * max_vertical)
                blocks = (
                    mcu_columns
                    * mcu_rows
                    * sum(
                        components[component][0] * components[component][1]
                        for component in selected
                    )
                )
            else:
                horizontal, vertical = components[selected[0]]
                blocks = ((width * horizontal + 8 * max_horizontal - 1) // (8 * max_horizontal)) * (
                    (height * vertical + 8 * max_vertical - 1) // (8 * max_vertical)
                )
            required_bits = blocks * codes_per_block
            if frame_marker == 0xC2 and start > 0:
                required_bits = 1
            scan_marker = _jpeg_scan_end(data, end)
            if scan_marker is None or (scan_marker - end) * 8 < required_bits:
                return None
            if frame_marker in {0xC0, 0xC1}:
                if sequential.intersection(selected):
                    return None
                sequential.update(selected)
            elif start == 0 and high == 0:
                if progressive_dc.intersection(selected):
                    return None
                progressive_dc.update(selected)
            saw_scan = True
            index = scan_marker
            segments += 1
            continue
        index = end
        segments += 1
    return None


def _jpeg_scan_end(data: bytes, start: int) -> int | None:
    """Find the next non-restart marker while respecting entropy byte stuffing."""
    index = start
    while index < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker_start = index
        index += 1
        if index >= len(data):
            return None
        if data[index] == 0x00:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        if index >= len(data) or data[index] == 0x00:
            return None
        marker = data[index]
        if 0xD0 <= marker <= 0xD7:
            index += 1
            continue
        return marker_start
    return None


def _native_failure(message: str, *, operation: str) -> NativeError:
    return NativeError(
        "native_data_unavailable",
        message,
        operation=operation,
    )
