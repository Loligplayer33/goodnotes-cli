"""Conservative rendering of native pen/highlighter geometry from copied records.

The UUID namespace mapping and the supported TPL ink schemas were established
from copied data. This is an approximate reconstruction, not Goodnotes' renderer.
Unknown objects, version conflicts and decoding failures remain visible in the
coverage metadata rather than being reported as an empty or complete page.
"""

from __future__ import annotations

import math
import struct
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from ._vendor.compression import decode_apple_lz4
from ._vendor.tpl import decode_tpl
from ._vendor.wire import Message, WireType, decode_message
from .models import Page
from .native import NativeError, SnapshotProtocol, read_page_records

_INK_FORMATS = {"vuA(v)A(S(uu))A(S(uuuu))", "vuA(v)A(S(uu))A(S(uuuu))vA(f)"}
_COORDINATE_SCALE = 72 / 132


@dataclass(frozen=True)
class NativeOverlay:
    svg: bytes
    metadata: dict[str, object]


@dataclass(frozen=True)
class Ink:
    points: tuple[tuple[float, float], ...]
    width: float
    color: str
    alpha: float
    dash: tuple[float, ...] = ()


@dataclass
class _DecodeBudget:
    bytes_left: int = 16 * 1024 * 1024
    points_left: int = 100_000

    def charge(self, *, byte_count: int = 0, point_count: int = 0) -> None:
        self.bytes_left -= byte_count
        self.points_left -= point_count
        if self.bytes_left < 0 or self.points_left < 0:
            raise NativeError(
                "native_data_unavailable",
                "Native page decoding exceeds its decompressed-byte or point budget.",
                operation="decode_native_page",
            )


def _one(message: Message, field: int, kind: type, *, required: bool = True) -> bytes | int | None:
    fields = message.by_number(field)
    if not fields and not required:
        return None
    if len(fields) != 1 or not isinstance(fields[0].value, kind):
        raise ValueError(f"Missing or invalid protobuf field {field}")
    return fields[0].value


def _uuid(value: bytes | int | None) -> str:
    if not isinstance(value, bytes):
        raise ValueError("Invalid annotation UUID")
    text = value.decode("ascii")
    parsed = UUID(text)
    if str(parsed).upper() != text.upper():
        raise ValueError("Noncanonical annotation UUID")
    return text.upper()


def _float(value: int) -> float:
    return struct.unpack("<f", struct.pack("<I", value))[0]


def _point(x: int, y: int) -> tuple[float, float]:
    point = (_float(x), _float(y))
    if not all(math.isfinite(value) and abs(value) <= 100_000 for value in point):
        raise ValueError("Invalid native ink coordinate")
    return point


def _fixed(message: Message, number: int, default: float) -> float:
    fields = message.by_number(number)
    if not fields:
        return default
    if len(fields) != 1 or fields[0].wire_type != WireType.FIXED32:
        raise ValueError("Invalid native color or translation float")
    value = fields[0].fixed_float()
    if value is None or not math.isfinite(value):
        raise ValueError("Nonfinite native color or translation")
    return value


def _segment_paths(
    starts: list[tuple[float, float]],
    segments: list[tuple[tuple[float, float], tuple[float, float]]],
) -> list[list[tuple[float, float]]]:
    """Decode only start/segment relationships established by observed records."""
    if not segments:
        return [[point] for point in starts]

    # One start plus the ordered four-u sample stream is the observed stroke
    # representation. Its tuples are not endpoint-connected graph edges.
    # Multiple starts may denote additional subpaths, but their relationship
    # to this stream is not established; do not infer boundaries by proximity.
    if len(starts) > 1:
        raise ValueError("Ambiguous native ink starts and segments relationship")

    def ordered_path(
        initial: tuple[float, float] | None,
        segment_slice: list[tuple[tuple[float, float], tuple[float, float]]],
    ) -> list[tuple[float, float]]:
        points: list[tuple[float, float]] = []

        def append(point: tuple[float, float]) -> None:
            if not points or math.dist(points[-1], point) >= 1e-3:
                points.append(point)

        if initial is not None:
            append(initial)
        for segment_start, segment_end in segment_slice:
            append(segment_start)
            append(segment_end)
        return points

    if len(starts) == 1:
        return [ordered_path(starts[0], segments)]

    if not starts:
        # With no explicit path starts, only a single stored segment has an
        # unambiguous interpretation. Several segments could be one sampled
        # path or multiple paths; do not guess at their relationship.
        if len(segments) > 1:
            raise ValueError("Ambiguous native ink segments without start markers")
        return [ordered_path(None, segments)]
    raise ValueError("Ambiguous native ink starts and segments relationship")


