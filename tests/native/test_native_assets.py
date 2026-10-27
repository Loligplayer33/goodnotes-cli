from __future__ import annotations

import base64
import json
import sqlite3
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path

import pymupdf
import pytest

import goodnotes_cli.native.assets as native_assets
from goodnotes_cli.native import NativeError


@dataclass(frozen=True)
class FakeSandbox:
    root: Path
    library_dir: Path
    attachments_dir: Path
    projection_path: Path


def _sandbox(
    root: Path,
    *,
    schema: str = "full",
    rows: tuple[tuple[object, ...], ...] = (),
) -> FakeSandbox:
    library_dir = root / "Library"
    attachments_dir = library_dir / "Attachments"
    projection_path = library_dir / "Databases" / "projection.sqlite"
    attachments_dir.mkdir(parents=True)
    projection_path.parent.mkdir(parents=True)
    if schema == "full":
        table = "CREATE TABLE attachments (id TEXT, path TEXT, available INTEGER, document_id TEXT)"
    elif schema == "legacy":
        table = "CREATE TABLE attachments (id TEXT, path TEXT)"
    else:
        table = None
    with sqlite3.connect(projection_path) as connection:
        if table is not None:
            connection.execute(table)
            if schema == "full" and rows:
                connection.executemany(
                    "INSERT INTO attachments(id, path, available, document_id) VALUES (?, ?, ?, ?)",
                    rows,
                )
            if schema == "legacy" and rows:
                connection.executemany(
                    "INSERT INTO attachments(id, path) VALUES (?, ?)",
                    [(row[0], row[1]) for row in rows],
                )
    return FakeSandbox(root, library_dir, attachments_dir, projection_path)


def _png(width: int = 2, height: int = 3) -> bytes:
    row = b"\x00" + bytes((20, 40, 60)) * width
    raw = row * height
    return _png_with_raw(raw, width=width, height=height)


def _png_with_raw(
    raw: bytes,
    *,
    width: int = 2,
    height: int = 3,
    bit_depth: int = 8,
    color_type: int = 2,
    interlace: int = 0,
) -> bytes:

    def chunk(kind: bytes, content: bytes) -> bytes:
        checksum = zlib.crc32(kind + content) & 0xFFFFFFFF
        return struct.pack(">I", len(content)) + kind + content + struct.pack(">I", checksum)

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(
            b"IHDR",
            struct.pack(">IIBBBBB", width, height, bit_depth, color_type, 0, 0, interlace),
        )
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _jpeg(width: int = 3, height: int = 2) -> bytes:
    pixmap = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, width, height), False)
    return pixmap.tobytes("jpeg")


def _baseline_jpeg() -> bytes:
    return base64.b64decode(
        "/9j/2wBDAAgEBAQEBAUFBQUFBQYGBgYGBgYGBgYGBgYHBwcICAgHBwcGBgcHCAgICAkJCQgICAgJCQoKCgwMCwsODg4RERT/xABLAAEBAAAAAAAAAAAAAAAAAAAACAEBAAAAAAAAAAAAAAAAAAAAABABAAAAAAAAAAAAAAAAAAAAABEBAAAAAAAAAAAAAAAAAAAAAP/AABEIACgAMgMBEgACEgADEgD/2gAMAwEAAhEDEQA/AJ/AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAB//Z"
    )


def _insert(sandbox: FakeSandbox, *rows: tuple[object, ...]) -> None:
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.executemany(
            "INSERT INTO attachments(id, path, available, document_id) VALUES (?, ?, ?, ?)",
            rows,
        )


