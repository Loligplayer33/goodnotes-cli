"""Native image bindings and shape envelopes with independent generated records."""

from __future__ import annotations

import math
import struct
from typing import Any, cast

import pytest

from goodnotes_cli._vendor.wire import decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native.geometry import _decode_native_shape
from goodnotes_cli.native.objects import decode_native_objects
from goodnotes_cli.native.records import Ink, NativeImage, NativePath, _DecodeBudget
from tests.support.native_oracle import (
    ATTACHMENT_ID,
    RESOURCE_ID,
    ROOT_ID,
    _ellipse_body,
    _ellipse_with_unverified_field_6_records,
    _image_records_without_field_6,
    _issue_8_alternate_shape_envelope_records,
    _issue_8_combined_image_records,
    _native_two_point_shape_records,
    _without_message_fields,
    field,
    native_image_records,
    native_records,
    varint,
)


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
    assert [command.op for command in path.commands] == ["M", "L"]
    assert path.commands[0].values == (1.0, 2.0)
    assert path.commands[1].values == (10.0, 20.0)
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
    assert objects[0].payload.commands[1].values == (15.0, 27.0)
    assert metadata["unsupported_reasons"] == {}


def test_ellipse_translation_uses_the_shared_shape_transform() -> None:
    objects, metadata = decode_native_objects(_ellipse_with_unverified_field_6_records(), ROOT_ID)

    assert isinstance(objects[0].payload, NativePath)
    assert objects[0].payload.commands[0].values == pytest.approx((31, 50), abs=1e-5)
    assert metadata["unsupported_reasons"] == {}


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
    _decode_native_shape(body, _DecodeBudget(points_left=13))
    with pytest.raises(NativeError, match="point budget"):
        _decode_native_shape(body, _DecodeBudget(points_left=12))
    # Integer-valued FIXED32 fields are not interchangeable with VARINT envelope versions.
    malformed = _ellipse_body() + varint(21 * 8 + 5) + struct.pack("<I", 24)
    objects, metadata = decode_native_objects(native_records(body=malformed), ROOT_ID)
    assert objects == []
    assert metadata["unsupported_objects"] == 1
