from __future__ import annotations

import math
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pymupdf as fitz
import pytest

from goodnotes_cli import annotations as annotation_module
from goodnotes_cli._vendor.compression import CompressionError, decode_apple_lz4
from goodnotes_cli._vendor.tpl import TplDecodeError, decode_tpl
from goodnotes_cli._vendor.wire import DecodeError, decode_message
from goodnotes_cli.annotations import decode_ink, decode_native_records, render_native_overlay
from goodnotes_cli.models import Page
from goodnotes_cli.native import NativeError

PAGE_ID = "00000000-0000-0000-0000-000000000100"
ROOT_ID = "00000000-0000-0000-0000-000000000101"
STORAGE_ID = "00000000-0000-0000-0000-000000000200"
LOGICAL_ID = "00000000-0000-0000-0000-000000000300"
FORMAT = "vuA(v)A(S(uu))A(S(uuuu))"


class _FixtureSnapshot:
    root = Path("/synthetic-snapshot")
    library_dir = root / "Library"


def varint(value: int) -> bytes:
    result = bytearray()
    while value >= 128:
        result.append((value & 127) | 128)
        value >>= 7
    result.append(value)
    return bytes(result)


def field(number: int, value: bytes | int | float) -> bytes:
    if isinstance(value, bytes):
        return varint(number * 8 + 2) + varint(len(value)) + value
    if isinstance(value, float):
        return varint(number * 8 + 5) + struct.pack("<f", value)
    return varint(number * 8) + varint(value)


def tpl(
    *,
    points: list[tuple[float, float]] | None = None,
    width: float = 4.0,
    flags: list[int] | None = None,
    format_string: str = FORMAT,
) -> bytes:
    points = points if points is not None else [(10.0, 20.0), (40.0, 20.0), (40.0, 50.0)]
    starts = points[:1]
    segments = list(zip(points, points[1:], strict=False))
    flags = flags if flags is not None else [1] * (len(segments) + 1)
    values = struct.pack("<HfI", 0, width, len(flags))
    values += b"".join(struct.pack("<H", value) for value in flags)
    values += struct.pack("<I", len(starts))
    values += b"".join(struct.pack("<ff", *point) for point in starts)
    values += struct.pack("<I", len(segments))
    values += b"".join(struct.pack("<ffff", *start, *end) for start, end in segments)
    if format_string.endswith("vA(f)"):
        values += struct.pack("<HI", 0, 0)
    contents = format_string.encode() + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def framed(data: bytes, *, compressed: bool = True) -> bytes:
    if not compressed:
        return b"bv4-" + struct.pack("<II", len(data), len(data)) + data + b"bv4$"
    remaining = max(0, len(data) - 15)
    extension = bytearray()
    if len(data) >= 15:
        while remaining >= 255:
            extension.append(255)
            remaining -= 255
        extension.append(remaining)
    block = bytes([min(len(data), 15) << 4]) + bytes(extension) + data
    return b"bv41" + struct.pack("<II", len(data), len(block)) + block + b"bv4$"


def native_records(
    *,
    storage_id: str = STORAGE_ID,
    logical_id: str = LOGICAL_ID,
    category: str = "normal",
    erased: bool = False,
    type_code: int = 24,
    actor: int = 1,
    sequence: int = 1,
    body: bytes | None = None,
    extra_descriptor: bytes = b"",
    storage_alias: bool = True,
) -> list[tuple[bytes, bytes]]:
    descriptor = (
        field(1, logical_id.encode())
        + (field(6, storage_id.encode()) if storage_alias else b"")
        + field(8, actor)
        + field(9, sequence)
        + field(16, type_code)
        + extra_descriptor
    )
    if erased:
        descriptor += field(3, 1)
    if body is None:
        body = field(1, logical_id.encode()) + field(2, framed(tpl()))
    prefix = f"{ROOT_ID}.{category}.{storage_id}.".encode()
    return [(prefix + b"descriptor", descriptor), (prefix + b"item", field(7, body))]