def test_loads_verified_png_and_jpeg_with_dimensions_and_content_mime(tmp_path: Path) -> None:
    png_bytes = _png()
    jpeg_bytes = _jpeg()
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("asset-png", "attachments/photo.jpg", 1, "doc-a"),
            ("asset-jpeg", "photo.png", 1, "doc-a"),
        ),
    )
    (sandbox.attachments_dir / "photo.jpg").write_bytes(png_bytes)
    (sandbox.attachments_dir / "photo.png").write_bytes(jpeg_bytes)

    png = native_assets.load_native_image(sandbox, "doc-a", "asset-png")
    jpeg = native_assets.load_native_image(sandbox, "doc-a", "asset-jpeg")

    assert (png.data, png.mime, png.width, png.height) == (png_bytes, "image/png", 2, 3)
    assert png.integrity == "scanlines-checked"
    assert (jpeg.data, jpeg.mime, jpeg.width, jpeg.height) == (
        jpeg_bytes,
        "image/jpeg",
        3,
        2,
    )
    assert jpeg.integrity == "decoder-unverified"


def test_fingerprint_is_immutable_json_serializable_and_tracks_binding_and_file_stats(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset-a", "nested/photo.png", 1, "doc-a"),),
    )
    image_path = sandbox.attachments_dir / "nested" / "photo.png"
    image_path.parent.mkdir()
    image_path.write_bytes(_png())

    original = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    assert isinstance(original, tuple)
    assert isinstance(json.dumps(original), str)

    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute("UPDATE attachments SET available = 0 WHERE id = 'asset-a'")
    binding_changed = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    assert binding_changed != original

    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute("UPDATE attachments SET available = 1 WHERE id = 'asset-a'")
    restored = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    assert restored != original  # The projection identity changed as well.

    image_path.write_bytes(_png(3, 2))
    file_changed = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    assert file_changed != restored


def test_fingerprint_keeps_missing_unsafe_and_unavailable_unused_assets_representable(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("missing", "not-here.png", 1, "doc-a"),
            ("unsafe", "../outside.png", 1, "doc-a"),
            ("unavailable", "not-here-too.png", 0, "doc-a"),
        ),
    )

    fingerprint = native_assets.native_attachment_fingerprint(sandbox, "doc-a")

    assert isinstance(fingerprint, tuple)
    assert len(fingerprint) == 6


