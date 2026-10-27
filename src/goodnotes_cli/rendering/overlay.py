from __future__ import annotations

import math
from collections import Counter
from collections.abc import Callable
from typing import Protocol, cast
from uuid import UUID

from ..models import Page
from ..native import NativeError, SnapshotProtocol, read_page_records
from ..native.assets import AttachmentSandbox, NativeImageData, load_native_image
from ..native.objects import decode_native_objects
from ..native.records import DecodedNativeObject, NativeImage, NativeOverlay
from ..rendering.ink_svg import _svg

_COORDINATE_SCALE = 72 / 132

_MAX_PAGE_IMAGE_BYTES = 16 * 1024 * 1024

_MAX_PAGE_IMAGE_PIXELS = 25_000_000


def _resolve_native_images(
    sandbox: AttachmentSandbox,
    page: Page,
    objects: list[DecodedNativeObject],
) -> tuple[list[DecodedNativeObject], dict[str, NativeImageData], Counter[str]]:
    resolved: dict[str, NativeImageData] = {}
    unavailable: set[str] = set()
    over_budget: set[str] = set()
    included: list[DecodedNativeObject] = []
    failures: Counter[str] = Counter()
    total_bytes = total_pixels = 0
    for native_object in objects:
        image = native_object.payload
        if not isinstance(image, NativeImage):
            included.append(native_object)
            continue
        attachment_id = image.attachment_id
        if attachment_id in unavailable:
            failures["native-image-unavailable"] += 1
            continue
        if attachment_id in over_budget:
            failures["native-image-budget-exceeded"] += 1
            continue
        raster = resolved.get(attachment_id)
        if raster is None:
            try:
                raster = load_native_image(sandbox, page.document_id, attachment_id)
            except NativeError:
                unavailable.add(attachment_id)
                failures["native-image-unavailable"] += 1
                continue

        next_bytes = total_bytes + len(raster.data)
        next_pixels = total_pixels + raster.width * raster.height
        if next_bytes > _MAX_PAGE_IMAGE_BYTES or next_pixels > _MAX_PAGE_IMAGE_PIXELS:
            over_budget.add(attachment_id)
            failures["native-image-budget-exceeded"] += 1
            continue
        total_bytes, total_pixels = next_bytes, next_pixels
        resolved[attachment_id] = raster
        included.append(native_object)
    return included, resolved, failures


def _record_image_failures(metadata: dict[str, object], failures: Counter[str]) -> None:
    failed_count = sum(failures.values())
    if not failed_count:
        return
    metadata["rendered_objects"] = max(
        0, cast(int, metadata.get("rendered_objects", 0)) - failed_count
    )
    metadata["rendered_images"] = max(
        0, cast(int, metadata.get("rendered_images", 0)) - failed_count
    )
    metadata["unsupported_objects"] = (
        cast(int, metadata.get("unsupported_objects", 0)) + failed_count
    )
    reasons = metadata.get("unsupported_reasons")
    updated_reasons = dict(reasons) if isinstance(reasons, dict) else {}
    for reason, count in failures.items():
        updated_reasons[reason] = int(updated_reasons.get(reason, 0)) + count
    metadata["unsupported_reasons"] = updated_reasons
    warnings = metadata.get("warnings")
    updated_warnings = list(warnings) if isinstance(warnings, list) else []
    updated_warnings.append(
        "Some copied native images were unavailable or exceeded the page image budget. "
        "Their pixels were omitted from this partial reconstruction."
    )
    metadata["warnings"] = updated_warnings
    metadata["coverage"] = "partial"


class OverlaySandbox(SnapshotProtocol, AttachmentSandbox, Protocol):
    """Snapshot paths required for records and, when present, image attachments."""


def render_native_overlay(
    sandbox: OverlaySandbox,
    page: Page,
    *,
    width_points: float,
    height_points: float,
    record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None = None,
) -> NativeOverlay:
    if page.rotation != 0:
        raise NativeError(
            "native_data_unavailable",
            "Native rendering does not support rotated Goodnotes pages yet.",
            operation="map_native_page",
        )
    if page.width is None or page.height is None:
        raise NativeError(
            "native_data_unavailable",
            "Native rendering requires the copied page's coordinate dimensions.",
            operation="map_native_page",
        )
    dimensions = [page.width, page.height, width_points, height_points]
    if any(
        isinstance(value, bool) or not math.isfinite(value) or value <= 0 for value in dimensions
    ):
        raise NativeError(
            "native_data_unavailable",
            "Invalid native page dimensions.",
            operation="map_native_page",
        )
    scale_x, scale_y = width_points / page.width, height_points / page.height
    if abs(scale_x - _COORDINATE_SCALE) > 0.0001 or abs(scale_y - _COORDINATE_SCALE) > 0.0001:
        raise NativeError(
            "native_data_unavailable",
            "PDF and native coordinate dimensions do not match the verified scale.",
            operation="map_native_page",
        )
    try:
        root_uuid = str(UUID(int=UUID(page.id).int + 1)).upper()
    except (ValueError, AttributeError) as exc:
        raise NativeError(
            "native_data_unavailable",
            "Invalid native page UUID mapping.",
            operation="map_native_page",
        ) from exc
    records = (
        read_page_records(sandbox, root_uuid) if record_reader is None else record_reader(root_uuid)
    )
    if not records:
        # A successful lookup is distinct from an unavailable store or failed read.
        # Empty pages can legitimately have no object records in the observed namespace.
        # Preserve that observation without claiming that the app page has no annotations.
        _, metadata = decode_native_objects(records, root_uuid)
        metadata.update(
            annotation_status="native-no-records-observed",
            coverage="no-native-records-observed",
            warnings=[
                "No native records matched this page in the copied store. The image contains "
                "its copied background; absence of annotations in Goodnotes is unverified."
            ],
        )
        return NativeOverlay(b"", metadata)
    objects, metadata = decode_native_objects(records, root_uuid)
    objects, image_data, image_failures = _resolve_native_images(sandbox, page, objects)
    _record_image_failures(metadata, image_failures)
    image_integrity = Counter(
        image_data[native_object.payload.attachment_id].integrity
        for native_object in objects
        if isinstance(native_object.payload, NativeImage)
    )
    if image_integrity:
        metadata["image_integrity"] = dict(image_integrity)
        if image_integrity["decoder-unverified"]:
            metadata["coverage"] = "partial"
            cast(list[str], metadata["warnings"]).append(
                "JPEG pixel integrity is unverified; the decoder can repair damaged scan data."
            )
    if not objects and metadata["unsupported_objects"]:
        raise NativeError(
            "native_data_unavailable",
            "The copied page has native records, but none could be rendered by the "
            "supported native renderer.",
            operation="decode_native_page",
            context={"native_annotations": metadata},
        )
    try:
        svg = _svg(
            objects,
            width_points,
            height_points,
            _COORDINATE_SCALE,
            image_data=image_data,
        )
    except (ValueError, TypeError) as exc:
        raise NativeError(
            "native_data_unavailable",
            "A decoded native object could not be rendered safely.",
            operation="render_native_page",
            context={"native_annotations": metadata},
        ) from exc
    return NativeOverlay(svg, metadata)
