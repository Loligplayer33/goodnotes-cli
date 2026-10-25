"""Read copied native image attachments through their projection bindings."""

from __future__ import annotations

import hashlib
import json
import math
import os
import sqlite3
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import pymupdf

from .errors import GoodnotesError
from .native import NativeError
from .safety import (
    _require_not_production,
    open_database,
    path_contains,
    resolve_confined_file,
    stat_key,
)

_MAX_IMAGE_BYTES = 16 * 1024 * 1024
_MAX_IMAGE_PIXELS = 25_000_000
_IMAGE_READ_CHUNK = 1024 * 1024
_MAX_ATTACHMENT_ROWS = 10_000
_MAX_STORED_PATH_BYTES = 4096
_MAX_ATTACHMENT_SCAN_ENTRIES = 200_000
_FINGERPRINT_VERSION = "native-attachments-v1"


class AttachmentSandbox(Protocol):
    """The validated paths needed to read one copied attachment."""

    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...

    @property
    def attachments_dir(self) -> Path: ...

    @property
    def projection_path(self) -> Path: ...


@dataclass(frozen=True)
class NativeImageData:
    """A bounded, decoded raster attachment with its actual image dimensions."""

    data: bytes
    mime: str
    width: int
    height: int
    integrity: Literal["scanlines-checked", "decoder-unverified"] = "decoder-unverified"


def native_attachment_fingerprint(
    sandbox: AttachmentSandbox, document_id: str
) -> tuple[object, ...]:
    """Return a conservative, content-free signature for a notebook's copied assets.

    The projection and attachment files are never opened for writing. Missing rows,
    unavailable bindings, and unsafe unused paths remain representable so an unrelated
    incomplete attachment does not prevent native pen rendering.
    """

    if not _bounded_identifier(document_id):
        raise _native_failure(
            "The requested notebook binding exceeds the safe metadata limit.",
            operation="fingerprint_native_attachments",
        )
    projection_signature = _projection_signature(sandbox)
    attachment_root, root_signature = _attachment_root(sandbox)
    try:
        with open_database(sandbox.projection_path) as connection:
            columns = _attachment_columns(connection)
            required = {"id", "path", "available", "document_id"}
            if not required.issubset(columns):
                schema_state: object = ("schema-unavailable", tuple(sorted(columns)))
                rows: list[sqlite3.Row] = []
            else:
                schema_state = "schema-available"
                rows = connection.execute(
                    "SELECT id, path, available FROM attachments "
                    "WHERE document_id = ? ORDER BY id, path LIMIT ?",
                    (document_id, _MAX_ATTACHMENT_ROWS + 1),
                ).fetchall()
    except sqlite3.Error as exc:
        raise _native_failure(
            "Could not read attachment bindings from the copied projection.",
            operation="fingerprint_native_attachments",
        ) from exc

    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while attachment metadata was read.",
            operation="fingerprint_native_attachments",
        )
    if len(rows) > _MAX_ATTACHMENT_ROWS:
        raise _native_failure(
            "The copied projection has too many image bindings to fingerprint safely.",
            operation="fingerprint_native_attachments",
        )

    document_signature = _metadata_digest((document_id,))
    if schema_state != "schema-available":
        _ensure_root_unchanged(attachment_root, root_signature)
        return (
            _FINGERPRINT_VERSION,
            projection_signature,
            root_signature,
            document_signature,
            schema_state,
        )

    basename_map: dict[str, list[Path]] | None = None
    bindings: list[tuple[object, ...]] = []
    for row in rows:
        raw_path = row["path"]
        if isinstance(raw_path, str):
            state, file_signature, basename_map = _fingerprint_path(
                attachment_root,
                raw_path,
                basename_map=basename_map,
            )
        elif raw_path is None:
            state, file_signature = "missing", None
        else:
            state, file_signature = "unavailable", None
        metadata_signature = _metadata_digest((row["id"], raw_path, row["available"]))
        bindings.append((metadata_signature, state, file_signature))

    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while attachment metadata was read.",
            operation="fingerprint_native_attachments",
        )
    _ensure_root_unchanged(attachment_root, root_signature)
    return (
        _FINGERPRINT_VERSION,
        projection_signature,
        root_signature,
        document_signature,
        schema_state,
        tuple(bindings),
    )


