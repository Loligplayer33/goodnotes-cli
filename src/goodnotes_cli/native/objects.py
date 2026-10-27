from __future__ import annotations

import math
import struct
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import cast

from .._vendor.wire import Message, WireType, decode_message
from ..native.ink import _decode_variable_brush, decode_ink
from ..native.paths import _decode_native_path, _decode_native_shape, _fixed_point
from ..native.records import (
    DecodedNativeObject,
    Ink,
    NativeImage,
    NativePath,
    _DecodeBudget,
    _NativeDecodeIssue,
    _UnsupportedNativeVariant,
)
from ..native.values import _one, _uuid


def _native_image_rect(payload: bytes) -> tuple[float, float, float, float]:
    message = decode_message(payload)
    if len(message.fields) != 2 or {item.number for item in message.fields} != {1, 2}:
        raise ValueError("Invalid native image rectangle")
    origin_payload = cast(bytes, _one(message, 1, bytes))
    size_payload = cast(bytes, _one(message, 2, bytes))
    x, y = _fixed_point(origin_payload)
    width, height = _fixed_point(size_payload)
    if width <= 0 or height <= 0 or width > 100_000 or height > 100_000:
        raise ValueError("Invalid native image dimensions")
    if abs(x + width) > 100_000 or abs(y + height) > 100_000:
        raise ValueError("Native image rectangle exceeds coordinate bounds")
    return x, y, width, height


def _decode_native_image(descriptor: Message, item: Message, logical_id: str) -> NativeImage:
    """Decode an image reference and native-unit bounds without opening media."""
    if any(field.number != 1 for field in item.fields):
        raise ValueError("Unsupported native image item envelope")
    payload = cast(bytes, _one(item, 1, bytes))
    body = decode_message(payload)
    if any(field.number not in {1, 2, 3, 4, 5, 6, 15, 18} for field in body.fields):
        raise ValueError("Unsupported native image body field")
    if _uuid(_one(body, 1, bytes)) != logical_id:
        raise _NativeDecodeIssue("image-binding-invalid", "Native image logical identity mismatch")
    original_resource = _uuid(_one(body, 4, bytes))
    field_6 = body.by_number(6)
    if field_6 and (
        len(field_6) != 1
        or field_6[0].wire_type != WireType.VARINT
        or not isinstance(field_6[0].value, int)
        or field_6[0].value != 1
    ):
        raise _UnsupportedNativeVariant("unsupported-native-image-field-6")
    if _one(body, 18, int) != 24:
        raise ValueError("Unsupported native image version or transform")
    for number in (5, 15):
        opaque = cast(bytes, _one(body, number, bytes))
        if len(opaque) > 64:
            raise ValueError("Native image metadata exceeds its byte budget")

    x, y, width, height = _native_image_rect(cast(bytes, _one(body, 2, bytes)))
    center_x, center_y, center_width, center_height = _native_image_rect(
        cast(bytes, _one(body, 3, bytes))
    )
    tolerance = max(1e-3, max(abs(x), abs(y), width, height) * 1e-6)
    if (
        not math.isclose(center_x, x + width / 2, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_y, y + height / 2, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_width, width, rel_tol=1e-6, abs_tol=tolerance)
        or not math.isclose(center_height, height, rel_tol=1e-6, abs_tol=tolerance)
    ):
        raise ValueError("Native image center and top-left bounds disagree")

    descriptor_resource = _one(descriptor, 4, bytes, required=False)
    descriptor_attachment = _one(descriptor, 7, bytes, required=False)
    if descriptor_resource is None and descriptor_attachment is None:
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is None:
        if _uuid(descriptor_resource) != original_resource:
            raise _NativeDecodeIssue(
                "image-binding-invalid", "Native image resource identity mismatch"
            )
        attachment_id = original_resource
    elif descriptor_resource is not None and descriptor_attachment is not None:
        if _uuid(descriptor_resource) != original_resource:
            raise _NativeDecodeIssue(
                "image-binding-invalid", "Native image resource identity mismatch"
            )
        attachment_id = _uuid(descriptor_attachment)
    else:
        raise _UnsupportedNativeVariant("image-binding-incomplete")
    return NativeImage(attachment_id, x, y, width, height)


def _unsupported_reason(error: Exception) -> str:
    if isinstance(error, _UnsupportedNativeVariant):
        return error.reason
    if isinstance(error, _NativeDecodeIssue):
        return error.reason
    return "decode-failed"