def page() -> Page:
    return Page(PAGE_ID, "synthetic-notebook", 1, "a", width=132, height=132)


def test_native_fixture_decodes_independently_known_geometry() -> None:
    inks, metadata = decode_native_records(native_records(), ROOT_ID)
    assert len(inks) == 1
    assert inks[0].points == ((10.0, 20.0), (40.0, 20.0), (40.0, 50.0))
    assert (inks[0].width, inks[0].color, inks[0].alpha) == (4.0, "#000000", 1.0)
    assert metadata["rendered_objects"] == metadata["rendered_strokes"] == 1
    assert metadata["coverage"] == "supported-pen-records"
    assert metadata["full_fidelity_verified"] is False


@pytest.mark.parametrize("opaque_metadata", [None, 0, 123, 2**64 - 1])
def test_direct_identity_variant_preserves_known_geometry(opaque_metadata: int | None) -> None:
    body = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl())) + field(20, b"")
    records = native_records(
        storage_id=LOGICAL_ID,
        storage_alias=False,
        body=body,
        extra_descriptor=field(14, opaque_metadata) if opaque_metadata is not None else b"",
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert len(inks) == 1
    assert inks[0].points == ((10.0, 20.0), (40.0, 20.0), (40.0, 50.0))
    assert metadata["descriptor_variants"] == {"direct-identity": 1}
    assert metadata["unmodeled_empty_body_fields"] == [20]
    assert metadata["unsupported_objects"] == 0
    assert metadata["coverage"] == (
        "partial" if opaque_metadata is not None else "supported-pen-records"
    )
    assert metadata["unmodeled_descriptor_fields"] == ([14] if opaque_metadata is not None else [])
    if opaque_metadata is not None:
        assert any(
            "opaque metadata" in str(warning) for warning in cast(list, metadata["warnings"])
        )


def test_direct_identity_without_alias_cannot_bind_another_storage_key() -> None:
    inks, metadata = decode_native_records(native_records(storage_alias=False), ROOT_ID)
    assert not inks and metadata["unsupported_objects"] == 1


def test_invalid_present_alias_never_falls_back_to_direct_identity() -> None:
    records = [
        (key.replace(STORAGE_ID.encode(), LOGICAL_ID.encode()), value)
        for key, value in native_records()
    ]
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert not inks and metadata["unsupported_objects"] == 1


def test_erased_direct_identity_variant_stays_excluded() -> None:
    records = native_records(
        storage_id=LOGICAL_ID, storage_alias=False, erased=True, extra_descriptor=field(14, 123)
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert not inks and metadata["erased_objects"] == 1
    assert metadata["unsupported_objects"] == 0
    assert metadata["coverage"] == "partial"


@pytest.mark.parametrize(
    "extra_metadata",
    [field(14, b"not-a-varint"), field(14, 1.0), field(14, 0) + field(14, 1), field(14, 2**64)],
)
def test_opaque_descriptor_metadata_requires_one_unsigned_varint(extra_metadata: bytes) -> None:
    records = native_records(
        storage_id=LOGICAL_ID, storage_alias=False, extra_descriptor=extra_metadata
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert not inks and metadata["unsupported_objects"] == 1


def test_invalid_direct_variant_does_not_restore_older_logical_version() -> None:
    records = native_records() + native_records(
        storage_id=LOGICAL_ID, storage_alias=False, extra_descriptor=field(14, b"invalid")
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert not inks
    reasons = cast(dict, metadata["unsupported_reasons"])
    assert reasons["ambiguous-logical-version"] == 1


@pytest.mark.parametrize("extra", [field(20, b"geometry"), field(20, 1), field(20, b"") * 2])
def test_auxiliary_body_field_twenty_is_empty_only(extra: bytes) -> None:
    body = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl())) + extra
    inks, metadata = decode_native_records(native_records(body=body), ROOT_ID)
    assert not inks and metadata["unsupported_objects"] == 1


def test_lasso_translation_and_rgba_survive_to_rendered_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(tpl()))
        + field(4, field(1, 1.0) + field(4, 0.5))
        + field(6, field(1, 10.0) + field(2, 5.0))
    )
    monkeypatch.setattr(
        annotation_module, "read_page_records", lambda *_: native_records(body=body)
    )
    overlay = render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert b"10.909091,13.636364" in overlay.svg
    assert b"#ff0000" in overlay.svg
    assert b"0.500000" in overlay.svg
    with fitz.open(stream=overlay.svg, filetype="svg") as document:
        pixmap = document[0].get_pixmap(dpi=132, alpha=False)
        red, green, blue = pixmap.pixel(30, 25)
    assert red >= 250
    assert 120 <= green <= 135 and 120 <= blue <= 135


