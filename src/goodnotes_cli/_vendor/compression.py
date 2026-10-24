# ruff: noqa
# mypy: ignore-errors
"""Decoder for Apple's framed LZ4 stream (``bv41`` / ``bv4$``)."""

from __future__ import annotations

import struct

_MAX_OUTPUT = 16 * 1024 * 1024


class CompressionError(ValueError):
    """Raised when an Apple LZ4 stream is malformed."""


def _decode_lz4_block(source: bytes, expected_size: int, dictionary: bytes) -> bytes:
    """Decode a standard LZ4 block, using at most 64 KiB of prior output."""
    output = bytearray()
    position = 0
    history = dictionary[-65536:]
    while position < len(source):
        token = source[position]
        position += 1
        literal_length = token >> 4
        if literal_length == 15:
            while True:
                if position >= len(source):
                    raise CompressionError("truncated LZ4 literal length")
                extension = source[position]
                position += 1
                literal_length += extension
                if extension != 255:
                    break
        if position + literal_length > len(source):
            raise CompressionError("truncated LZ4 literals")
        if len(output) + literal_length > expected_size:
            raise CompressionError("LZ4 output exceeds declared size")
        output.extend(source[position : position + literal_length])
        position += literal_length
        if position == len(source):
            break
        if position + 2 > len(source):
            raise CompressionError("truncated LZ4 match offset")
        offset = int.from_bytes(source[position : position + 2], "little")
        position += 2
        if offset == 0 or offset > len(history) + len(output):
            raise CompressionError("invalid LZ4 match offset")
        match_length = token & 15
        if match_length == 15:
            while True:
                if position >= len(source):
                    raise CompressionError("truncated LZ4 match length")
                extension = source[position]
                position += 1
                match_length += extension
                if extension != 255:
                    break
        match_length += 4
        if len(output) + match_length > expected_size:
            raise CompressionError("LZ4 output exceeds declared size")
        for _ in range(match_length):
            index = len(history) + len(output) - offset
            output.append(history[index] if index < len(history) else output[index - len(history)])
    if len(output) != expected_size:
        raise CompressionError(f"LZ4 size mismatch: expected {expected_size}, got {len(output)}")
    return bytes(output)


def decode_apple_lz4(data: bytes, *, max_output: int = _MAX_OUTPUT) -> tuple[bytes, int]:
    """Decode an Apple framed LZ4 payload and return (output, bytes_consumed).

    The frame is a sequence of explicit ``bv41`` compressed or ``bv4-`` stored
    blocks, terminated by ``bv4$``. This is a format decoder, not float scanning.
    """
    output = bytearray()
    position = 0
    blocks = 0
    while True:
        if position + 4 > len(data):
            raise CompressionError("missing Apple LZ4 end marker")
        magic = data[position : position + 4]
        position += 4
        if magic == b"bv4$":
            return bytes(output), position
        if magic not in (b"bv41", b"bv4-"):
            raise CompressionError(f"unexpected Apple LZ4 block magic {magic!r}")
        blocks += 1
        if blocks > 4096:
            raise CompressionError("Apple LZ4 block count exceeds budget")
        if position + 8 > len(data):
            raise CompressionError("truncated Apple LZ4 block header")
        uncompressed_size, stored_size = struct.unpack_from("<II", data, position)
        position += 8
        if uncompressed_size == 0:
            raise CompressionError("Apple LZ4 block makes no output progress")
        if uncompressed_size > min(_MAX_OUTPUT, max_output) - len(output):
            raise CompressionError("Apple LZ4 output exceeds the decompression budget")
        if position + stored_size > len(data):
            raise CompressionError("truncated Apple LZ4 block")
        block = data[position : position + stored_size]
        position += stored_size
        if magic == b"bv4-":
            if len(block) != uncompressed_size:
                raise CompressionError("stored Apple LZ4 block size mismatch")
            output.extend(block)
        else:
            output.extend(_decode_lz4_block(block, uncompressed_size, bytes(output[-65536:])))