def test_fingerprint_serializes_blob_metadata_without_reading_image_contents(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.execute(
            "INSERT INTO attachments(id, path, available, document_id) VALUES (?, ?, ?, ?)",
            ("asset", sqlite3.Binary(b"not-a-path"), 1, "doc-a"),
        )

    fingerprint = native_assets.native_attachment_fingerprint(sandbox, "doc-a")

    assert isinstance(json.dumps(fingerprint), str)
    bindings = fingerprint[-1]
    assert isinstance(bindings, tuple)
    first_binding = bindings[0]
    assert isinstance(first_binding, tuple)
    assert first_binding[1] == "unavailable"
    with pytest.raises(NativeError, match="path"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_fingerprint_distinguishes_missing_and_invalid_attachment_paths(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("blob", sqlite3.Binary(b"not-a-path"), 1, "doc-a"),
            ("missing", None, 1, "doc-a"),
            ("nul", "bad\x00path", 1, "doc-a"),
        ),
    )

    fingerprint = native_assets.native_attachment_fingerprint(sandbox, "doc-a")

    bindings = fingerprint[-1]
    assert isinstance(bindings, tuple)
    states = []
    for binding in bindings:
        assert isinstance(binding, tuple)
        states.append(binding[1])
    assert states == ["unavailable", "missing", "unsafe"]


def test_fingerprint_stops_after_the_attachment_row_limit(tmp_path: Path) -> None:
    sandbox = _sandbox(tmp_path / "snapshot")
    with sqlite3.connect(sandbox.projection_path) as connection:
        connection.executemany(
            "INSERT INTO attachments(id, path, available, document_id) VALUES (?, ?, ?, ?)",
            ((f"asset-{index}", f"image-{index}.png", 1, "doc-a") for index in range(10_001)),
        )

    with pytest.raises(NativeError, match="too many"):
        native_assets.native_attachment_fingerprint(sandbox, "doc-a")


@pytest.mark.parametrize("stored_path", ["alias.png", "linked/photo.png"])
def test_loader_rejects_internal_symlink_paths(tmp_path: Path, stored_path: str) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", stored_path, 1, "doc-a"),),
    )
    (sandbox.attachments_dir / "real.png").write_bytes(_png())
    (sandbox.attachments_dir / "alias.png").symlink_to(sandbox.attachments_dir / "real.png")
    linked = sandbox.attachments_dir / "linked"
    linked.symlink_to(sandbox.attachments_dir, target_is_directory=True)

    with pytest.raises(NativeError, match="unsafe"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")

    fingerprint = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    bindings = fingerprint[-1]
    assert isinstance(bindings, tuple)
    first_binding = bindings[0]
    assert isinstance(first_binding, tuple)
    assert first_binding[1] == "unsafe"


def test_basename_fallback_stops_when_its_inventory_limit_is_reached(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "missing/photo.png", 1, "doc-a"),),
    )
    (sandbox.attachments_dir / "nested").mkdir()
    (sandbox.attachments_dir / "nested" / "other.png").write_bytes(_png())
    (sandbox.attachments_dir / "second.png").write_bytes(_png())
    monkeypatch.setattr(native_assets, "_MAX_ATTACHMENT_SCAN_ENTRIES", 2)

    with pytest.raises(NativeError, match="could not be inspected"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_loader_rejects_bad_png_crc_and_jpeg_without_a_decodable_scan(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("bad-png", "bad.png", 1, "doc-a"),
            ("bad-jpeg", "bad.jpg", 1, "doc-a"),
            ("empty-png", "empty.png", 1, "doc-a"),
            ("short-png", "short.png", 1, "doc-a"),
            ("bad-filter", "filter.png", 1, "doc-a"),
        ),
    )
    png = bytearray(_png())
    png[-5] ^= 0xFF
    (sandbox.attachments_dir / "bad.png").write_bytes(png)
    # A frame header and EOI marker do not constitute a decodable JPEG image.
    (sandbox.attachments_dir / "bad.jpg").write_bytes(
        bytes.fromhex("ffd8ffc00011080002000303011100021100031100ffd9")
    )
    (sandbox.attachments_dir / "empty.png").write_bytes(_png_with_raw(b""))
    (sandbox.attachments_dir / "short.png").write_bytes(_png_with_raw(b"\x00" + b"\x00" * 5))
    (sandbox.attachments_dir / "filter.png").write_bytes(
        _png_with_raw(b"\x05" + b"\x00" * 6 + (b"\x00" + b"\x00" * 6) * 2)
    )

    with pytest.raises(NativeError, match="invalid|incomplete|decoded|decode"):
        native_assets.load_native_image(sandbox, "doc-a", "bad-png")
    with pytest.raises(NativeError, match="invalid|incomplete|decoded|decode"):
        native_assets.load_native_image(sandbox, "doc-a", "bad-jpeg")
    for attachment_id in ("empty-png", "short-png", "bad-filter"):
        with pytest.raises(NativeError, match="invalid|incomplete|decoded|decode"):
            native_assets.load_native_image(sandbox, "doc-a", attachment_id)


@pytest.mark.parametrize("scan_bytes", [0, 1, 5, 10])
def test_loader_rejects_truncated_progressive_jpeg_scans(tmp_path: Path, scan_bytes: int) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "truncated.jpg", 1, "doc-a"),),
    )
    jpeg = _jpeg(50, 40)
    scan_marker = jpeg.index(b"\xff\xda")
    scan_length = int.from_bytes(jpeg[scan_marker + 2 : scan_marker + 4], "big")
    scan_start = scan_marker + 2 + scan_length
    truncated = jpeg[: scan_start + scan_bytes] + b"\xff\xd9"
    (sandbox.attachments_dir / "truncated.jpg").write_bytes(truncated)

    with pytest.raises(NativeError, match="invalid|incomplete|decoded|decode"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_loader_accepts_complete_baseline_jpeg_as_decoder_unverified(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "baseline.jpg", 1, "doc-a"),),
    )
    (sandbox.attachments_dir / "baseline.jpg").write_bytes(_baseline_jpeg())

    image = native_assets.load_native_image(sandbox, "doc-a", "asset")

    assert (image.mime, image.width, image.height) == ("image/jpeg", 50, 40)
    assert image.integrity == "decoder-unverified"


