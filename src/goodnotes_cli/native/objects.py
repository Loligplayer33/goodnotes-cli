"""Pair native records, resolve identity and order, and report reconstruction coverage."""

from __future__ import annotations

import struct
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import IntEnum
from typing import cast

from .._vendor.wire import WireType, decode_message
from .geometry import _decode_native_fill, _decode_native_image, _decode_native_shape
from .ink import _decode_variable_brush, decode_ink
from .paths import _decode_native_path, _decode_native_pattern_path
from .records import (
    DecodedNativeObject,
    Ink,
    NativeImage,
    NativePath,
    NativePayload,
    _DecodeBudget,
    _NativeDecodeIssue,
    _UnsupportedNativeVariant,
)
from .values import NativeObjectType, _one, _uuid


class _DescriptorField(IntEnum):
    LOGICAL_ID = 1
    ERASED = 3
    RESOURCE_ID = 4
    STORAGE_ALIAS = 6
    ATTACHMENT_ID = 7
    ACTOR = 8
    SEQUENCE = 9
    OPAQUE_METADATA = 14
    OBJECT_TYPE = 16


class _ItemEnvelope(IntEnum):
    IMAGE = 1
    ANNOTATION = 7
    FILL = 9


@dataclass
class _PageObservations:
    """Evidence collected even when a later field makes an object undecodable."""

    categories: Counter[str] = field(default_factory=Counter)
    reasons: Counter[str] = field(default_factory=Counter)
    logical_counts: Counter[str] = field(default_factory=Counter)
    descriptor_variants: Counter[str] = field(default_factory=Counter)
    unmodeled_descriptor_fields: set[int] = field(default_factory=set)
    unmodeled_empty_body_fields: set[int] = field(default_factory=set)
    unmodeled_body_fields: set[int] = field(default_factory=set)
    unmodeled_image_fields: set[int] = field(default_factory=set)
    unmodeled_tpl_fields: dict[str, set[int]] = field(default_factory=lambda: defaultdict(set))
    image_body_field_6_absent: int = 0
    erased: int = 0
    deleted: int = 0


@dataclass(frozen=True)
class _Candidate:
    actor: int
    sequence: int
    logical_id: str
    payloads: Sequence[NativePayload]


def _unsupported_reason(error: Exception) -> str:
    if isinstance(error, (_UnsupportedNativeVariant, _NativeDecodeIssue)):
        return error.reason
    return "decode-failed"


