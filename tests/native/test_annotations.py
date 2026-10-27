from __future__ import annotations

import math
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pymupdf as fitz
import pytest

from goodnotes_cli._vendor.compression import CompressionError, decode_apple_lz4
from goodnotes_cli._vendor.tpl import TplDecodeError, decode_tpl
from goodnotes_cli._vendor.wire import DecodeError, decode_message
from goodnotes_cli.annotations import (
    Ink,
    NativeImage,
    NativePath,
    decode_ink,
    decode_native_objects,
    decode_native_records,
    render_native_overlay,
)
from goodnotes_cli.models import Page
from goodnotes_cli.native import NativeError
from goodnotes_cli.native import ink as ink_module
from goodnotes_cli.native import objects as object_module
from goodnotes_cli.native import paths as path_module
from goodnotes_cli.native import records as records_module
from goodnotes_cli.native import values as values_module
from goodnotes_cli.rendering import ink_svg as svg_module
from goodnotes_cli.rendering import overlay as overlay_module
from tests.support.native_oracle import (
    ATTACHMENT_ID,
    FORMAT,
    LOGICAL_ID,
    PAGE_ID,
    RESOURCE_ID,
    ROOT_ID,
    STORAGE_ID,
    field,
    framed,
    native_image_records,
    native_path_body,
    native_path_tpl,
    native_records,
    rgba_message,
    tpl,
    variable_brush_tpl,
    varint,
)


class _FixtureSnapshot:
    root = Path("/synthetic-snapshot")
    library_dir = root / "Library"
    projection_path = library_dir / "Databases" / "projection.sqlite"
    attachments_dir = library_dir / "Attachments"


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
    monkeypatch.setattr(overlay_module, "read_page_records", lambda *_: native_records(body=body))
    overlay = render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert b"10.909091,13.636364" in overlay.svg
    assert b"#ff0000" in overlay.svg
    assert b"0.500000" in overlay.svg
    with fitz.open(stream=overlay.svg, filetype="svg") as document:
        pixmap = document[0].get_pixmap(dpi=132, alpha=False)
        red, green, blue = pixmap.pixel(30, 25)
    assert red >= 250
    assert 120 <= green <= 135 and 120 <= blue <= 135


def test_native_overlay_accepts_a_session_record_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def read_from_session(root_uuid: str) -> list[tuple[bytes, bytes]]:
        calls.append(root_uuid)
        return native_records()

    monkeypatch.setattr(
        overlay_module, "read_page_records", lambda *_: pytest.fail("opened one-shot store")
    )
    overlay = render_native_overlay(
        _FixtureSnapshot(),
        page(),
        width_points=72,
        height_points=72,
        record_reader=read_from_session,
    )
    assert calls == [ROOT_ID]
    assert overlay.metadata["rendered_strokes"] == 1


@pytest.mark.parametrize("compressed", [False, True])
def test_both_apple_block_encodings_and_dot(compressed: bool) -> None:
    payload = framed(tpl(points=[(25.0, 35.0)]), compressed=compressed)
    body = decode_message(field(2, payload))
    ink = decode_ink(body)[0]
    assert ink.points == ((25.0, 35.0),)
    assert b"<circle" in svg_module._svg([ink], 72, 72, 72 / 132)


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


def test_exact_start_to_segment_matches_do_not_guess_multi_start_paths() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (80.0, 90.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_multiple_start_dots_are_not_joined() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (80.0, 90.0)],
                    segments=[],
                )
            ),
        )
    )
    inks = decode_ink(body)
    svg = svg_module._svg(inks, 72, 72, 72 / 132)
    assert [ink.points for ink in inks] == [((10.0, 20.0),), ((80.0, 90.0),)]
    assert svg.count(b"<circle") == 2 and b"<polyline" not in svg


def test_unmatched_multiple_starts_with_segments_are_rejected() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(5.0, 6.0), (7.0, 8.0)],
                    segments=[((10.0, 20.0), (40.0, 20.0))],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_single_start_keeps_ordered_segment_samples_in_one_stroke() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((44.0, 20.0), (70.0, 20.0)),
                    ],
                )
            ),
        )
    )
    inks = decode_ink(body)
    assert len(inks) == 1
    assert inks[0].points == (
        (10.0, 20.0),
        (40.0, 20.0),
        (44.0, 20.0),
        (70.0, 20.0),
    )