def test_loader_accepts_adam7_png_with_exact_pass_scanlines(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "interlaced.png", 1, "doc-a"),),
    )
    # Adam7 passes contain one top-left pixel, one top-row pixel, then the bottom row.
    interlaced = _png_with_raw(
        b"\x00\x10\x00\x20\x00\x30\x40",
        width=2,
        height=2,
        color_type=0,
        interlace=1,
    )
    (sandbox.attachments_dir / "interlaced.png").write_bytes(interlaced)

    image = native_assets.load_native_image(sandbox, "doc-a", "asset")

    assert (image.mime, image.width, image.height) == ("image/png", 2, 2)
    assert image.integrity == "scanlines-checked"


@pytest.mark.parametrize(
    ("schema", "rows", "attachment_id", "document_id", "message"),
    [
        ("full", (("asset", "image.png", 1, "doc-other"),), "asset", "doc-a", "unique"),
        ("full", (("asset", "image.png", 0, "doc-a"),), "asset", "doc-a", "unavailable"),
        (
            "full",
            (("asset", "one.png", 1, "doc-a"), ("asset", "two.png", 1, "doc-a")),
            "asset",
            "doc-a",
            "unique",
        ),
        ("legacy", (("asset", "image.png"),), "asset", "doc-a", "binding"),
        ("none", (), "asset", "doc-a", "binding"),
    ],
)
def test_loader_requires_one_available_document_binding(
    tmp_path: Path,
    schema: str,
    rows: tuple[tuple[object, ...], ...],
    attachment_id: str,
    document_id: str,
    message: str,
) -> None:
    sandbox = _sandbox(tmp_path / "snapshot", schema=schema, rows=rows)

    with pytest.raises(NativeError, match=message):
        native_assets.load_native_image(sandbox, document_id, attachment_id)


def test_missing_or_incomplete_attachment_schema_has_a_stable_fingerprint(
    tmp_path: Path,
) -> None:
    sandbox = _sandbox(tmp_path / "snapshot", schema="legacy")

    first = native_assets.native_attachment_fingerprint(sandbox, "doc-a")
    second = native_assets.native_attachment_fingerprint(sandbox, "doc-a")

    assert first == second
    schema_state = first[4]
    assert isinstance(schema_state, tuple)
    assert schema_state[0] == "schema-unavailable"
    with pytest.raises(NativeError, match="binding"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


@pytest.mark.parametrize("unsafe_path", ["../outside.png", "/tmp/outside.png"])
def test_loader_rejects_attachment_paths_outside_the_copied_folder(
    tmp_path: Path, unsafe_path: str
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", unsafe_path, 1, "doc-a"),),
    )

    with pytest.raises(NativeError, match="unsafe|outside"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_loader_rejects_symlink_escape_and_nonregular_attachment(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("symlink", "outside.png", 1, "doc-a"),
            ("directory", "folder", 1, "doc-a"),
        ),
    )
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png())
    (sandbox.attachments_dir / "outside.png").symlink_to(outside)
    (sandbox.attachments_dir / "folder").mkdir()

    with pytest.raises(NativeError, match="unsafe|outside"):
        native_assets.load_native_image(sandbox, "doc-a", "symlink")
    with pytest.raises(NativeError, match="missing|unsafe|outside|size limit"):
        native_assets.load_native_image(sandbox, "doc-a", "directory")