def _decode_pair(
    category: str,
    storage_id: str,
    pair: dict[str, bytes],
    budget: _DecodeBudget,
    observations: _PageObservations,
) -> _Candidate | None:
    """Decode one object, retaining observations made before any field failure."""
    if category == "deleted":
        observations.deleted += 1
        return None
    if category != "normal":
        observations.reasons["unknown-record-category"] += 1
        return None
    descriptor_payload = pair.get("descriptor")
    if descriptor_payload is None:
        raise ValueError("Missing annotation descriptor/item pair")
    descriptor = decode_message(descriptor_payload)
    logical_id = _uuid(_one(descriptor, _DescriptorField.LOGICAL_ID, bytes))
    # Rejected or erased versions still make this logical identity ambiguous.
    observations.logical_counts[logical_id] += 1
    if set(pair) != {"descriptor", "item"}:
        raise ValueError("Missing annotation descriptor/item pair")
    if any(
        item.number not in {1, 2, 3, 4, 6, 7, 8, 9, 14, 16, 20, 21} for item in descriptor.fields
    ):
        raise ValueError("Unsupported native descriptor field")
    alias = _one(descriptor, _DescriptorField.STORAGE_ALIAS, bytes, required=False)
    bound_id = _uuid(alias) if alias is not None else logical_id
    if bound_id != storage_id.upper():
        raise ValueError("Annotation storage identity mismatch")
    metadata_fields = descriptor.by_number(_DescriptorField.OPAQUE_METADATA)
    if metadata_fields:
        if (
            len(metadata_fields) != 1
            or metadata_fields[0].wire_type != WireType.VARINT
            or not isinstance(metadata_fields[0].value, int)
            or not 0 <= metadata_fields[0].value <= (1 << 64) - 1
        ):
            raise ValueError("Unsupported native descriptor metadata")
        observations.unmodeled_descriptor_fields.add(_DescriptorField.OPAQUE_METADATA)
    is_erased = _one(descriptor, _DescriptorField.ERASED, int, required=False)
    if is_erased not in {None, 0, 1}:
        raise ValueError("Unknown erased flag")
    if is_erased == 1:
        observations.erased += 1
        return None
    object_type = _one(descriptor, _DescriptorField.OBJECT_TYPE, int)
    if object_type not in {
        NativeObjectType.STANDARD,
        NativeObjectType.VARIABLE_BRUSH,
        NativeObjectType.PATTERN_FILL,
    }:
        observations.reasons["unsupported-object-type"] += 1
        return None
    if descriptor.by_number(20) or descriptor.by_number(21):
        observations.reasons["unsupported-parent-transform"] += 1
        return None
    actor = cast(int, _one(descriptor, _DescriptorField.ACTOR, int))
    sequence = cast(int, _one(descriptor, _DescriptorField.SEQUENCE, int))
    item = decode_message(pair["item"])
    item_numbers = {field.number for field in item.fields}
    if item_numbers == {_ItemEnvelope.IMAGE}:
        if object_type != NativeObjectType.STANDARD:
            raise ValueError("Unsupported non-ink native image type")
        image_body = decode_message(cast(bytes, _one(item, _ItemEnvelope.IMAGE, bytes)))
        image = _decode_native_image(descriptor, item, logical_id)
        observations.unmodeled_image_fields.update({5, 15})
        if image_body.by_number(6):
            observations.unmodeled_image_fields.add(6)
        else:
            observations.image_body_field_6_absent += 1
        observations.descriptor_variants[
            "image-attachment-alias"
            if descriptor.by_number(_DescriptorField.ATTACHMENT_ID)
            else "direct-image-resource"
        ] += 1
        return _Candidate(actor, sequence, logical_id, [image])
    elif item_numbers == {_ItemEnvelope.FILL}:
        if (
            object_type != NativeObjectType.STANDARD
            or descriptor.by_number(_DescriptorField.RESOURCE_ID)
            or descriptor.by_number(_DescriptorField.ATTACHMENT_ID)
        ):
            raise ValueError("Unsupported native fill binding")
        body = decode_message(cast(bytes, _one(item, _ItemEnvelope.FILL, bytes)))
        if _uuid(_one(body, 1, bytes)) != logical_id:
            raise ValueError("Annotation logical identity mismatch")
        fill = _decode_native_fill(body, budget)
        observations.unmodeled_body_fields.update({2, 3, 5, 15})
        observations.descriptor_variants["filled-geometry"] += 1
        return _Candidate(actor, sequence, logical_id, [fill])
    elif item_numbers == {_ItemEnvelope.ANNOTATION}:
        body = decode_message(cast(bytes, _one(item, _ItemEnvelope.ANNOTATION, bytes)))
        if _uuid(_one(body, 1, bytes)) != logical_id:
            raise ValueError("Annotation logical identity mismatch")
        if object_type != NativeObjectType.PATTERN_FILL and (
            descriptor.by_number(_DescriptorField.RESOURCE_ID)
            or descriptor.by_number(_DescriptorField.ATTACHMENT_ID)
        ):
            raise ValueError("Image attachment binding on a non-image object")
        body_variant = _one(body, 3, int, required=False)
        if object_type == NativeObjectType.PATTERN_FILL:
            patterned = _decode_native_pattern_path(body, budget)
            pattern = patterned.pattern
            if pattern is None or descriptor.by_number(_DescriptorField.ATTACHMENT_ID):
                raise ValueError("Unsupported native pattern resource binding")
            if (
                _uuid(_one(descriptor, _DescriptorField.RESOURCE_ID, bytes))
                != pattern.attachment_id
            ):
                raise _NativeDecodeIssue(
                    "image-binding-invalid", "Native image resource identity mismatch"
                )
            payloads: list[NativePayload] = [patterned]
            observations.unmodeled_body_fields.add(20)
            observations.unmodeled_tpl_fields["pattern-fill"].update({2, 3})
        elif object_type == NativeObjectType.VARIABLE_BRUSH:
            payloads = [*_decode_variable_brush(body, budget)]
            observations.unmodeled_body_fields.add(3)
            observations.unmodeled_tpl_fields["variable-brush"].update({0, 3, 4, 8, 9})
        elif body_variant == 1:
            payloads = [_decode_native_path(body, budget)]
            observations.unmodeled_body_fields.add(3)
            observations.unmodeled_body_fields.add(20)
            observations.unmodeled_tpl_fields["native-path"].update({2, 3})
        elif _one(body, 9, bytes, required=False) not in {None, b""}:
            payloads = [_decode_native_shape(body, budget)]
            observations.unmodeled_body_fields.add(9)
        else:
            payloads = [*decode_ink(body, budget)]
            if body_variant is not None:
                observations.unmodeled_body_fields.add(3)
        if _one(body, 20, bytes, required=False) == b"":
            observations.unmodeled_empty_body_fields.add(20)
        variant = "storage-alias" if alias is not None else "direct-identity"
        observations.descriptor_variants[variant] += 1
        return _Candidate(actor, sequence, logical_id, payloads)
    else:
        raise ValueError("Unsupported native annotation item envelope")