def test_single_start_preserves_explicitly_ordered_retraced_segments() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((40.0, 20.0), (50.0, 20.0)),
                        ((50.0, 20.0), (40.0, 20.0)),
                        ((40.0, 20.0), (40.0, 30.0)),
                    ],
                )
            ),
        )
    )
    inks = decode_ink(body)
    assert len(inks) == 1
    assert inks[0].points == (
        (10.0, 20.0),
        (40.0, 20.0),
        (50.0, 20.0),
        (40.0, 20.0),
        (40.0, 30.0),
    )


def test_multiple_segments_without_start_markers_are_rejected_as_ambiguous() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="segments without start markers"):
        decode_ink(body)


def test_conflicting_start_markers_with_segments_are_rejected() -> None:
    body = decode_message(
        field(
            2,
            framed(
                tpl(
                    starts=[(10.0, 20.0), (10.0005, 20.0)],
                    segments=[
                        ((10.0, 20.0), (40.0, 20.0)),
                        ((80.0, 90.0), (100.0, 90.0)),
                    ],
                )
            ),
        )
    )
    with pytest.raises(ValueError, match="Ambiguous native ink starts and segments"):
        decode_ink(body)


def test_nonempty_native_dash_pattern_is_preserved() -> None:
    body = decode_message(field(2, framed(tpl(format_string=FORMAT + "vA(f)", dash=(4.5, 2.25)))))
    ink = decode_ink(body)[0]
    assert ink.dash == (4.5, 2.25)
    assert b'stroke-dasharray="2.454545 1.227273"' in svg_module._svg([ink], 72, 72, 72 / 132)


@pytest.mark.parametrize("dash", [(-1.0, 2.0), (math.nan, 2.0)])
def test_invalid_nonempty_native_dash_pattern_is_rejected(dash: tuple[float, ...]) -> None:
    body = decode_message(field(2, framed(tpl(format_string=FORMAT + "vA(f)", dash=dash))))
    with pytest.raises(ValueError, match="Unsupported native dash pattern"):
        decode_ink(body)


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
        overlay_module, "read_page_records", lambda *_: pytest.fail("opened native")
    )
    with pytest.raises(NativeError):
        render_native_overlay(
            _FixtureSnapshot(), replace(page(), **edit), width_points=72, height_points=72
        )


def test_successful_empty_native_lookup_renders_with_unverified_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(overlay_module, "read_page_records", lambda *_: [])
    result = render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert result.svg == b""
    assert result.metadata["annotation_status"] == "native-no-records-observed"
    assert result.metadata["coverage"] == "no-native-records-observed"
    assert result.metadata["native_records"] == result.metadata["rendered_objects"] == 0
    assert result.metadata["full_fidelity_verified"] is False
    assert "absence of annotations in Goodnotes is unverified" in str(result.metadata["warnings"])


@pytest.mark.parametrize("operation", ["read_native_copy", "inspect_native_store"])
def test_failed_native_read_never_becomes_an_empty_overlay(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    expected = NativeError(
        "native_data_unavailable", "Generated read failure.", operation=operation
    )

    def fail(*_args: Any) -> Any:
        raise expected

    monkeypatch.setattr(overlay_module, "read_page_records", fail)
    with pytest.raises(NativeError) as result:
        render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert result.value is expected


def test_unknown_objects_only_fail_instead_of_rendering_a_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        overlay_module, "read_page_records", lambda *_: native_records(type_code=99)
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
        values_module, "decode_tpl", lambda *_: pytest.fail("unsupported generic parse")
    )
    body = decode_message(field(2, framed(tpl(format_string="A(A(u))"))))
    with pytest.raises(ValueError, match="Unsupported ink TPL"):
        decode_ink(body)


def test_aggregate_point_budget_is_enforced_across_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    budget_class = records_module._DecodeBudget
    monkeypatch.setattr(object_module, "_DecodeBudget", lambda: budget_class(points_left=6))
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


def _decoded_path(payload: bytes, *, ellipse: bool = False) -> NativePath:
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    assert len(objects) == 1
    assert isinstance(objects[0].payload, NativePath)
    return objects[0].payload


def test_native_circular_path_preserves_cubic_controls_arc_and_closed_subpaths() -> None:
    path = _decoded_path(
        native_path_tpl(
            command_groups=[
                [(0, (12, 10)), (3, (10, 10, 2, 0, math.pi / 2)), (2, (9, 13, 8, 11, 12, 10))],
                [(0, (30, 40)), (2, (31, 41, 32, 42, 30, 40))],
            ]
        )
    )
    assert [command.op for command in path.commands] == ["M", "A", "C", "Z", "M", "C", "Z"]
    assert path.commands[1].values == pytest.approx((2, 2, 0, 1, 0, 10, 12), abs=1e-6)
    assert path.commands[2].values == (9, 13, 8, 11, 12, 10)
    assert path.fill_color == "#4080bf"
    assert path.fill_alpha == 1
    assert path.stroke_color is None