def decode_ink(body: Message, budget: _DecodeBudget | None = None) -> list[Ink]:
    """Decode the observed legacy pen schemas, preserving colour and lasso moves."""
    if any(item.number not in {1, 2, 4, 5, 6, 7, 9, 15, 20, 21} for item in body.fields):
        raise ValueError("Unsupported ink body field")
    # These envelope fields were present alongside the supported ink schema.
    # Clocks in7/15 remain opaque. New style/visibility fields must not silently
    # acquire guessed semantics.
    for number in (7, 15):
        _one(body, number, bytes, required=False)
    if _one(body, 5, int, required=False) not in {None, 1}:
        raise ValueError("Unsupported ink style flag")
    if _one(body, 9, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary geometry")
    if _one(body, 20, bytes, required=False) not in {None, b""}:
        raise ValueError("Unsupported ink auxiliary body field")
    if _one(body, 21, int, required=False) not in {None, 24}:
        raise ValueError("Unsupported ink body type")
    payload = cast(bytes, _one(body, 2, bytes))
    if not payload.startswith((b"bv41", b"bv4-")):
        raise ValueError("Unsupported ink compression")
    budget = budget or _DecodeBudget()
    if budget.bytes_left <= 0:
        budget.charge(byte_count=1)
    decompressed, consumed = decode_apple_lz4(
        payload, max_output=min(4 * 1024 * 1024, budget.bytes_left)
    )
    budget.charge(byte_count=len(decompressed))
    if consumed != len(payload):
        raise ValueError("Unconsumed compressed ink bytes")
    format_end = decompressed.find(b"\0", 8, 256)
    if format_end < 0 or decompressed[8:format_end] not in {item.encode() for item in _INK_FORMATS}:
        raise ValueError("Unsupported ink TPL schema")
    image = decode_tpl(decompressed)
    values = image.values
    width = _float(cast(int, values[1]))
    if not math.isfinite(width) or not 0.05 <= width <= 100:
        raise ValueError("Invalid native pen width")
    flags = cast(list[int], values[2])
    starts = cast(list[tuple[int, int]], values[3])
    segments = cast(list[tuple[int, int, int, int]], values[4])
    budget.charge(point_count=len(starts) + len(segments) * 2)
    if any(value not in {0, 1} for value in flags):
        raise ValueError("Unsupported native visibility flags")
    if flags and len(flags) != len(segments) + 1:
        raise ValueError("Native visibility flags do not match segments")
    if any(value == 0 for value in flags[1:]):
        raise ValueError("Uncertain native ink visibility pattern")
    color_payload = _one(body, 4, bytes, required=False)
    color_message = decode_message(color_payload) if isinstance(color_payload, bytes) else None
    if color_message and any(item.number not in {1, 2, 3, 4} for item in color_message.fields):
        raise ValueError("Unsupported ink color field")
    channels = (
        [_fixed(color_message, index, 0.0) for index in (1, 2, 3)]
        if color_message
        else [0.0, 0.0, 0.0]
    )
    alpha = _fixed(color_message, 4, 1.0) if color_message else 1.0
    if any(not 0 <= value <= 1 for value in [*channels, alpha]):
        raise ValueError("Native RGBA value is outside 0..1")
    color = "#" + "".join(f"{round(channel * 255):02x}" for channel in channels)
    offset = _one(body, 6, bytes, required=False)
    offset_message = decode_message(offset) if isinstance(offset, bytes) and offset else None
    if offset_message and any(item.number not in {1, 2} for item in offset_message.fields):
        raise ValueError("Unsupported ink transform field")
    dx = _fixed(offset_message, 1, 0.0) if offset_message else 0.0
    dy = _fixed(offset_message, 2, 0.0) if offset_message else 0.0
    if abs(dx) > 100_000 or abs(dy) > 100_000:
        raise ValueError("Native translation exceeds coordinate bounds")
    start_points = [_point(*start) for start in starts]
    segment_points = [
        (_point(segment[0], segment[1]), _point(segment[2], segment[3])) for segment in segments
    ]
    groups = _segment_paths(start_points, segment_points)
    dash: tuple[float, ...] = ()
    if len(values) == 7:
        candidate = cast(list[float], values[6])
        if candidate:
            if any(not math.isfinite(value) or value < 0 or value > 10_000 for value in candidate):
                raise ValueError("Unsupported native dash pattern")
            if any(candidate):
                dash = tuple(candidate)
    result = [
        Ink(tuple((x + dx, y + dy) for x, y in group), width, color, alpha, dash)
        for group in groups
        if group
    ]
    if not result:
        raise ValueError("Ink has no decoded points")
    return result


def _svg(inks: list[Ink], width: float, height: float, scale: float) -> bytes:
    elements = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.6f}pt" '
        f'height="{height:.6f}pt" viewBox="0 0 {width:.6f} {height:.6f}">'
    ]
    for ink in inks:
        points = ink.points
        if len(points) == 1:
            x, y = points[0]
            elements.append(
                f'<circle cx="{x * scale:.6f}" cy="{y * scale:.6f}" '
                f'r="{ink.width * scale / 2:.6f}" fill="{ink.color}" '
                f'fill-opacity="{ink.alpha:.6f}"/>'
            )
        else:
            coordinates = " ".join(f"{x * scale:.6f},{y * scale:.6f}" for x, y in points)
            dash = (
                (
                    ' stroke-dasharray="'
                    + " ".join(f"{value * scale:.6f}" for value in ink.dash)
                    + '"'
                )
                if ink.dash
                else ""
            )
            elements.append(
                f'<polyline points="{coordinates}" fill="none" '
                f'stroke="{ink.color}" stroke-opacity="{ink.alpha:.6f}" '
                f'stroke-width="{ink.width * scale:.6f}" stroke-linecap="round" '
                f'stroke-linejoin="round"{dash}/>'
            )
    elements.append("</svg>")
    return "\n".join(elements).encode()