@pytest.mark.parametrize("compressed", [False, True])
def test_both_apple_block_encodings_and_dot(compressed: bool) -> None:
    payload = framed(tpl(points=[(25.0, 35.0)]), compressed=compressed)
    body = decode_message(field(2, payload))
    ink = decode_ink(body)[0]
    assert ink.points == ((25.0, 35.0),)
    assert b"<circle" in annotation_module._svg([ink], 72, 72, 72 / 132)


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


@pytest.mark.parametrize(
    "variant",
    ["erased", "deleted", "unknown", "malformed", "missing", "identity", "schema", "parent"],
)
def test_nonrenderable_records_are_never_reported_as_complete(variant: str) -> None:
    kwargs: dict[str, Any] = {}
    if variant in {"erased", "deleted", "unknown"}:
        kwargs = {
            "erased": variant == "erased",
            "category": "deleted" if variant == "deleted" else "normal",
            "type_code": 99 if variant == "unknown" else 24,
        }
    if variant == "schema":
        kwargs["body"] = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl(format_string="z")))
    if variant == "parent":
        kwargs["extra_descriptor"] = field(20, STORAGE_ID.encode())
    records = native_records(**kwargs)
    if variant == "malformed":
        records[1] = (records[1][0], b"\x00")
    if variant == "missing":
        records.pop()
    if variant == "identity":
        records[1] = (records[1][0], field(7, field(1, STORAGE_ID.encode())))
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert not inks
    assert metadata["rendered_objects"] == metadata["rendered_strokes"] == 0
    if variant == "erased":
        assert metadata["erased_objects"] == 1
    elif variant == "deleted":
        assert metadata["deleted_objects"] == 1 and metadata["coverage"] == "partial"
    else:
        assert metadata["unsupported_objects"] == 1 and metadata["coverage"] == "partial"


def test_duplicate_logical_versions_are_excluded_even_if_one_is_erased() -> None:
    records = native_records() + native_records(storage_id=STORAGE_ID[:-1] + "1", erased=True)
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert inks == []
    assert metadata["unsupported_reasons"] == {"ambiguous-logical-version": 1}


def test_cross_actor_order_is_reported_as_partial() -> None:
    records = native_records() + native_records(
        storage_id=STORAGE_ID[:-1] + "1", logical_id=LOGICAL_ID[:-1] + "1", actor=2
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert len(inks) == 2
    assert metadata["coverage"] == "partial"
    assert any(
        "Cross-device drawing order" in warning for warning in cast(list[str], metadata["warnings"])
    )


@pytest.mark.parametrize(
    "edit",
    [{"rotation": 90}, {"width": None}, {"height": 0}, {"width": math.inf}, {"id": "invalid"}],
)
def test_invalid_page_mapping_fails_before_native_read(
    monkeypatch: pytest.MonkeyPatch, edit: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        annotation_module, "read_page_records", lambda *_: pytest.fail("opened native")
    )
    with pytest.raises(NativeError):
        render_native_overlay(
            _FixtureSnapshot(), replace(page(), **edit), width_points=72, height_points=72
        )


def test_missing_native_records_is_unknown_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(annotation_module, "read_page_records", lambda *_: [])
    with pytest.raises(NativeError, match="does not establish.*no ink") as result:
        render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert result.value.code == "native_data_unavailable"


def test_unknown_objects_only_fail_instead_of_rendering_a_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        annotation_module, "read_page_records", lambda *_: native_records(type_code=99)
    )
    with pytest.raises(NativeError, match="none could be rendered") as captured:
        render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    coverage = captured.value.details["native_annotations"]
    assert isinstance(coverage, dict)
    assert coverage["unsupported_objects"] == 1
    assert coverage["unsupported_reasons"] == {"unsupported-object-type": 1}


