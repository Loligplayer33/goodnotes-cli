"""Generated format-level checks with no Goodnotes files or native runtime."""

from __future__ import annotations

import struct

import pytest
from hypothesis import given
from hypothesis import strategies as st

from goodnotes_cli._vendor.compression import (
    CompressionError,
    _decode_lz4_block,
    decode_apple_lz4,
)
from goodnotes_cli._vendor.tpl import TplDecodeError, decode_tpl
from goodnotes_cli._vendor.wire import DecodeError, decode_message


def _varint(value: int) -> bytes:
    groups = max(1, (value.bit_length() + 6) // 7)
    return bytes(
        ((value >> (7 * index)) & 127) | (128 if index < groups - 1 else 0)
        for index in range(groups)
    )


@st.composite
def _wire_field(draw: st.DrawFn) -> tuple[bytes, int, int, int | bytes]:
    number = draw(st.integers(1, (1 << 29) - 1))
    kind = draw(st.sampled_from([0, 1, 2, 5]))
    value: int | bytes
    if kind == 2:
        value = draw(st.binary(max_size=128))
        payload = _varint(len(value)) + value
    else:
        bits = {0: 64, 1: 64, 5: 32}[kind]
        value = draw(st.integers(0, (1 << bits) - 1))
        payload = _varint(value) if kind == 0 else value.to_bytes(bits // 8, "little")
    return _varint(number * 8 + kind) + payload, number, kind, value


@given(st.lists(_wire_field(), max_size=30))
def test_wire_preserves_values_raw_bytes_and_offsets(
    records: list[tuple[bytes, int, int, int | bytes]],
) -> None:
    encoded = b"".join(record[0] for record in records)
    message = decode_message(encoded)
    assert message.raw == encoded
    assert len(message.fields) == len(records)
    offset = 0
    for field, (raw, number, kind, value) in zip(message.fields, records, strict=True):
        assert (field.number, int(field.wire_type), field.value) == (number, kind, value)
        assert field.raw == raw
        assert field.offset == offset
        offset += len(raw)
    assert b"".join(field.raw for field in message.fields) == encoded


@given(st.binary(min_size=1, max_size=128), st.integers(1, (1 << 29) - 1))
def test_wire_rejects_truncated_length_delimited_payload(payload: bytes, number: int) -> None:
    encoded = _varint(number * 8 + 2) + _varint(len(payload)) + payload
    with pytest.raises(DecodeError, match="truncated"):
        decode_message(encoded[:-1])


@given(st.integers(1 << 64, (1 << 70) - 1))
def test_wire_rejects_varints_outside_uint64(value: int) -> None:
    with pytest.raises(DecodeError, match="varint"):
        decode_message(b"\x08" + _varint(value))


@given(st.integers(1, (1 << 29) - 1), st.sampled_from([3, 4, 6, 7]))
def test_wire_rejects_unsupported_wire_types(number: int, kind: int) -> None:
    with pytest.raises(DecodeError):
        decode_message(_varint(number * 8 + kind))


def _extension(length: int) -> bytes:
    return b"\xff" * (length // 255) + bytes([length % 255])


def _literal_block(payload: bytes) -> bytes:
    length = len(payload)
    return (
        bytes([min(length, 15) << 4]) + (_extension(length - 15) if length >= 15 else b"") + payload
    )


@given(
    dictionary=st.binary(max_size=100),
    literals=st.binary(min_size=1, max_size=40),
    match_length=st.integers(4, 600),
    data=st.data(),
)
def test_lz4_matches_simple_bytewise_reference(
    dictionary: bytes, literals: bytes, match_length: int, data: st.DataObject
) -> None:
    offset = data.draw(st.integers(1, len(dictionary) + len(literals)), label="offset")
    token = (min(len(literals), 15) << 4) | min(match_length - 4, 15)
    block = (
        bytes([token])
        + (_extension(len(literals) - 15) if len(literals) >= 15 else b"")
        + literals
        + struct.pack("<H", offset)
        + (_extension(match_length - 19) if match_length >= 19 else b"")
    )
    # Deliberately use a byte-at-a-time oracle instead of the decoder's seed doubling.
    history = bytearray(dictionary + literals)
    for _ in range(match_length):
        history.append(history[-offset])
    expected = bytes(history[len(dictionary) :])
    assert _decode_lz4_block(block, len(expected), dictionary) == expected
    for wrong_size in (len(expected) - 1, len(expected) + 1):
        with pytest.raises(CompressionError):
            _decode_lz4_block(block, wrong_size, dictionary)


@given(st.binary(min_size=1, max_size=40), st.integers(4, 18))
def test_lz4_rejects_zero_and_out_of_range_offsets(literals: bytes, match_length: int) -> None:
    prefix = _literal_block(literals)
    prefix = bytes([prefix[0] | (match_length - 4)]) + prefix[1:]
    for offset in (0, len(literals) + 1):
        with pytest.raises(CompressionError, match="offset"):
            _decode_lz4_block(prefix + struct.pack("<H", offset), len(literals) + match_length, b"")


@given(st.binary(min_size=1, max_size=32), st.integers(65535, 65538))
def test_lz4_handles_dictionary_sizes_around_64k(seed: bytes, size: int) -> None:
    dictionary = (seed * ((size + len(seed) - 1) // len(seed)))[:size]
    offset = min(size, 65535)
    block = b"\x00" + struct.pack("<H", offset)
    assert _decode_lz4_block(block, 4, dictionary) == dictionary[-offset:][:4]


@given(
    st.lists(st.tuples(st.booleans(), st.binary(min_size=1, max_size=300)), min_size=1, max_size=5),
    st.binary(max_size=20),
    st.data(),
)
def test_apple_lz4_mixed_frames_consumption_truncation_and_budget(
    blocks: list[tuple[bool, bytes]], suffix: bytes, data: st.DataObject
) -> None:
    framed = bytearray()
    for compressed, payload in blocks:
        stored = _literal_block(payload) if compressed else payload
        framed += (
            (b"bv41" if compressed else b"bv4-")
            + struct.pack("<II", len(payload), len(stored))
            + stored
        )
    framed += b"bv4$"
    encoded = bytes(framed)
    expected = b"".join(payload for _, payload in blocks)
    assert decode_apple_lz4(encoded + suffix) == (expected, len(encoded))
    assert decode_apple_lz4(encoded, max_output=len(expected)) == (expected, len(encoded))
    with pytest.raises(CompressionError, match="budget"):
        decode_apple_lz4(encoded, max_output=len(expected) - 1)
    cut = data.draw(st.integers(0, len(encoded) - 1), label="truncation")
    with pytest.raises(CompressionError):
        decode_apple_lz4(encoded[:cut])


def _tpl_image(format_string: str, payload: bytes) -> bytes:
    body = format_string.encode("ascii") + b"\0" + payload
    return b"tpl\0" + struct.pack("<I", len(body) + 8) + body


@pytest.mark.parametrize(
    ("token", "packing", "minimum", "maximum"),
    [
        ("c", "b", -(1 << 7), (1 << 7) - 1),
        ("j", "h", -(1 << 15), (1 << 15) - 1),
        ("v", "H", 0, (1 << 16) - 1),
        ("i", "i", -(1 << 31), (1 << 31) - 1),
        ("u", "I", 0, (1 << 32) - 1),
        ("I", "q", -(1 << 63), (1 << 63) - 1),
        ("U", "Q", 0, (1 << 64) - 1),
    ],
)
@given(data=st.data())
def test_tpl_integer_arrays_preserve_width_and_sign(
    token: str, packing: str, minimum: int, maximum: int, data: st.DataObject
) -> None:
    values = data.draw(st.lists(st.integers(minimum, maximum), max_size=30), label="values")
    payload = struct.pack("<I", len(values)) + b"".join(
        struct.pack("<" + packing, v) for v in values
    )
    image = _tpl_image(f"A({token})", payload)
    decoded = decode_tpl(image)
    assert decoded.values == (values,)
    assert decoded.raw == image
    assert decoded.format == f"A({token})"
    assert decoded.flags == 0


@given(st.lists(st.lists(st.integers(0, (1 << 32) - 1), max_size=8), max_size=8))
def test_tpl_nested_arrays_preserve_group_boundaries(values: list[list[int]]) -> None:
    payload = struct.pack("<I", len(values))
    for row in values:
        payload += struct.pack("<I", len(row)) + b"".join(struct.pack("<I", v) for v in row)
    assert decode_tpl(_tpl_image("A(A(u))", payload)).values == (values,)


@given(
    text=st.text(alphabet=st.characters(exclude_categories=["Cs"]), max_size=40),
    binary=st.binary(max_size=80),
    number=st.floats(allow_nan=False, allow_infinity=False, width=64),
    sentinel=st.integers(0, (1 << 32) - 1),
)
def test_tpl_variable_length_fields_and_double_preserve_following_field(
    text: str, binary: bytes, number: float, sentinel: int
) -> None:
    utf8 = text.encode("utf-8")
    payload = (
        struct.pack("<I", len(utf8) + 1)
        + utf8
        + struct.pack("<I", len(binary))
        + binary
        + struct.pack("<dI", number, sentinel)
    )
    decoded = decode_tpl(_tpl_image("sBfu", payload))
    assert decoded.values == (text, binary, number, sentinel)
    # Check signed zero and exact double bits as well as numeric equality.
    assert struct.pack("<d", decoded.values[2]) == struct.pack("<d", number)


@given(
    st.lists(
        st.tuples(st.integers(0, (1 << 32) - 1), st.integers(0, (1 << 32) - 1)),
        min_size=1,
        max_size=20,
    )
)
def test_tpl_records_reject_truncated_payload_with_corrected_header(
    values: list[tuple[int, int]],
) -> None:
    payload = struct.pack("<I", len(values)) + b"".join(struct.pack("<II", *v) for v in values)
    assert decode_tpl(_tpl_image("A(S(uu))", payload)).values == (values,)
    # Rebuild the size header so this tests payload validation, not just the header.
    with pytest.raises(TplDecodeError, match="truncated"):
        decode_tpl(_tpl_image("A(S(uu))", payload[:-1]))


@given(st.integers(1_000_001, 1_000_032))
def test_tpl_rejects_excessive_array_counts(count: int) -> None:
    with pytest.raises(TplDecodeError, match="count"):
        # Supply enough bytes so only the count budget, not truncation, rejects it.
        decode_tpl(_tpl_image("A(c)", struct.pack("<I", count) + b"x" * count))


@given(st.integers(33, 60))
def test_tpl_rejects_excessive_format_nesting(depth: int) -> None:
    with pytest.raises(TplDecodeError, match="nesting"):
        decode_tpl(_tpl_image("S(" * depth + "u" + ")" * depth, struct.pack("<I", 0)))
