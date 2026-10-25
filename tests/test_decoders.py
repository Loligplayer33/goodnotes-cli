from __future__ import annotations

import struct

from goodnotes_cli._vendor.compression import _decode_lz4_block, decode_apple_lz4
from goodnotes_cli._vendor.tpl import decode_tpl


def _tpl_image(format_string: str, values: bytes) -> bytes:
    contents = format_string.encode("ascii") + b"\0" + values
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


def test_lz4_backreference_copies_nonoverlapping_prior_output() -> None:
    # Four literals followed by a four-byte match at offset four.
    assert _decode_lz4_block(b"\x40abcd\x04\x00", 8, b"") == b"abcdabcd"


def test_lz4_overlapping_match_repeats_its_seed_as_rle() -> None:
    # One literal followed by a 19-byte offset-one match.
    source = b"\x1fA\x01\x00\x00"
    assert _decode_lz4_block(source, 20, b"") == b"A" * 20


def test_lz4_extended_literal_and_match_lengths() -> None:
    literals = b"abcdefghijklmnopq"
    assert _decode_lz4_block(b"\xf0\x02" + literals, 17, b"") == literals

    # 15 + 255 + 10 + 4 = 284 match bytes, overlapping the initial literal.
    extended_match = b"\x1fZ\x01\x00\xff\x0a"
    assert _decode_lz4_block(extended_match, 285, b"") == b"Z" * 285


def test_lz4_dictionary_backreference_and_output_spanning_match() -> None:
    assert _decode_lz4_block(b"\x00\x04\x00", 4, b"abcd") == b"abcd"
    # The offset-three seed starts in the dictionary, crosses into output,
    # then repeats the combined 'abc' pattern.
    assert _decode_lz4_block(b"\x11c\x03\x00", 6, b"ab") == b"cabcab"


def test_apple_lz4_cross_block_backreference_uses_prior_block_dictionary() -> None:
    stored = b"bv4-" + struct.pack("<II", 4, 4) + b"abcd"
    compressed = b"bv41" + struct.pack("<II", 4, 3) + b"\x00\x04\x00"
    decoded, consumed = decode_apple_lz4(stored + compressed + b"bv4$")
    assert decoded == b"abcdabcd"
    assert consumed == len(stored + compressed + b"bv4$")


def test_tpl_unsigned_u_and_double_f_keep_distinct_widths_and_meanings() -> None:
    image = _tpl_image("uf", struct.pack("<Id", 0x3F800000, 1.5))
    assert decode_tpl(image).values == (0x3F800000, 1.5)


def test_tpl_modern_string_consumes_length_minus_one_before_next_field() -> None:
    image = _tpl_image("su", struct.pack("<I", 4) + b"ink!"[:3] + struct.pack("<I", 0x12345678))
    assert decode_tpl(image).values == ("ink", 0x12345678)


def test_tpl_fixed_width_arrays_preserve_unsigned_record_values() -> None:
    values = struct.pack("<I", 2) + struct.pack("<IIII", 1, 2, 0xFFFFFFFF, 4)
    assert decode_tpl(_tpl_image("A(S(uu))", values)).values == ([(1, 2), (0xFFFFFFFF, 4)],)
