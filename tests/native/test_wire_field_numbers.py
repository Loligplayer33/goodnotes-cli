"""Boundary checks for protobuf field-number diagnostics."""

import pytest

from goodnotes_cli._vendor.wire import DecodeError, decode_message


@pytest.mark.parametrize("data, number", [(b"\x00", 0), (b"\x80\x80\x80\x80\x10", 1 << 29)])
def test_invalid_protobuf_field_number_reports_actual_number(data: bytes, number: int) -> None:
    with pytest.raises(DecodeError, match=rf"field number {number} is outside the valid range"):
        decode_message(data)
