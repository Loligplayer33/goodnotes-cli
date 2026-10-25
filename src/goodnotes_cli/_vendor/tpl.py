# ruff: noqa
"""Typed parser for Troy Hanson TPL memory images used by GoodNotes ink."""

from __future__ import annotations

from dataclasses import dataclass
import struct
import sys


class TplDecodeError(ValueError):
    """Raised for malformed or unsupported TPL images."""


if sys.version_info >= (3, 10):
    from typing import TypeAlias

    TplValue: TypeAlias = int | float | bytes | str | tuple["TplValue", ...] | list["TplValue"]
    _FormatNode: TypeAlias = str | tuple[str, tuple["_FormatNode", ...]]
else:
    TplValue = object
    _FormatNode = object


@dataclass(frozen=True)
class TplImage:
    format: str
    flags: int
    values: tuple[TplValue, ...]
    raw: bytes


class _Cursor:
    def __init__(self, data: bytes, position: int) -> None:
        self.data = data
        self.position = position

    def take(self, size: int) -> bytes:
        end = self.position + size
        if end > len(self.data):
            raise TplDecodeError("truncated TPL value")
        value = self.data[self.position : end]
        self.position = end
        return value

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]


_FIXED_FORMATS = {
    "c": "b",
    "j": "h",
    "v": "H",
    "i": "i",
    "u": "I",
    "I": "q",
    "U": "Q",
    "f": "d",
}


def _decode_fixed_array(
    children: tuple[_FormatNode, ...], count: int, cursor: _Cursor
) -> list[TplValue] | None:
    """Use ``struct.iter_unpack`` for arrays of fixed-width scalar records."""
    if len(children) == 1 and isinstance(children[0], str) and children[0] in _FIXED_FORMATS:
        formats = _FIXED_FORMATS[children[0]]
        raw = cursor.take(count * struct.calcsize("<" + formats))
        return [row[0] for row in struct.iter_unpack("<" + formats, raw)]
    if len(children) == 1 and isinstance(children[0], tuple) and children[0][0] == "S":
        fields = children[0][1]
        formats = _fixed_formats(fields) if fields else None
        if formats is not None:
            raw = cursor.take(count * struct.calcsize("<" + formats))
            return [row for row in struct.iter_unpack("<" + formats, raw)]
    formats = _fixed_formats(children) if children else None
    if formats is not None:
        raw = cursor.take(count * struct.calcsize("<" + formats))
        return [row for row in struct.iter_unpack("<" + formats, raw)]
    return None


def _fixed_formats(children: tuple[_FormatNode, ...]) -> str | None:
    """Return the struct format when every node is a fixed-width scalar."""
    formats: list[str] = []
    for child in children:
        if not isinstance(child, str):
            return None
        format_code = _FIXED_FORMATS.get(child)
        if format_code is None:
            return None
        formats.append(format_code)
    return "".join(formats)


def _parse_format(format_string: str) -> tuple[_FormatNode, ...]:
    position = 0

    def group(terminator: str | None = None, depth: int = 0) -> tuple[_FormatNode, ...]:
        nonlocal position
        if depth > 32:
            raise TplDecodeError("TPL format nesting exceeds budget")
        nodes: list[_FormatNode] = []
        while position < len(format_string):
            token = format_string[position]
            position += 1
            if token == ")":
                if terminator is None:
                    raise TplDecodeError("unexpected TPL format closing parenthesis")
                return tuple(nodes)
            if token in "AS":
                if position >= len(format_string) or format_string[position] != "(":
                    raise TplDecodeError(f"TPL {token} requires a parenthesized group")
                position += 1
                nodes.append((token, group(")", depth + 1)))
            elif token in "jviuIUcsfB":
                nodes.append(token)
            else:
                raise TplDecodeError(f"unsupported TPL format token {token!r}")
        if terminator is not None:
            raise TplDecodeError("unclosed TPL format group")
        return tuple(nodes)

    return group()


def _decode_value(node: _FormatNode, cursor: _Cursor) -> TplValue:
    if isinstance(node, tuple):
        kind, children = node
        if kind == "S":
            return tuple(_decode_value(child, cursor) for child in children)
        count = cursor.u32()
        if count > 1_000_000 or count > len(cursor.data) - cursor.position or not children:
            raise TplDecodeError("TPL array count exceeds remaining bytes or budget")
        fixed_values = _decode_fixed_array(children, count, cursor)
        if fixed_values is not None:
            return fixed_values
        if len(children) == 1:
            return [_decode_value(children[0], cursor) for _ in range(count)]
        return [tuple(_decode_value(child, cursor) for child in children) for _ in range(count)]
    if node == "s":
        size = cursor.u32()
        raw = cursor.take(max(size - 1, 0))
        return raw.decode("utf-8", errors="replace")
    if node == "B":
        return cursor.take(cursor.u32())
    raw = cursor.take(struct.calcsize("<" + _FIXED_FORMATS[node]))
    return struct.unpack("<" + _FIXED_FORMATS[node], raw)[0]


def decode_tpl(data: bytes) -> TplImage:
    """Decode a complete TPL image according to its embedded format string."""
    if len(data) < 9 or data[:3] != b"tpl":
        raise TplDecodeError("not a TPL image")
    flags = data[3]
    if flags & 1:
        raise TplDecodeError("big-endian TPL images are not supported")
    image_size = struct.unpack_from("<I", data, 4)[0]
    if image_size != len(data):
        raise TplDecodeError(f"TPL image size mismatch: expected {image_size}, got {len(data)}")
    end = data.find(b"\0", 8)
    if end < 0:
        raise TplDecodeError("unterminated TPL format string")
    try:
        format_string = data[8:end].decode("ascii")
    except UnicodeDecodeError as error:
        raise TplDecodeError("non-ASCII TPL format string") from error
    nodes = _parse_format(format_string)
    cursor = _Cursor(data, end + 1)
    values = tuple(_decode_value(node, cursor) for node in nodes)
    if cursor.position != len(data):
        raise TplDecodeError(f"unconsumed TPL bytes: {len(data) - cursor.position}")
    return TplImage(format_string, flags, values, data)