@pytest.mark.parametrize("malformed", [b"\x00", b"\x08" + b"\xff" * 10 + b"\x01", b"\x0a\xff"])
def test_protobuf_rejects_invalid_input(malformed: bytes) -> None:
    with pytest.raises(DecodeError):
        decode_message(malformed)


def test_format_decoders_have_expansion_and_nesting_budgets() -> None:
    with pytest.raises(CompressionError, match="budget"):
        decode_apple_lz4(b"bv41" + struct.pack("<II", 2**30, 1) + b"\0" + b"bv4$")
    nested = ("A(" * 40 + "u" + ")" * 40).encode()
    image = b"tpl\0" + struct.pack("<I", len(nested) + 9) + nested + b"\0"
    with pytest.raises(TplDecodeError, match="nesting"):
        decode_tpl(image)
    raw = b"A(u)\0" + struct.pack("<I", 2**32 - 1)
    with pytest.raises(TplDecodeError, match="count"):
        decode_tpl(b"tpl\0" + struct.pack("<I", len(raw) + 8) + raw)


def test_ink_rejects_unsupported_format_before_generic_tpl_decode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        annotation_module, "decode_tpl", lambda *_: pytest.fail("unsupported generic parse")
    )
    body = decode_message(field(2, framed(tpl(format_string="A(A(u))"))))
    with pytest.raises(ValueError, match="Unsupported ink TPL"):
        decode_ink(body)


def test_aggregate_point_budget_is_enforced_across_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    budget_class = annotation_module._DecodeBudget
    monkeypatch.setattr(annotation_module, "_DecodeBudget", lambda: budget_class(points_left=6))
    records = native_records() + native_records(
        storage_id=STORAGE_ID[:-1] + "1", logical_id=LOGICAL_ID[:-1] + "1"
    )
    with pytest.raises(NativeError, match="point budget"):
        decode_native_records(records, ROOT_ID)


def test_long_native_segment_is_preserved_instead_of_split_into_dots() -> None:
    body = decode_message(field(2, framed(tpl(points=[(10.0, 20.0), (500.0, 20.0)]))))
    inks = decode_ink(body)
    assert len(inks) == 1 and inks[0].points == ((10.0, 20.0), (500.0, 20.0))


def test_lz4_zero_progress_block_is_rejected() -> None:
    with pytest.raises(CompressionError, match="no output progress"):
        decode_apple_lz4(b"bv4-" + struct.pack("<II", 0, 0) + b"bv4$")


@pytest.mark.parametrize(
    "extra",
    [
        field(99, 1),
        field(9, b"future-geometry"),
        field(5, 2),
        field(21, 35),
        field(6, field(3, 1.0)),
    ],
)
def test_unknown_style_transform_or_geometry_is_reported_unsupported(extra: bytes) -> None:
    body = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl())) + extra
    inks, metadata = decode_native_records(native_records(body=body), ROOT_ID)
    assert not inks
    assert metadata["coverage"] == "partial"
    assert metadata["unsupported_objects"] == 1


def test_tied_creation_clocks_warn_about_drawing_order() -> None:
    records = native_records() + native_records(
        storage_id=STORAGE_ID[:-1] + "1", logical_id=LOGICAL_ID[:-1] + "1"
    )
    inks, metadata = decode_native_records(records, ROOT_ID)
    assert len(inks) == 2 and metadata["coverage"] == "partial"
    assert any(
        "creation clocks are equal" in warning for warning in cast(list[str], metadata["warnings"])
    )