def test_native_elliptical_arc_uses_distinct_radii_and_its_observed_metadata() -> None:
    path = _decoded_path(
        native_path_tpl(
            ellipse=True,
            command_groups=[
                [(0, (24, 30)), (6, (20, 30, 4, 2, 0, math.pi / 2, 0))],
            ],
        ),
        ellipse=True,
    )
    assert path.commands[1].values == pytest.approx((4, 2, 0, 1, 0, 20, 32), abs=1e-6)
    assert path.commands[-1].op == "Z"


@pytest.mark.parametrize("ellipse", [False, True])
def test_native_path_metadata_cannot_cross_schema_variants(ellipse: bool) -> None:
    payload = native_path_tpl(ellipse=ellipse, command_groups=[[(0, (20, 30))]])
    body = native_path_body(payload, metadata=b"" if ellipse else b"\x0a\x00")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert objects == []
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize("end_angle", [0.0, math.tau, -math.tau])
def test_native_full_revolution_is_rejected_instead_of_becoming_an_empty_svg_arc(
    end_angle: float,
) -> None:
    payload = native_path_tpl(
        command_groups=[
            [(0, (12, 10)), (3, (10, 10, 2, 0, end_angle))],
        ]
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize(
    "commands",
    [
        [(2, (1, 2, 3, 4, 5, 6))],
        [(0, (1, 2)), (0, (3, 4))],
        [(0, (1, 2)), (8, ())],
        [(0, (1, 2)), (2, (3, 4, 5, 6))],
        [(0, (1, 2)), (3, (1, 2, -1, 0, 1))],
        [(0, (math.nan, 2))],
        [(0, (100001, 2))],
    ],
)
def test_malformed_native_path_never_returns_a_partial_prefix(
    commands: list[tuple[int, tuple[float, ...]]],
) -> None:
    payload = native_path_tpl(command_groups=[commands])
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["rendered_objects"] == 0
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize("flags,directions", [([0, 0], [1]), ([0, 1], [0]), ([0, 1], [])])
def test_native_path_rejects_unknown_visibility_and_arc_direction(
    flags: list[int],
    directions: list[int],
) -> None:
    payload = native_path_tpl(
        command_groups=[[(0, (12, 10)), (3, (10, 10, 2, 0, 1))]],
        flags=flags,
        arc_directions=directions,
    )
    objects, metadata = decode_native_objects(
        native_records(body=native_path_body(payload)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_native_path_point_budget_counts_controls_before_returning_any_geometry() -> None:
    payload = native_path_tpl(
        command_groups=[
            [(0, (12, 10)), (3, (10, 10, 2, 0, 1)), (2, (1, 2, 3, 4, 5, 6))],
        ]
    )
    body = decode_message(native_path_body(payload))
    path_module._decode_native_path(body, records_module._DecodeBudget(points_left=6))
    with pytest.raises(NativeError, match="point budget"):
        path_module._decode_native_path(body, records_module._DecodeBudget(points_left=5))


def _brush_body(payload: bytes, *, extra: bytes = b"") -> bytes:
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(payload))
        + field(3, 5)
        + field(4, rgba_message(0.2, 0.4, 0.6, 0.5))
        + field(21, 25)
        + extra
    )


def test_variable_brush_preserves_ordered_samples_and_each_diameter() -> None:
    payload = variable_brush_tpl(samples=[(0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 4)])
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(payload),
        ),
        ROOT_ID,
    )
    assert metadata["unsupported_objects"] == 0
    ink = objects[0].payload
    assert isinstance(ink, Ink)
    assert ink.points == ((1, 1), (2, 3), (5, 6))
    assert ink.widths == (2, 2, 4)
    assert ink.alpha == 0.5
    assert ink.color == "#336699"