def load_native_image(
    sandbox: AttachmentSandbox, document_id: str, attachment_id: str
) -> NativeImageData:
    """Read and verify one available PNG/JPEG bound to this copied notebook."""

    if not _bounded_identifier(document_id) or not _bounded_identifier(attachment_id):
        raise _native_failure(
            "The requested image binding exceeds the safe metadata limit.",
            operation="load_native_image_binding",
        )
    projection_signature = _projection_signature(sandbox)
    attachment_root, root_signature = _attachment_root(sandbox)
    row = _load_attachment_binding(sandbox, document_id, attachment_id)
    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while the image binding was checked.",
            operation="load_native_image_binding",
        )

    if not _is_available(row["available"]):
        raise _native_failure(
            "The referenced copied image attachment is unavailable.",
            operation="load_native_image_binding",
        )
    raw_path = row["path"]
    if not isinstance(raw_path, str) or not raw_path:
        raise _native_failure(
            "The referenced copied image has no usable attachment path.",
            operation="resolve_native_image",
        )

    resolved, state, _ = _resolve_attachment_path(attachment_root, raw_path)
    if resolved is None:
        descriptions = {
            "missing": "The referenced copied image is missing.",
            "unsafe": (
                "The referenced copied image path is unsafe or outside the attachment folder."
            ),
            "ambiguous": "The referenced copied image path has an ambiguous basename.",
            "unavailable": "The referenced copied image could not be inspected safely.",
        }
        raise _native_failure(
            descriptions.get(state, "The referenced copied image is unavailable."),
            operation="resolve_native_image",
        )

    data, expected_signature = _read_stable_image_file(attachment_root, resolved)
    second_path, second_state, _ = _resolve_attachment_path(attachment_root, raw_path)
    if second_state != "available" or second_path != resolved:
        raise _native_failure(
            "The copied image binding changed while the attachment was read.",
            operation="read_native_image",
        )
    _ensure_root_unchanged(
        attachment_root,
        root_signature,
        operation="read_native_image",
    )
    try:
        second_signature = stat_key(second_path.stat())
    except OSError as exc:
        raise _native_failure(
            "The copied image changed while the attachment was read.",
            operation="read_native_image",
        ) from exc
    if second_signature != expected_signature:
        raise _native_failure(
            "The copied image changed while the attachment was read.",
            operation="read_native_image",
        )
    if _projection_signature(sandbox) != projection_signature:
        raise _native_failure(
            "The copied projection changed while the image was read.",
            operation="read_native_image",
        )

    return _decode_native_image(data)


def _load_attachment_binding(
    sandbox: AttachmentSandbox, document_id: str, attachment_id: str
) -> sqlite3.Row:
    try:
        with open_database(sandbox.projection_path) as connection:
            columns = _attachment_columns(connection)
            required = {"id", "path", "available", "document_id"}
            if not required.issubset(columns):
                raise _native_failure(
                    "The copied projection cannot verify this image's notebook binding.",
                    operation="load_native_image_binding",
                )
            rows = connection.execute(
                "SELECT id, path, available FROM attachments "
                "WHERE document_id = ? AND id = ? ORDER BY id, path LIMIT 2",
                (document_id, attachment_id),
            ).fetchall()
    except NativeError:
        raise
    except sqlite3.Error as exc:
        raise _native_failure(
            "Could not read the referenced image binding from the copied projection.",
            operation="load_native_image_binding",
        ) from exc

    if len(rows) != 1:
        raise _native_failure(
            "The referenced image has no unique binding to this notebook.",
            operation="load_native_image_binding",
        )
    return rows[0]


def _attachment_columns(connection: sqlite3.Connection) -> set[str]:
    table = connection.execute(
        "SELECT 1 FROM sqlite_schema WHERE type = 'table' AND name = 'attachments'"
    ).fetchone()
    if table is None:
        return set()
    return {row["name"] for row in connection.execute("PRAGMA table_info(attachments)")}