def test_loader_enforces_compressed_byte_and_decoded_pixel_limits(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("oversized-bytes", "large.bin", 1, "doc-a"),
            ("oversized-pixels", "large.png", 1, "doc-a"),
        ),
    )
    (sandbox.attachments_dir / "large.bin").write_bytes(b"x" * (16 * 1024 * 1024 + 1))

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    oversized_header = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 5001, 5000, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b""))
        + chunk(b"IEND", b"")
    )
    (sandbox.attachments_dir / "large.png").write_bytes(oversized_header)

    with pytest.raises(NativeError, match="size limit"):
        native_assets.load_native_image(sandbox, "doc-a", "oversized-bytes")
    with pytest.raises(NativeError, match="pixel limit"):
        native_assets.load_native_image(sandbox, "doc-a", "oversized-pixels")


def test_loader_rejects_nonimage_and_truncated_image_data(tmp_path: Path) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(
            ("pdf", "drawing.pdf", 1, "doc-a"),
            ("truncated", "truncated.png", 1, "doc-a"),
        ),
    )
    (sandbox.attachments_dir / "drawing.pdf").write_bytes(b"%PDF-1.7\n")
    (sandbox.attachments_dir / "truncated.png").write_bytes(_png()[:40])

    with pytest.raises(NativeError, match="PNG or JPEG"):
        native_assets.load_native_image(sandbox, "doc-a", "pdf")
    with pytest.raises(NativeError, match="header|decoded|decode"):
        native_assets.load_native_image(sandbox, "doc-a", "truncated")


def test_loader_detects_file_tampering_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "image.png", 1, "doc-a"),),
    )
    image_path = sandbox.attachments_dir / "image.png"
    image_path.write_bytes(_png())
    original_read = native_assets.os.read
    modified = False

    def tampering_read(descriptor: int, size: int) -> bytes:
        nonlocal modified
        result = original_read(descriptor, size)
        if not modified:
            modified = True
            image_path.write_bytes(_png(4, 4))
        return result

    monkeypatch.setattr(native_assets.os, "read", tampering_read)

    with pytest.raises(NativeError, match="changed"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_loader_detects_projection_mutation_during_image_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "image.png", 1, "doc-a"),),
    )
    (sandbox.attachments_dir / "image.png").write_bytes(_png())
    original_read = native_assets._read_stable_image_file

    def mutate_binding(root: Path, path: Path) -> tuple[bytes, tuple[int, ...]]:
        result = original_read(root, path)
        with sqlite3.connect(sandbox.projection_path) as connection:
            connection.execute("UPDATE attachments SET available = 0 WHERE id = 'asset'")
        return result

    monkeypatch.setattr(native_assets, "_read_stable_image_file", mutate_binding)

    with pytest.raises(NativeError, match="projection changed"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")


def test_loader_rejects_symlink_replacement_before_opening_image(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _sandbox(
        tmp_path / "snapshot",
        rows=(("asset", "image.png", 1, "doc-a"),),
    )
    image_path = sandbox.attachments_dir / "image.png"
    image_path.write_bytes(_png())
    outside = tmp_path / "outside.png"
    outside.write_bytes(_png(4, 4))
    original_read = native_assets._read_stable_image_file
    attempted_read = False

    def replace_then_read(root: Path, path: Path) -> tuple[bytes, tuple[int, ...]]:
        path.unlink()
        path.symlink_to(outside)
        return original_read(root, path)

    original_os_read = native_assets.os.read

    def track_read(descriptor: int, size: int) -> bytes:
        nonlocal attempted_read
        attempted_read = True
        return original_os_read(descriptor, size)

    monkeypatch.setattr(native_assets, "_read_stable_image_file", replace_then_read)
    monkeypatch.setattr(native_assets.os, "read", track_read)

    with pytest.raises(NativeError, match="unsafe|outside|changed"):
        native_assets.load_native_image(sandbox, "doc-a", "asset")

    assert not attempted_read
    assert outside.read_bytes() == _png(4, 4)