@pytest.mark.parametrize(
    "sample",
    [
        (0, math.inf, 3, 0, 1, 2, 5, 6, 0, 1, 4),
        (0, 2, 3, 0, 1, 0, 5, 6, 0, 1, 4),
        (0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 101),
        (0, 2, 3, math.nan, 1, 2, 5, 6, 0, 1, 4),
    ],
)
def test_variable_brush_rejects_invalid_sample_without_returning_a_prefix(
    sample: tuple[float, ...],
) -> None:
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(variable_brush_tpl(samples=[sample])),
        ),
        ROOT_ID,
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_variable_brush_visibility_and_aggregate_point_budget_are_enforced() -> None:
    samples: list[tuple[float, ...]] = [(0, 2, 3, 0, 1, 2, 5, 6, 0, 1, 4)]
    hidden = variable_brush_tpl(samples=samples, flags=[0, 0])
    objects, metadata = decode_native_objects(
        native_records(
            type_code=25,
            body=_brush_body(hidden),
        ),
        ROOT_ID,
    )
    assert objects == []
    assert metadata["unsupported_reasons"] == {"visibility-uncertain": 1}
    body = decode_message(_brush_body(variable_brush_tpl(samples=samples)))
    ink_module._decode_variable_brush(body, records_module._DecodeBudget(points_left=3))
    with pytest.raises(NativeError, match="point budget"):
        ink_module._decode_variable_brush(body, records_module._DecodeBudget(points_left=2))


def _ellipse_body(
    *,
    angle: float = math.pi / 2,
    extra_shape: bytes = b"",
    style: bytes = b"\x10\x01",
    extra_ink: bool = False,
    auxiliary_width: float = 2.0,
) -> bytes:
    payload = tpl(
        points=[] if not extra_ink else [(2, 3)],
        flags=[],
        format_string=FORMAT + "vA(f)",
        width=2.0,
    )
    start = 8 + len(FORMAT + "vA(f)") + 1
    raw = bytearray(payload)
    struct.pack_into("<H", raw, start, 2)
    struct.pack_into("<H", raw, len(raw) - 6, 1)
    shape = (
        field(1, field(1, 30.0) + field(2, 40.0))
        + field(2, field(1, 8.0) + field(2, 3.0))
        + field(3, angle)
        + extra_shape
    )
    auxiliary = field(4, shape) + field(5, style) + field(15, auxiliary_width)
    return (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(bytes(raw)))
        + field(4, rgba_message(0.1, 0.3, 0.5, 0.75))
        + field(9, auxiliary)
        + field(21, 24)
    )


def _without_message_fields(payload: bytes, numbers: set[int]) -> bytes:
    return b"".join(
        item.raw for item in decode_message(payload).fields if item.number not in numbers
    )


def _image_records_without_field_6(*, logical_id: str = LOGICAL_ID) -> list[tuple[bytes, bytes]]:
    records = native_image_records(logical_id=logical_id)
    key, item = records[1]
    body = cast(bytes, decode_message(item).by_number(1)[0].value)
    records[1] = (key, field(1, _without_message_fields(body, {6})))
    return records


def _issue_8_combined_image_records() -> list[tuple[bytes, bytes]]:
    records = _image_records_without_field_6(logical_id=STORAGE_ID)
    descriptor_key, descriptor_value = records[0]
    descriptor_value = _without_message_fields(descriptor_value, {6, 7})
    descriptor_value += field(2, b"generated") + field(14, 1)
    records[0] = (descriptor_key, descriptor_value)
    return records


def _issue_8_alternate_shape_envelope_records() -> list[tuple[bytes, bytes]]:
    return _native_two_point_shape_records([(1.0, 2.0), (10.0, 20.0)])


def _native_two_point_shape_records(
    points: list[tuple[float, float]], *, offset: tuple[float, float] | None = None
) -> list[tuple[bytes, bytes]]:
    body = decode_message(_ellipse_body())
    auxiliary = decode_message(cast(bytes, body.by_number(9)[0].value))
    point_stream = b"".join(field(1, field(1, x) + field(2, y)) for x, y in points)
    replacement = field(1, point_stream) + b"".join(
        item.raw for item in auxiliary.fields if item.number != 4
    )
    body_bytes = b"".join(
        field(9, replacement) if item.number == 9 else item.raw for item in body.fields
    )
    if offset is not None:
        body_bytes += field(6, field(1, offset[0]) + field(2, offset[1]))
    return native_records(body=body_bytes)


def _ellipse_with_unverified_field_6_records() -> list[tuple[bytes, bytes]]:
    body = decode_message(_ellipse_body())
    body_bytes = b"".join(item.raw for item in body.fields if item.number != 6)
    body_bytes += field(6, field(1, 1.0) + field(2, 2.0))
    return native_records(body=body_bytes)