def decode_native_records(
    records: list[tuple[bytes, bytes]], root_uuid: str
) -> tuple[list[Ink], dict[str, object]]:
    """Decode paired records and report every object which was not rendered."""
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
    candidates: list[tuple[int, int, str, list[Ink]]] = []
    budget = _DecodeBudget()
    logical_counts: Counter[str] = Counter()
    descriptor_variants: Counter[str] = Counter()
    unmodeled_descriptor_fields: set[int] = set()
    unmodeled_empty_body_fields: set[int] = set()
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
                item.number not in {1, 2, 3, 6, 8, 9, 14, 16, 20, 21} for item in descriptor.fields
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
            if _one(descriptor, 16, int) != 24:
                reasons["unsupported-object-type"] += 1
                continue
            if descriptor.by_number(20) or descriptor.by_number(21):
                reasons["unsupported-parent-transform"] += 1
                continue
            item = decode_message(pair["item"])
            if any(field.number != 7 for field in item.fields):
                raise ValueError("Unsupported annotation item envelope")
            body = decode_message(cast(bytes, _one(item, 7, bytes)))
            if _uuid(_one(body, 1, bytes)) != logical_id:
                raise ValueError("Annotation logical identity mismatch")
            actor = cast(int, _one(descriptor, 8, int))
            sequence = cast(int, _one(descriptor, 9, int))
            candidates.append((actor, sequence, logical_id, decode_ink(body, budget)))
            descriptor_variants["storage-alias" if alias is not None else "direct-identity"] += 1
            if body.by_number(20):
                unmodeled_empty_body_fields.add(20)
        except (ValueError, UnicodeError, IndexError, TypeError, struct.error) as exc:
            if "Unsupported ink TPL schema" in str(exc):
                reason = "unsupported-ink-schema"
            elif "Uncertain native ink visibility" in str(exc):
                reason = "visibility-uncertain"
            else:
                reason = "decode-failed"
            reasons[reason] += 1
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
        "Native annotations are an approximate reconstruction. Pencil pressure, shapes, "
        "images, text boxes and application drawing order are not fully supported.",
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
    if reasons:
        warnings.append(
            "Some native records could not be reconstructed. Do not treat this image "
            "as complete evidence of all page annotations."
        )
    inks = [
        ink
        for row in sorted(candidates, key=lambda row: (row[1], row[0], row[2]))
        for ink in row[3]
    ]
    metadata: dict[str, object] = {
        "annotation_status": "native-reconstructed-approximate",
        "source": "copied-native-records",
        "coverage": (
            "partial"
            if reasons or deleted or len(actors) > 1 or order_ties or unmodeled_descriptor_fields
            else "supported-pen-records"
        ),
        "full_fidelity_verified": False,
        "page_mapping": "projection-uuid-plus-one-observed",
        "native_records": len(records),
        "native_objects": len(objects),
        "rendered_objects": len(candidates),
        "rendered_strokes": len(inks),
        "erased_objects": erased,
        "deleted_objects": deleted,
        "unsupported_objects": sum(reasons.values()),
        "unsupported_reasons": dict(reasons),
        "categories": dict(categories),
        "descriptor_variants": dict(descriptor_variants),
        "unmodeled_descriptor_fields": sorted(unmodeled_descriptor_fields),
        "unmodeled_empty_body_fields": sorted(unmodeled_empty_body_fields),
        "unmodeled_observed_envelope_fields": [7, 15],
        "warnings": warnings,
    }
    return inks, metadata


def render_native_overlay(
    sandbox: SnapshotProtocol,
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
        raise NativeError(
            "native_data_unavailable",
            "No matching native page records are available "
            "in this copy. This does not establish that the page has no ink.",
            operation="read_native_page",
        )
    inks, metadata = decode_native_records(records, root_uuid)
    if not inks and metadata["unsupported_objects"]:
        raise NativeError(
            "native_data_unavailable",
            "The copied page has native records, but none "
            "could be rendered by the supported pen decoder.",
            operation="decode_native_page",
            context={"native_annotations": metadata},
        )
    return NativeOverlay(_svg(inks, width_points, height_points, _COORDINATE_SCALE), metadata)