def _metadata(
    observations: _PageObservations,
    candidates: Sequence[_Candidate],
    ordered_objects: Sequence[DecodedNativeObject],
    *,
    record_count: int,
    object_count: int,
) -> dict[str, object]:
    """Describe reconstruction evidence without equating unsupported data with absence."""
    actors = {row.actor for row in candidates}
    creation_clocks = Counter((row.actor, row.sequence) for row in candidates)
    order_ties = any(count > 1 for count in creation_clocks.values())
    warnings = [
        "Native annotations are an approximate reconstruction. Unobserved object types, "
        "some pen dynamics, and application drawing order remain unverified.",
        "Page identity uses the observed projection UUID plus one native namespace mapping.",
    ]
    if len(actors) > 1:
        warnings.append(
            "Multiple creation actors are present. Cross-device drawing order is unknown."
        )
    if order_ties:
        warnings.append(
            "Some annotation creation clocks are equal. Their drawing order is unknown."
        )
    if observations.deleted:
        warnings.append(
            "Deleted-category objects were excluded. Their application visibility was "
            "not independently verified."
        )
    if observations.unmodeled_descriptor_fields:
        warnings.append(
            "Native descriptor field 14 is preserved as opaque metadata. Its effect on "
            "application rendering is unverified, so reconstruction coverage is partial."
        )
    if observations.unmodeled_body_fields:
        warnings.append(
            "Observed native body fields "
            + ", ".join(str(number) for number in sorted(observations.unmodeled_body_fields))
            + " include values whose independent rendering effect is unverified."
        )
    if observations.unmodeled_image_fields:
        warnings.append(
            "Native image body fields "
            + ", ".join(str(number) for number in sorted(observations.unmodeled_image_fields))
            + " are preserved as opaque metadata. Image bytes are resolved separately from "
            "the copied attachment store."
        )
    if observations.image_body_field_6_absent:
        warnings.append(
            "Some decoded native image records omit body field 6; its effect on "
            "application appearance or visibility is unverified."
        )
    if observations.unmodeled_tpl_fields:
        warnings.append(
            "Validated native template arrays "
            + ", ".join(sorted(observations.unmodeled_tpl_fields))
            + " contain additional opaque samples that do not drive reconstructed geometry."
        )
    if "variable-brush" in observations.unmodeled_tpl_fields:
        warnings.append(
            "Pencil widths use the configured width and relative sample dynamics. "
            "Absolute pressure response and pencil texture remain approximate."
        )
    if "pattern-fill" in observations.unmodeled_tpl_fields:
        warnings.append(
            "Pattern fills use the observed scale-1 raster tiling mode. "
            "Pattern resolution and brush anchoring remain approximate for other resources."
        )
    if observations.reasons:
        warnings.append(
            "Some native records could not be reconstructed. Do not treat this image "
            "as complete evidence of all page annotations."
        )
    stroke_count = sum(
        1
        for native_object in ordered_objects
        if isinstance(native_object.payload, (Ink, NativePath))
    )
    image_count = sum(
        isinstance(native_object.payload, NativeImage) for native_object in ordered_objects
    )
    path_count = sum(
        isinstance(native_object.payload, NativePath) for native_object in ordered_objects
    )
    metadata: dict[str, object] = {
        "annotation_status": "native-reconstructed-approximate",
        "source": "copied-native-records",
        "coverage": (
            "partial"
            if observations.reasons
            or observations.deleted
            or len(actors) > 1
            or order_ties
            or observations.unmodeled_descriptor_fields
            or observations.unmodeled_body_fields
            or observations.unmodeled_image_fields
            or observations.image_body_field_6_absent
            or observations.unmodeled_tpl_fields
            else "supported-native-records"
            if image_count or path_count
            else "supported-pen-records"
        ),
        "full_fidelity_verified": False,
        "page_mapping": "projection-uuid-plus-one-observed",
        "native_records": record_count,
        "native_objects": object_count,
        "rendered_objects": len(candidates),
        "rendered_strokes": stroke_count,
        "rendered_paths": path_count,
        "rendered_images": image_count,
        "erased_objects": observations.erased,
        "deleted_objects": observations.deleted,
        "unsupported_objects": sum(observations.reasons.values()),
        "unsupported_reasons": dict(observations.reasons),
        "categories": dict(observations.categories),
        "descriptor_variants": dict(observations.descriptor_variants),
        "unmodeled_descriptor_fields": sorted(observations.unmodeled_descriptor_fields),
        "unmodeled_empty_body_fields": sorted(observations.unmodeled_empty_body_fields),
        "unmodeled_body_fields": sorted(observations.unmodeled_body_fields),
        "unmodeled_image_fields": sorted(observations.unmodeled_image_fields),
        "native_image_body_field_6_absent_records": observations.image_body_field_6_absent,
        "unmodeled_tpl_fields": {
            name: sorted(field_numbers)
            for name, field_numbers in sorted(observations.unmodeled_tpl_fields.items())
        },
        "unmodeled_observed_envelope_fields": [7, 15],
        "warnings": warnings,
    }
    return metadata