def test_issue_8_generated_controls_remain_supported() -> None:
    image_objects, image_metadata = decode_native_objects(native_image_records(), ROOT_ID)
    assert len(image_objects) == 1
    assert isinstance(image_objects[0].payload, NativeImage)
    assert image_metadata["unsupported_reasons"] == {}

    shape_objects, shape_metadata = decode_native_objects(
        native_records(body=_ellipse_body()), ROOT_ID
    )
    assert len(shape_objects) == 1
    assert isinstance(shape_objects[0].payload, NativePath)
    assert shape_metadata["unsupported_reasons"] == {}


def test_issue_8_image_variants_use_exact_resource_bindings_and_accept_omitted_field_6() -> None:
    variants = [
        (_image_records_without_field_6(), 1),
        (native_image_records(attachment_id=None), 0),
        (_issue_8_combined_image_records(), 1),
    ]
    for records, field_6_absent in variants:
        objects, metadata = decode_native_objects(records, ROOT_ID)
        assert len(objects) == 1
        assert isinstance(objects[0].payload, NativeImage)
        assert metadata["unsupported_objects"] == 0
        assert metadata["unsupported_reasons"] == {}
        assert metadata["native_image_body_field_6_absent_records"] == field_6_absent
        assert metadata["coverage"] == "partial"
        assert metadata["full_fidelity_verified"] is False


def test_issue_8_alternate_geometry_is_decoded_as_an_open_two_point_stroke() -> None:
    objects, metadata = decode_native_objects(_issue_8_alternate_shape_envelope_records(), ROOT_ID)

    assert len(objects) == 1
    path = objects[0].payload
    assert isinstance(path, NativePath)
    assert [command.op for command in path.commands] == ["M", "C"]
    assert path.commands[0].values == (1.0, 2.0)
    assert path.commands[1].values == pytest.approx((4.0, 8.0, 7.0, 14.0, 10.0, 20.0))
    assert path.fill_color is None
    assert path.stroke_color == "#1a4d80"
    assert path.stroke_width == 2.0
    assert metadata["unsupported_reasons"] == {}
    assert metadata["rendered_paths"] == 1
    assert metadata["full_fidelity_verified"] is False


def test_issue_8_shape_translation_moves_the_open_line_by_bounded_fixed32_pair() -> None:
    records = _native_two_point_shape_records([(1.0, 2.0), (10.0, 20.0)], offset=(5.0, 7.0))

    objects, metadata = decode_native_objects(records, ROOT_ID)

    assert isinstance(objects[0].payload, NativePath)
    assert objects[0].payload.commands[0].values == (6.0, 9.0)
    assert objects[0].payload.commands[1].values == pytest.approx(
        (9.0, 15.0, 12.0, 21.0, 15.0, 27.0)
    )
    assert metadata["unsupported_reasons"] == {}


def test_ellipse_field_6_remains_unsupported_without_a_native_example() -> None:
    objects, metadata = decode_native_objects(_ellipse_with_unverified_field_6_records(), ROOT_ID)

    assert objects == []
    assert metadata["unsupported_reasons"] == {"unsupported-native-geometry-field-6": 1}


@pytest.mark.parametrize(
    ("offset", "reason"),
    [
        (field(1, 1.0), "unsupported-native-geometry-field-6"),
        (field(1, 1.0) + field(2, 2.0) + field(3, 3.0), "unsupported-native-geometry-field-6"),
        (field(1, math.nan) + field(2, 2.0), "decode-failed"),
        (field(1, 100_001.0) + field(2, 2.0), "decode-failed"),
        (field(1, 1.0) + field(1, 2.0) + field(2, 3.0), "decode-failed"),
    ],
)
def test_native_shape_translation_rejects_incomplete_or_unsafe_pairs(
    offset: bytes, reason: str
) -> None:
    records = _native_two_point_shape_records([(1.0, 2.0), (10.0, 20.0)])
    key, item = records[1]
    body = decode_message(cast(bytes, decode_message(item).by_number(7)[0].value))
    body_bytes = b"".join(field.raw for field in body.fields) + field(6, offset)
    records[1] = (key, field(7, body_bytes))

    objects, metadata = decode_native_objects(records, ROOT_ID)

    assert objects == []
    assert metadata["unsupported_reasons"] == {reason: 1}