def decode_native_objects(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[DecodedNativeObject], dict[str, object]]:
    """Decode supported annotation payloads while retaining source order metadata."""
    categories: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    objects: dict[tuple[str, str], dict[str, bytes]] = defaultdict(dict)
    for key, value in records:
        try:
            root, category, storage_id, kind = key.decode("ascii").split(".")
            if root != root_uuid or kind not in {"descriptor", "item"}:
                raise ValueError("Unsupported native object key")
            _uuid(storage_id.encode())
            categories[category] += 1
            if kind in objects[(category, storage_id)]:
                raise ValueError("Duplicate native key")
            objects[(category, storage_id)][kind] = value
        except (UnicodeError, ValueError):
            reasons["invalid-record-key"] += 1
    candidates: list[tuple[int, int, str, Sequence[Ink | NativePath | NativeImage]]] = []
    budget = _DecodeBudget()
    logical_counts: Counter[str] = Counter()
    descriptor_variants: Counter[str] = Counter()
    unmodeled_descriptor_fields: set[int] = set()
    unmodeled_empty_body_fields: set[int] = set()
    unmodeled_body_fields: set[int] = set()
    unmodeled_image_fields: set[int] = set()
    unmodeled_tpl_fields: dict[str, set[int]] = defaultdict(set)
    image_body_field_6_absent = 0
    erased = deleted = 0
    for (category, storage_id), pair in objects.items():
        if category == "deleted":
            deleted += 1
            continue
        if category != "normal":
            reasons["unknown-record-category"] += 1
            continue
        try:
            if set(pair) != {"descriptor", "item"}:
                raise ValueError("Missing annotation descriptor/item pair")
            descriptor = decode_message(pair["descriptor"])
            if any(
                item.number not in {1, 2, 3, 4, 6, 7, 8, 9, 14, 16, 20, 21}
                for item in descriptor.fields
            ):
                raise ValueError("Unsupported native descriptor field")
            logical_id = _uuid(_one(descriptor, 1, bytes))
            logical_counts[logical_id] += 1
            alias = _one(descriptor, 6, bytes, required=False)
            bound_id = _uuid(alias) if alias is not None else logical_id
            if bound_id != storage_id.upper():
                raise ValueError("Annotation storage identity mismatch")
            metadata_fields = descriptor.by_number(14)
            if metadata_fields:
                if (
                    len(metadata_fields) != 1
                    or metadata_fields[0].wire_type != WireType.VARINT
                    or not isinstance(metadata_fields[0].value, int)
                    or not 0 <= metadata_fields[0].value <= (1 << 64) - 1
                ):
                    raise ValueError("Unsupported native descriptor metadata")
                unmodeled_descriptor_fields.add(14)
            is_erased = _one(descriptor, 3, int, required=False)
            if is_erased not in {None, 0, 1}:
                raise ValueError("Unknown erased flag")
            if is_erased == 1:
                erased += 1
                continue
            object_type = _one(descriptor, 16, int)
            if object_type not in {24, 25}:
                reasons["unsupported-object-type"] += 1
                continue
            if descriptor.by_number(20) or descriptor.by_number(21):
                reasons["unsupported-parent-transform"] += 1
                continue
            actor = cast(int, _one(descriptor, 8, int))
            sequence = cast(int, _one(descriptor, 9, int))
            item = decode_message(pair["item"])
            item_numbers = {field.number for field in item.fields}
            if item_numbers == {1}:
                if object_type != 24:
                    raise ValueError("Unsupported non-ink native image type")
                image_body = decode_message(cast(bytes, _one(item, 1, bytes)))
                image = _decode_native_image(descriptor, item, logical_id)
                unmodeled_image_fields.update({5, 15})
                if image_body.by_number(6):
                    unmodeled_image_fields.add(6)
                else:
                    image_body_field_6_absent += 1
                descriptor_variants[
                    "image-attachment-alias" if descriptor.by_number(7) else "direct-image-resource"
                ] += 1
                candidates.append((actor, sequence, logical_id, [image]))
            elif item_numbers == {7}:
                body = decode_message(cast(bytes, _one(item, 7, bytes)))
                if _uuid(_one(body, 1, bytes)) != logical_id:
                    raise ValueError("Annotation logical identity mismatch")
                if descriptor.by_number(4) or descriptor.by_number(7):
                    raise ValueError("Image attachment binding on a non-image object")
                body_variant = _one(body, 3, int, required=False)
                if object_type == 25:
                    payloads: list[Ink | NativePath | NativeImage] = [
                        *_decode_variable_brush(body, budget)
                    ]
                    unmodeled_body_fields.add(3)
                    unmodeled_tpl_fields["variable-brush"].update({0, 3, 4, 8, 9})
                elif body_variant == 1:
                    payloads = [_decode_native_path(body, budget)]
                    unmodeled_body_fields.add(3)
                    unmodeled_body_fields.add(20)
                    unmodeled_empty_body_fields.add(20)
                    unmodeled_tpl_fields["native-path"].update({2, 3})
                elif _one(body, 9, bytes, required=False) not in {None, b""}:
                    payloads = [_decode_native_shape(body, budget)]
                    unmodeled_body_fields.add(9)
                else:
                    payloads = [*decode_ink(body, budget)]
                    if body_variant is not None:
                        unmodeled_body_fields.add(3)
                if body.by_number(20):
                    unmodeled_empty_body_fields.add(20)
                variant = "storage-alias" if alias is not None else "direct-identity"
                descriptor_variants[variant] += 1
                candidates.append((actor, sequence, logical_id, payloads))
            else:
                raise ValueError("Unsupported native annotation item envelope")
        except (ValueError, UnicodeError, IndexError, TypeError, struct.error) as exc:
            reasons[_unsupported_reason(exc)] += 1
    duplicate_ids = {logical_id for logical_id, count in logical_counts.items() if count > 1}
    if duplicate_ids:
        reasons["ambiguous-logical-version"] += sum(
            1 for row in candidates if row[2] in duplicate_ids
        )
        candidates = [row for row in candidates if row[2] not in duplicate_ids]
    actors = {row[0] for row in candidates}
    creation_clocks = Counter((row[0], row[1]) for row in candidates)
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
    if deleted:
        warnings.append(
            "Deleted-category objects were excluded. Their application visibility was "
            "not independently verified."
        )
    if unmodeled_descriptor_fields:
        warnings.append(
            "Native descriptor field 14 is preserved as opaque metadata. Its effect on "
            "application rendering is unverified, so reconstruction coverage is partial."
        )
    if unmodeled_body_fields:
        warnings.append(
            "Observed native body fields "
            + ", ".join(str(number) for number in sorted(unmodeled_body_fields))
            + " include values whose independent rendering effect is unverified."
        )
    if unmodeled_image_fields:
        warnings.append(
            "Native image body fields "
            + ", ".join(str(number) for number in sorted(unmodeled_image_fields))
            + " are preserved as opaque metadata. Image bytes are resolved separately from "
            "the copied attachment store."
        )
    if image_body_field_6_absent:
        warnings.append(
            "Some decoded native image records omit body field 6; its effect on "
            "application appearance or visibility is unverified."
        )
    if unmodeled_tpl_fields:
        warnings.append(
            "Validated native template arrays "
            + ", ".join(sorted(unmodeled_tpl_fields))
            + " contain additional opaque samples that do not drive reconstructed geometry."
        )
    if reasons:
        warnings.append(
            "Some native records could not be reconstructed. Do not treat this image "
            "as complete evidence of all page annotations."
        )
    ordered_objects = [
        DecodedNativeObject(actor, sequence, logical_id, payload, part_index)
        for actor, sequence, logical_id, payloads in sorted(
            candidates, key=lambda row: (row[1], row[0], row[2])
        )
        for part_index, payload in enumerate(payloads)
    ]
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
            if reasons
            or deleted
            or len(actors) > 1
            or order_ties
            or unmodeled_descriptor_fields
            or unmodeled_body_fields
            or unmodeled_image_fields
            or image_body_field_6_absent
            or unmodeled_tpl_fields
            else "supported-native-records"
            if image_count or path_count
            else "supported-pen-records"
        ),
        "full_fidelity_verified": False,
        "page_mapping": "projection-uuid-plus-one-observed",
        "native_records": len(records),
        "native_objects": len(objects),
        "rendered_objects": len(candidates),
        "rendered_strokes": stroke_count,
        "rendered_paths": path_count,
        "rendered_images": image_count,
        "erased_objects": erased,
        "deleted_objects": deleted,
        "unsupported_objects": sum(reasons.values()),
        "unsupported_reasons": dict(reasons),
        "categories": dict(categories),
        "descriptor_variants": dict(descriptor_variants),
        "unmodeled_descriptor_fields": sorted(unmodeled_descriptor_fields),
        "unmodeled_empty_body_fields": sorted(unmodeled_empty_body_fields),
        "unmodeled_body_fields": sorted(unmodeled_body_fields),
        "unmodeled_image_fields": sorted(unmodeled_image_fields),
        "native_image_body_field_6_absent_records": image_body_field_6_absent,
        "unmodeled_tpl_fields": {
            name: sorted(field_numbers)
            for name, field_numbers in sorted(unmodeled_tpl_fields.items())
        },
        "unmodeled_observed_envelope_fields": [7, 15],
        "warnings": warnings,
    }
    return ordered_objects, metadata


def decode_native_records(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[Ink], dict[str, object]]:
    """Compatibility view returning only decoded pen/highlighter strokes."""
    objects, metadata = decode_native_objects(records, root_uuid)
    return [obj.payload for obj in objects if isinstance(obj.payload, Ink)], metadata
