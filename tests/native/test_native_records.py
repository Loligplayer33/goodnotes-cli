"""Native identity, ordering, partial coverage, and cumulative decode budget regressions."""

from __future__ import annotations

import struct
from typing import Any, cast

import pytest

from goodnotes_cli._vendor.compression import CompressionError, decode_apple_lz4
from goodnotes_cli._vendor.tpl import TplDecodeError, decode_tpl
from goodnotes_cli._vendor.wire import DecodeError, decode_message
from goodnotes_cli.native import NativeError
from goodnotes_cli.native import objects as object_module
from goodnotes_cli.native import records as records_module
from goodnotes_cli.native.objects import decode_native_objects, decode_native_records
from tests.support.native_oracle import (
    LOGICAL_ID,
    ROOT_ID,
    STORAGE_ID,
    _without_message_fields,
    field,
    framed,
    native_records,
    tpl,
    varint,
)


def _budget_test_records(payloads: list[bytes]) -> list[tuple[bytes, bytes]]:
    records: list[tuple[bytes, bytes]] = []
    for index, payload in enumerate(payloads):
        logical_id = f"00000000-0000-0000-0000-{index + 1000:012d}"
        storage_id = f"00000000-0000-0000-0000-{index + 2000:012d}"
        body = field(1, logical_id.encode()) + field(2, payload)
        records.extend(
            native_records(
                storage_id=storage_id, logical_id=logical_id, sequence=index + 1, body=body
            )
        )
    return records


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


@pytest.mark.parametrize("reverse_records", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    ("extra_descriptor", "storage_alias", "erased", "has_item"),
    [
        pytest.param(b"", True, False, False, id="missing-item"),
        pytest.param(field(99, 1), True, False, True, id="unknown-descriptor-field"),
        pytest.param(field(99, 1), True, False, False, id="unknown-field-and-missing-item"),
        pytest.param(b"", False, False, True, id="missing-alias-storage-mismatch"),
        pytest.param(field(6, b"invalid"), False, False, True, id="invalid-alias"),
        pytest.param(field(6, STORAGE_ID.encode()), False, False, True, id="wrong-alias"),
        pytest.param(b"", True, True, True, id="erased"),
        pytest.param(field(3, 2), True, False, True, id="invalid-erased-state"),
        pytest.param(field(14, b"invalid"), True, False, True, id="invalid-metadata"),
    ],
)
def test_readable_identity_excludes_ambiguous_geometry_before_semantic_acceptance(
    extra_descriptor: bytes,
    storage_alias: bool,
    erased: bool,
    has_item: bool,
    reverse_records: bool,
) -> None:
    rejected = native_records(
        storage_id=STORAGE_ID[:-1] + "1",
        sequence=2,
        extra_descriptor=extra_descriptor,
        storage_alias=storage_alias,
        erased=erased,
    )
    records = native_records() + (rejected if has_item else rejected[:1])
    objects, metadata = decode_native_objects(
        list(reversed(records)) if reverse_records else records, ROOT_ID
    )

    assert objects == []
    expected_reasons = {"ambiguous-logical-version": 1}
    if not erased:
        expected_reasons["decode-failed"] = 1
    assert metadata["unsupported_reasons"] == expected_reasons
    assert metadata["erased_objects"] == int(erased)
    assert metadata["rendered_objects"] == 0
    assert metadata["coverage"] == "partial"