@pytest.mark.parametrize(
    "invalid_field_6",
    [field(6, 0), field(6, 2), field(6, b"not-a-varint")],
)
def test_native_image_rejects_present_field_6_values_outside_observed_marker(
    invalid_field_6: bytes,
) -> None:
    records = native_image_records()
    key, item = records[1]
    body = decode_message(cast(bytes, decode_message(item).by_number(1)[0].value))
    body_bytes = _without_message_fields(b"".join(field.raw for field in body.fields), {6})
    body_bytes += invalid_field_6
    records[1] = (key, field(1, body_bytes))

    objects, metadata = decode_native_objects(records, ROOT_ID)

    assert objects == []
    assert metadata["unsupported_reasons"] == {"unsupported-native-image-field-6": 1}


def test_native_image_rejects_duplicate_field_6() -> None:
    records = native_image_records()
    key, item = records[1]
    body = cast(bytes, decode_message(item).by_number(1)[0].value)
    body += field(6, 1)
    records[1] = (key, field(1, body))

    objects, metadata = decode_native_objects(records, ROOT_ID)

    assert objects == []
    assert metadata["unsupported_reasons"] == {"unsupported-native-image-field-6": 1}


def test_issue_8_supported_image_keeps_mixed_native_coverage_partial() -> None:
    other_storage_id = "00000000-0000-0000-0000-000000000201"
    other_logical_id = "00000000-0000-0000-0000-000000000301"
    records = native_records(storage_id=other_storage_id, logical_id=other_logical_id)
    records.extend(_image_records_without_field_6())

    objects, metadata = decode_native_objects(records, ROOT_ID)
    assert len(objects) == 2
    assert sum(isinstance(item.payload, Ink) for item in objects) == 1
    assert sum(isinstance(item.payload, NativeImage) for item in objects) == 1
    assert metadata["unsupported_objects"] == 0
    assert metadata["unsupported_reasons"] == {}
    assert metadata["coverage"] == "partial"
    assert metadata["native_image_body_field_6_absent_records"] == 1
    assert any("omit body field 6" in warning for warning in cast(list[str], metadata["warnings"]))


def test_issue_8_image_without_copied_attachment_fails_without_background_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _image_records_without_field_6()

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise NativeError(
            "native_data_unavailable",
            "Generated attachment is unavailable.",
            operation="load_native_image",
        )

    monkeypatch.setattr(overlay_module, "load_native_image", unavailable)

    with pytest.raises(NativeError, match="none could be rendered") as captured:
        render_native_overlay(
            _FixtureSnapshot(),
            page(),
            width_points=72,
            height_points=72,
            record_reader=lambda _root_uuid: records,
        )

    assert captured.value.code == "native_data_unavailable"
    assert captured.value.details["operation"] == "decode_native_page"
    native_annotations = captured.value.details["native_annotations"]
    assert isinstance(native_annotations, dict)
    assert native_annotations["unsupported_reasons"] == {"native-image-unavailable": 1}
    assert native_annotations["native_image_body_field_6_absent_records"] == 1