def decode_native_objects(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[DecodedNativeObject], dict[str, object]]:
    """Decode supported annotation payloads while retaining source order metadata."""
    observations = _PageObservations()
    objects: dict[tuple[str, str], dict[str, bytes]] = defaultdict(dict)
    for key, value in records:
        try:
            root, category, storage_id, kind = key.decode("ascii").split(".")
            if root != root_uuid or kind not in {"descriptor", "item"}:
                raise ValueError("Unsupported native object key")
            _uuid(storage_id.encode())
            observations.categories[category] += 1
            if kind in objects[(category, storage_id)]:
                raise ValueError("Duplicate native key")
            objects[(category, storage_id)][kind] = value
        except (UnicodeError, ValueError):
            observations.reasons["invalid-record-key"] += 1

    candidates: list[_Candidate] = []
    budget = _DecodeBudget()
    for (category, storage_id), pair in objects.items():
        try:
            candidate = _decode_pair(category, storage_id, pair, budget, observations)
            if candidate is not None:
                candidates.append(candidate)
        # NativeError, including page budget exhaustion, intentionally aborts the page.
        except (ValueError, UnicodeError, IndexError, TypeError, struct.error) as exc:
            observations.reasons[_unsupported_reason(exc)] += 1

    duplicate_ids = {
        logical_id for logical_id, count in observations.logical_counts.items() if count > 1
    }
    if duplicate_ids:
        observations.reasons["ambiguous-logical-version"] += sum(
            1 for candidate in candidates if candidate.logical_id in duplicate_ids
        )
        candidates = [
            candidate for candidate in candidates if candidate.logical_id not in duplicate_ids
        ]
    ordered_objects = [
        DecodedNativeObject(
            candidate.actor, candidate.sequence, candidate.logical_id, payload, part_index
        )
        for candidate in sorted(
            candidates,
            key=lambda candidate: (candidate.sequence, candidate.actor, candidate.logical_id),
        )
        for part_index, payload in enumerate(candidate.payloads)
    ]
    metadata = _metadata(
        observations,
        candidates,
        ordered_objects,
        record_count=len(records),
        object_count=len(objects),
    )
    return ordered_objects, metadata


def decode_native_records(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[Ink], dict[str, object]]:
    """Compatibility view returning only decoded pen/highlighter strokes."""
    objects, metadata = decode_native_objects(records, root_uuid)
    return [obj.payload for obj in objects if isinstance(obj.payload, Ink)], metadata
