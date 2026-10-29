from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from .errors import NativeError

# Cumulative record limits are independent of per-payload and raster budgets.
_MAX_PAGE_DECODE_BYTES = 16 * 1024 * 1024
_MAX_PAGE_DECODE_POINTS = 100_000

_UnsupportedReason = Literal[
    "unsupported-native-image-field-6",
    "image-binding-incomplete",
    "unsupported-native-geometry-envelope",
    "unsupported-native-geometry-field-6",
    "unsupported-native-geometry-point-count",
]


class _NativeDecodeIssue(ValueError):
    """A decode failure with a stable coverage classification."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(message)
        self.reason = reason


class _UnsupportedNativeVariant(ValueError):
    """A recognized but unsupported variant with a static public reason code."""

    def __init__(self, reason: _UnsupportedReason) -> None:
        super().__init__()
        self.reason = reason


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
    widths: tuple[float, ...] = ()
    quadratic: bool = False


@dataclass(frozen=True)
class NativePathCommand:
    op: Literal["M", "L", "Q", "C", "A", "Z"]
    values: tuple[float, ...] = ()


@dataclass(frozen=True)
class NativePattern:
    attachment_id: str
    x: float
    y: float


@dataclass(frozen=True)
class NativePath:
    commands: tuple[NativePathCommand, ...]
    fill_color: str | None
    fill_alpha: float
    fill_rule: Literal["nonzero"] = "nonzero"
    stroke_color: str | None = None
    stroke_width: float = 0.0
    stroke_alpha: float = 1.0
    pattern: NativePattern | None = None


@dataclass(frozen=True)
class NativeImage:
    attachment_id: str
    x: float
    y: float
    width: float
    height: float
    rotation: float = 0.0


NativePayload = Ink | NativePath | NativeImage


@dataclass(frozen=True)
class DecodedNativeObject:
    actor: int
    sequence: int
    logical_id: str
    payload: NativePayload
    part_index: int = 0


@dataclass
class _DecodeBudget:
    bytes_left: int = _MAX_PAGE_DECODE_BYTES
    points_left: int = _MAX_PAGE_DECODE_POINTS

    def charge(self, *, byte_count: int = 0, point_count: int = 0) -> None:
        self.bytes_left -= byte_count
        self.points_left -= point_count
        if self.bytes_left < 0 or self.points_left < 0:
            raise NativeError(
                "native_data_unavailable",
                "Native page decoding exceeds its decompressed-byte or point budget.",
                operation="decode_native_page",
            )