def _is_available(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


def _bounded_identifier(value: str) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return len(value.encode("utf-8", errors="strict")) <= _MAX_STORED_PATH_BYTES
    except UnicodeError:
        return False


def _projection_signature(sandbox: AttachmentSandbox) -> tuple[int, int, int, int, int, int]:
    path = Path(sandbox.projection_path)
    try:
        info = path.lstat()
    except OSError as exc:
        raise _native_failure(
            "The copied projection could not be inspected safely.",
            operation="inspect_native_image_projection",
        ) from exc
    if not stat.S_ISREG(info.st_mode):
        raise _native_failure(
            "The copied projection is not a regular file.",
            operation="inspect_native_image_projection",
        )
    return stat_key(info)


def _attachment_root(
    sandbox: AttachmentSandbox,
) -> tuple[Path, tuple[int, int, int, int, int, int]]:
    requested = Path(sandbox.attachments_dir)
    try:
        requested_info = requested.lstat()
        root = requested.resolve(strict=True)
        root_info = root.lstat()
        snapshot_root = Path(sandbox.root).resolve(strict=True)
        library_root = Path(sandbox.library_dir).resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _native_failure(
            "The copied attachment folder could not be inspected safely.",
            operation="inspect_native_image_attachments",
        ) from exc
    if (
        stat.S_ISLNK(requested_info.st_mode)
        or not stat.S_ISDIR(root_info.st_mode)
        or not path_contains(snapshot_root, root)
        or root.parent != library_root
        or root.name.casefold() != "attachments"
    ):
        raise _native_failure(
            "The image attachment folder is not inside the validated copied snapshot.",
            operation="inspect_native_image_attachments",
        )
    _require_not_production(root, purpose="native image attachments")
    return root, stat_key(root_info)


def _ensure_root_unchanged(
    root: Path,
    expected: tuple[int, int, int, int, int, int],
    *,
    operation: str = "fingerprint_native_attachments",
) -> None:
    try:
        current = stat_key(root.lstat())
    except OSError as exc:
        raise _native_failure(
            "The copied attachment folder changed while it was inspected.",
            operation=operation,
        ) from exc
    if current != expected:
        raise _native_failure(
            "The copied attachment folder changed while it was inspected.",
            operation=operation,
        )


def _resolve_attachment_path(
    root: Path,
    raw_path: str,
    *,
    basename_map: dict[str, list[Path]] | None = None,
) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
    """Resolve a copied binding with the library's unique-basename fallback."""

    def result(
        path: Path | None, state: str
    ) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
        return path, state, basename_map

    try:
        stored = Path(raw_path)
    except (TypeError, ValueError, OSError):
        return result(None, "unavailable")
    try:
        if len(os.fsencode(raw_path)) > _MAX_STORED_PATH_BYTES:
            return result(None, "unsafe")
    except (UnicodeError, ValueError):
        return result(None, "unsafe")
    if stored.is_absolute():
        return _resolve_existing_attachment(root, stored, basename_map)
    if ".." in stored.parts:
        return result(None, "unsafe")

    original_parts = stored.parts
    relative_parts = original_parts
    if relative_parts and relative_parts[0].casefold() == "attachments":
        relative_parts = relative_parts[1:]
    if not relative_parts:
        return result(None, "missing")

    direct, direct_state, basename_map = _resolve_existing_attachment(
        root, root.joinpath(*relative_parts), basename_map
    )
    if direct is not None or direct_state != "missing":
        return direct, direct_state, basename_map
    if not stored.name:
        return result(None, "missing")

    if basename_map is None:
        try:
            basename_map = _attachment_paths_by_basename(root)
        except OSError:
            return result(None, "unavailable")
    matches = basename_map.get(stored.name, [])
    if len(matches) == 1:
        return result(matches[0], "available")
    return result(None, "ambiguous" if matches else "missing")


def _resolve_existing_attachment(
    root: Path,
    candidate: Path,
    basename_map: dict[str, list[Path]] | None,
) -> tuple[Path | None, str, dict[str, list[Path]] | None]:
    """Resolve a regular file without traversing a symlink in the copied tree."""
    try:
        relative = candidate.relative_to(root)
    except ValueError:
        return None, "unsafe", basename_map
    if any(part in {"", ".", ".."} for part in relative.parts):
        return None, "unsafe", basename_map

    current = root
    for index, part in enumerate(relative.parts):
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return None, "missing", basename_map
        except ValueError:
            return None, "unsafe", basename_map
        except OSError:
            return None, "unavailable", basename_map
        if stat.S_ISLNK(info.st_mode):
            return None, "unsafe", basename_map
        is_last = index == len(relative.parts) - 1
        if is_last:
            if not stat.S_ISREG(info.st_mode):
                return None, "unsafe", basename_map
        elif not stat.S_ISDIR(info.st_mode):
            return None, "unsafe", basename_map
    if not relative.parts:
        return None, "unsafe", basename_map
    return current, "available", basename_map


def _attachment_paths_by_basename(root: Path) -> dict[str, list[Path]]:
    paths: dict[str, list[Path]] = {}
    scanned = 0

    def fail_walk(exc: OSError) -> None:
        raise exc

    for parent, directories, files in os.walk(root, followlinks=False, onerror=fail_walk):
        scanned += len(directories) + len(files)
        if scanned > _MAX_ATTACHMENT_SCAN_ENTRIES:
            raise OSError("The copied attachment directory exceeds the safe path scan limit")
        directories[:] = [name for name in directories if not (Path(parent) / name).is_symlink()]
        for name in files:
            candidate = Path(parent) / name
            resolved, state, _ = _resolve_existing_attachment(root, candidate, None)
            if state == "available" and resolved is not None:
                paths.setdefault(candidate.name, []).append(resolved)
    return paths


def _fingerprint_path(
    root: Path,
    raw_path: str,
    *,
    basename_map: dict[str, list[Path]] | None,
) -> tuple[str, tuple[int, int, int, int, int, int] | None, dict[str, list[Path]] | None]:
    first, state, basename_map = _resolve_attachment_path(root, raw_path, basename_map=basename_map)
    if first is None:
        second, second_state, basename_map = _resolve_attachment_path(
            root, raw_path, basename_map=basename_map
        )
        if second is not None or second_state != state:
            raise _native_failure(
                "An attachment path changed while the native fingerprint was collected.",
                operation="fingerprint_native_attachments",
            )
        return state, None, basename_map
    try:
        before = stat_key(first.stat())
    except OSError:
        return "unavailable", None, basename_map
    second, second_state, basename_map = _resolve_attachment_path(
        root, raw_path, basename_map=basename_map
    )
    if second_state != "available" or second != first:
        raise _native_failure(
            "An attachment path changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        )
    try:
        after = stat_key(second.stat())
    except OSError as exc:
        raise _native_failure(
            "An attachment changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        ) from exc
    if before != after:
        raise _native_failure(
            "An attachment changed while the native fingerprint was collected.",
            operation="fingerprint_native_attachments",
        )
    return "available", before, basename_map


def _read_stable_image_file(root: Path, path: Path) -> tuple[bytes, tuple[int, ...]]:
    try:
        resolved_before = resolve_confined_file(root, path)
        if resolved_before is None or resolved_before != path:
            raise GoodnotesError("The copied image path is unavailable.")
        before = stat_key(path.lstat())
        if not stat.S_ISREG(before[2]):
            raise GoodnotesError("The copied image is not a regular file.")
        if before[3] <= 0 or before[3] > _MAX_IMAGE_BYTES:
            raise GoodnotesError("The copied image exceeds the compressed image size limit.")
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0),
        )
    except (GoodnotesError, OSError, RuntimeError, ValueError) as exc:
        raise _native_failure(
            "The copied image is missing, unsafe, or exceeds its size limit.",
            operation="read_native_image",
        ) from exc

    try:
        opened_signature = stat_key(os.fstat(descriptor))
        if opened_signature != before or not stat.S_ISREG(opened_signature[2]):
            raise GoodnotesError("The copied image changed before it could be read.")
        chunks: list[bytes] = []
        total = 0
        while total <= _MAX_IMAGE_BYTES:
            chunk = os.read(
                descriptor,
                min(_IMAGE_READ_CHUNK, _MAX_IMAGE_BYTES + 1 - total),
            )
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        if len(data) != before[3] or len(data) > _MAX_IMAGE_BYTES:
            raise GoodnotesError("The copied image changed or exceeds its size limit.")
        if stat_key(os.fstat(descriptor)) != before or stat_key(path.lstat()) != before:
            raise GoodnotesError("The copied image changed while it was read.")
        resolved_after = resolve_confined_file(root, path)
        if resolved_after != path:
            raise GoodnotesError("The copied image path changed while it was read.")
        return data, before
    except (GoodnotesError, OSError, RuntimeError, ValueError) as exc:
        raise _native_failure(
            "The copied image changed or could not be read safely.",
            operation="read_native_image",
        ) from exc
    finally:
        os.close(descriptor)


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


def _metadata_digest(values: tuple[object, ...]) -> str:
    def safe_value(value: object) -> object:
        if value is None or isinstance(value, (str, int, bool)):
            return value
        if isinstance(value, float):
            return value if math.isfinite(value) else str(value)
        if isinstance(value, bytes):
            return ("blob", len(value), hashlib.sha256(value).hexdigest())
        return (type(value).__name__, str(value))

    encoded = json.dumps(
        tuple(safe_value(value) for value in values),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _native_failure(message: str, *, operation: str) -> NativeError:
    return NativeError(
        "native_data_unavailable",
        message,
        operation=operation,
    )