def test_outlined_ellipse_rotation_matches_analytic_quadrant_endpoints() -> None:
    objects, metadata = decode_native_objects(native_records(body=_ellipse_body()), ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    path = objects[0].payload
    assert isinstance(path, NativePath)
    assert [command.op for command in path.commands] == ["M", "C", "C", "C", "C"]
    # Quarter turn maps the major radius to the vertical axis.
    endpoints = [path.commands[0].values] + [command.values[-2:] for command in path.commands[1:]]
    for actual, expected in zip(
        endpoints, [(30, 48), (27, 40), (30, 32), (33, 40), (30, 48)], strict=True
    ):
        assert actual == pytest.approx(expected, abs=1e-5)
    assert path.fill_color is None
    assert path.stroke_color == "#1a4d80"
    assert path.stroke_width == 2
    assert path.stroke_alpha == 0.75


@pytest.mark.parametrize(
    "options",
    [
        {"style": b"\x10\x00"},
        {"style": b"\x10\x01\x18\x01"},
        {"extra_shape": field(6, 1)},
        {"angle": math.nan},
        {"angle": 10.0},
        {"extra_ink": True},
        {"auxiliary_width": 3.0},
    ],
)
def test_outlined_ellipse_unknown_geometry_or_visibility_never_renders_partially(
    options: dict[str, Any],
) -> None:
    objects, metadata = decode_native_objects(
        native_records(body=_ellipse_body(**options)), ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_native_image_binding_and_both_bounds_must_agree() -> None:
    objects, metadata = decode_native_objects(native_image_records(), ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    image = objects[0].payload
    assert isinstance(image, NativeImage)
    assert image.attachment_id == ATTACHMENT_ID
    assert (image.x, image.y, image.width, image.height) == (20, 30, 40, 50)
    for records in [native_image_records(center_offset=(1, 0))]:
        objects, metadata = decode_native_objects(records, ROOT_ID)
        assert objects == []
        assert metadata["unsupported_objects"] == 1

    direct_resource, direct_metadata = decode_native_objects(
        native_image_records(attachment_id=None), ROOT_ID
    )
    assert len(direct_resource) == 1
    assert isinstance(direct_resource[0].payload, NativeImage)
    assert direct_resource[0].payload.attachment_id == RESOURCE_ID
    assert direct_metadata["unsupported_reasons"] == {}


def test_native_image_direct_resource_and_descriptor_resource_mismatch() -> None:
    records = native_image_records()
    descriptor = decode_message(records[0][1])
    direct_descriptor = b"".join(
        field(item.number, item.value) for item in descriptor.fields if item.number not in {4, 7}
    )
    direct = [(records[0][0], direct_descriptor), records[1]]
    objects, metadata = decode_native_objects(direct, ROOT_ID)
    assert metadata["unsupported_objects"] == 0
    assert isinstance(objects[0].payload, NativeImage)
    assert objects[0].payload.attachment_id == RESOURCE_ID
    mismatch_descriptor = b"".join(
        field(
            item.number,
            ATTACHMENT_ID.encode() if item.number == 4 else item.value,
        )
        for item in descriptor.fields
    )
    objects, metadata = decode_native_objects(
        [(records[0][0], mismatch_descriptor), records[1]], ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_reasons"] == {"image-binding-invalid": 1}


def test_native_image_attachment_field_without_resource_field_stays_unresolved() -> None:
    records = native_image_records()
    key, descriptor = records[0]
    descriptor = _without_message_fields(descriptor, {4})
    records[0] = (key, descriptor)

    objects, metadata = decode_native_objects(records, ROOT_ID)

    assert objects == []
    assert metadata["unsupported_reasons"] == {"image-binding-incomplete": 1}


@pytest.mark.parametrize(
    "extra_descriptor,extra_body,extra_item",
    [
        (field(20, b"unmodeled-transform"), b"", b""),
        (b"", field(99, 1), b""),
        (b"", field(6, 2), b""),
        (b"", field(6, 0), b""),
        (b"", field(6, b"wrong-wire-type"), b""),
        (b"", field(6, 1), b""),
        (b"", b"", field(7, b"unmodeled-envelope")),
    ],
)
def test_native_image_unknown_transform_or_envelope_never_renders(
    extra_descriptor: bytes,
    extra_body: bytes,
    extra_item: bytes,
) -> None:
    records = native_image_records()
    item = decode_message(records[1][1])
    body = cast(bytes, item.by_number(1)[0].value)
    malformed = [
        (records[0][0], records[0][1] + extra_descriptor),
        (records[1][0], field(1, body + extra_body) + extra_item),
    ]
    objects, metadata = decode_native_objects(malformed, ROOT_ID)
    assert objects == []
    assert metadata["unsupported_objects"] == 1


@pytest.mark.parametrize(
    "width,height,x",
    [
        (0.0, 5.0, 1.0),
        (-1.0, 5.0, 1.0),
        (math.inf, 5.0, 1.0),
        (5.0, math.nan, 1.0),
        (5.0, 5.0, 99999.0),
    ],
)
def test_native_image_rejects_invalid_rectangle_before_resolving_media(
    width: float,
    height: float,
    x: float,
) -> None:
    rectangle = field(1, field(1, x) + field(2, 1.0)) + field(2, field(1, width) + field(2, height))
    records = native_image_records()
    original_body = decode_message(cast(bytes, decode_message(records[1][1]).by_number(1)[0].value))
    body = b"".join(
        field(item.number, rectangle if item.number == 2 else item.value)
        for item in original_body.fields
    )
    objects, metadata = decode_native_objects(
        [records[0], (records[1][0], field(1, body))],
        ROOT_ID,
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_outlined_ellipse_exact_point_budget_and_fixed32_version_rejection() -> None:
    body = decode_message(_ellipse_body())
    path_module._decode_native_ellipse(body, records_module._DecodeBudget(points_left=13))
    with pytest.raises(NativeError, match="point budget"):
        path_module._decode_native_ellipse(body, records_module._DecodeBudget(points_left=12))
    # Integer-valued FIXED32 fields are not interchangeable with VARINT envelope versions.
    malformed = _ellipse_body() + varint(21 * 8 + 5) + struct.pack("<I", 24)
    objects, metadata = decode_native_objects(native_records(body=malformed), ROOT_ID)
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_native_descriptor_requires_varint_creation_clock() -> None:
    records = native_records()
    descriptor = decode_message(records[0][1])
    descriptor_payload = b"".join(
        varint(8 * 8 + 5) + struct.pack("<I", 1)
        if item.number == 8
        else field(item.number, item.value)
        for item in descriptor.fields
    )
    objects, metadata = decode_native_objects(
        [(records[0][0], descriptor_payload), records[1]], ROOT_ID
    )
    assert objects == []
    assert metadata["unsupported_objects"] == 1


def test_ink_translation_cannot_move_valid_points_beyond_decoder_bounds() -> None:
    body = decode_message(field(2, framed(tpl(points=[(99999.0, 10.0)]))) + field(6, field(1, 2.0)))
    with pytest.raises(ValueError, match="translated ink coordinate"):
        decode_ink(body)


@pytest.mark.parametrize("ellipse", [False, True])
@pytest.mark.parametrize("preceding", ["move", "cubic", "arc"])
def test_native_arc_rejects_discontinuous_start_without_inventing_an_svg_center(
    ellipse: bool,
    preceding: str,
) -> None:
    first = (0, (1000.0, 1000.0))
    commands: list[tuple[int, tuple[float, ...]]] = [first]
    if preceding == "cubic":
        commands.append((2, (900, 900, 950, 950, 1000, 1000)))
    elif preceding == "arc":
        commands = [
            (0, (1002, 1000)),
            (6, (1000, 1000, 2, 3, 0, math.pi / 2, 0))
            if ellipse
            else (3, (1000, 1000, 2, 0, math.pi / 2)),
        ]
    commands.append(
        (6, (10, 10, 2, 3, 0, math.pi / 2, 0)) if ellipse else (3, (10, 10, 2, 0, math.pi / 2))
    )
    payload = native_path_tpl(command_groups=[commands], ellipse=ellipse)
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert objects == []
    assert metadata["rendered_objects"] == 0
    assert metadata["unsupported_reasons"] == {"unsupported-native-path": 1}


@pytest.mark.parametrize("ellipse", [False, True])
def test_native_arc_rejects_an_opposite_point_on_the_same_ellipse(ellipse: bool) -> None:
    commands: list[tuple[int, tuple[float, ...]]] = [
        (0, (8.0, 10.0)),
        (6, (10, 10, 2, 3, 0, math.pi / 2, 0)) if ellipse else (3, (10, 10, 2, 0, math.pi / 2)),
    ]
    payload = native_path_tpl(command_groups=[commands], ellipse=ellipse)
    body = native_path_body(payload, metadata=b"\x0a\x00" if ellipse else b"")
    objects, metadata = decode_native_objects(native_records(body=body), ROOT_ID)
    assert objects == []
    assert metadata["unsupported_reasons"] == {"unsupported-native-path": 1}


def test_native_arc_preserves_valid_start_at_coordinate_limit() -> None:
    path = _decoded_path(
        native_path_tpl(
            command_groups=[
                [
                    (0, (100000.0, 10.0)),
                    (3, (99998, 10, 2, 0, math.pi / 2)),
                ]
            ]
        )
    )
    assert path.commands[0].values == (100000, 10)
    assert path.commands[1].values == pytest.approx((2, 2, 0, 1, 0, 99998, 12), abs=1e-6)


def test_ellipse_arc_retains_small_observed_parametric_angle_discrepancy() -> None:
    # Float32 path points can lie on the ellipse while its angle marker differs
    # slightly. This stays within the supported ellipse envelope.
    path = _decoded_path(
        native_path_tpl(
            ellipse=True,
            command_groups=[
                [
                    (0, (10 + 2 * math.cos(0.1), 20 + 30 * math.sin(0.1))),
                    (6, (10, 20, 2, 30, 0.11, math.pi / 2, 0)),
                ]
            ],
        ),
        ellipse=True,
    )
    assert path.commands[1].values == pytest.approx((2, 30, 0, 1, 0, 10, 50), abs=1e-5)


def test_decode_failure_classification_uses_reason_not_message() -> None:
    from goodnotes_cli.native.objects import _unsupported_reason
    from goodnotes_cli.native.records import _NativeDecodeIssue

    assert _unsupported_reason(_NativeDecodeIssue("visibility-uncertain", "Changed wording")) == (
        "visibility-uncertain"
    )
    assert _unsupported_reason(ValueError("visibility TPL schema path command image identity")) == (
        "decode-failed"
    )