@pytest.mark.parametrize("reverse_records", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize(
    "logical_fields",
    [
        pytest.param(None, id="missing-descriptor"),
        pytest.param(b"", id="missing-logical-id"),
        pytest.param(field(1, LOGICAL_ID.encode()) * 2, id="duplicate-logical-id"),
        pytest.param(field(1, 1), id="wrong-wire-logical-id"),
        pytest.param(field(1, b"invalid"), id="malformed-logical-id"),
        pytest.param(field(1, LOGICAL_ID.replace("-", "").encode()), id="noncanonical-logical-id"),
        pytest.param(field(1, b"\xff"), id="non-ascii-logical-id"),
        pytest.param(field(1, LOGICAL_ID.encode()) + b"\x00", id="invalid-wire-after-logical-id"),
    ],
)
def test_unreadable_identity_does_not_invent_ambiguity(
    logical_fields: bytes | None, reverse_records: bool
) -> None:
    other = native_records(storage_id=STORAGE_ID[:-1] + "1", sequence=2)
    records = native_records()
    if logical_fields is not None:
        descriptor = logical_fields + _without_message_fields(other[0][1], {1})
        records.append((other[0][0], descriptor))
    records.append(other[1])
    objects, metadata = decode_native_objects(
        list(reversed(records)) if reverse_records else records, ROOT_ID
    )

    assert len(objects) == 1
    assert objects[0].logical_id == LOGICAL_ID
    assert metadata["unsupported_reasons"] == {"decode-failed": 1}
    assert metadata["rendered_objects"] == 1
    assert metadata["coverage"] == "partial"


@pytest.mark.parametrize("reverse_records", [False, True], ids=["forward", "reverse"])
@pytest.mark.parametrize("category", ["deleted", "unknown"])
def test_non_normal_categories_do_not_contribute_logical_identity(
    category: str, reverse_records: bool
) -> None:
    records = native_records() + native_records(
        storage_id=STORAGE_ID[:-1] + "1", sequence=2, category=category
    )
    objects, metadata = decode_native_objects(
        list(reversed(records)) if reverse_records else records, ROOT_ID
    )

    assert len(objects) == 1
    assert objects[0].logical_id == LOGICAL_ID
    assert metadata["deleted_objects"] == int(category == "deleted")
    assert metadata["unsupported_reasons"] == (
        {} if category == "deleted" else {"unknown-record-category": 1}
    )
    assert metadata["coverage"] == "partial"


@pytest.mark.parametrize("extra", [field(20, b"geometry"), field(20, 1), field(20, b"") * 2])
def test_auxiliary_body_field_twenty_is_empty_only(extra: bytes) -> None:
    body = field(1, LOGICAL_ID.encode()) + field(2, framed(tpl())) + extra
    inks, metadata = decode_native_records(native_records(body=body), ROOT_ID)
    assert not inks and metadata["unsupported_objects"] == 1


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


def test_aggregate_point_budget_is_enforced_across_objects(monkeypatch: pytest.MonkeyPatch) -> None:
    budget_class = records_module._DecodeBudget
    monkeypatch.setattr(object_module, "_DecodeBudget", lambda: budget_class(points_left=6))
    records = native_records() + native_records(
        storage_id=STORAGE_ID[:-1] + "1", logical_id=LOGICAL_ID[:-1] + "1"
    )
    with pytest.raises(NativeError, match="point budget"):
        decode_native_records(records, ROOT_ID)


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


def test_malformed_blocks_charge_page_budget_and_preserve_partial_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Two valid blocks expand before an invalid next marker rejects each object.
    block = framed(b"x" * 20, compressed=False)[:-4]
    malformed = block + block + b"bad!"
    valid = framed(tpl())
    budget_class = records_module._DecodeBudget
    total = 2 * 40 + len(tpl())
    monkeypatch.setattr(object_module, "_DecodeBudget", lambda: budget_class(bytes_left=total))
    objects, metadata = decode_native_objects(
        _budget_test_records([malformed, valid, malformed]), ROOT_ID
    )
    assert len(objects) == 1
    assert metadata["unsupported_objects"] == 2
    assert metadata["coverage"] == "partial"

    monkeypatch.setattr(object_module, "_DecodeBudget", lambda: budget_class(bytes_left=total - 1))
    with pytest.raises(NativeError, match="decompressed-byte or point budget"):
        decode_native_objects(_budget_test_records([malformed, valid, malformed]), ROOT_ID)


def test_valid_multiple_objects_charge_each_block_once(monkeypatch: pytest.MonkeyPatch) -> None:
    data = tpl()
    split = len(data) // 2
    frame = framed(data[:split], compressed=False)[:-4] + framed(data[split:])
    budget_class = records_module._DecodeBudget
    monkeypatch.setattr(
        object_module, "_DecodeBudget", lambda: budget_class(bytes_left=2 * len(data))
    )
    objects, metadata = decode_native_objects(_budget_test_records([frame, frame]), ROOT_ID)
    assert len(objects) == 2
    assert metadata["unsupported_objects"] == 0
    monkeypatch.setattr(
        object_module, "_DecodeBudget", lambda: budget_class(bytes_left=2 * len(data) - 1)
    )
    with pytest.raises(NativeError, match="decompressed-byte or point budget"):
        decode_native_objects(_budget_test_records([frame, frame]), ROOT_ID)


def test_decode_failure_classification_uses_reason_not_message() -> None:
    from goodnotes_cli.native.objects import _unsupported_reason
    from goodnotes_cli.native.records import _NativeDecodeIssue

    assert _unsupported_reason(_NativeDecodeIssue("visibility-uncertain", "Changed wording")) == (
        "visibility-uncertain"
    )
    assert _unsupported_reason(ValueError("visibility TPL schema path command image identity")) == (
        "decode-failed"
    )
