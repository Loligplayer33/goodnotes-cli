"""Independent retained-work oracles cover generic and optimized TPL decoding."""

from __future__ import annotations

import struct

import pytest

from goodnotes_cli._vendor import tpl


def _image(format_string: str, payload: bytes) -> bytes:
    contents = format_string.encode("ascii") + b"\0" + payload
    return b"tpl\0" + struct.pack("<I", len(contents) + 8) + contents


@pytest.mark.parametrize("generic", [False, True])
@pytest.mark.parametrize("count", [0, 1, 3])
@pytest.mark.parametrize(
    ("format_string", "fields", "base", "per_record"),
    [("A(u)", 1, 6, 1), ("A(S(u))", 1, 8, 2), ("A(uu)", 2, 7, 3), ("A(S(uu))", 2, 9, 3)],
)
def test_fixed_and_generic_arrays_use_the_same_exact_work_oracle(
    monkeypatch: pytest.MonkeyPatch,
    generic: bool,
    count: int,
    format_string: str,
    fields: int,
    base: int,
    per_record: int,
) -> None:
    if generic:
        monkeypatch.setattr(tpl, "_decode_fixed_array", lambda *_: None)
    rows = [tuple(range(index, index + fields)) for index in range(count)]
    payload = struct.pack("<I", count) + b"".join(
        struct.pack("<" + "I" * fields, *row) for row in rows
    )
    image = _image(format_string, payload)
    expected = [row[0] for row in rows] if format_string == "A(u)" else rows
    cost = base + count * per_record
    monkeypatch.setattr(tpl, "_MAX_WORK", cost)
    assert tpl.decode_tpl(image).values == (expected,)
    monkeypatch.setattr(tpl, "_MAX_WORK", cost - 1)
    with pytest.raises(tpl.TplDecodeError, match="cumulative work"):
        tpl.decode_tpl(image)


@pytest.mark.parametrize(
    ("format_string", "payload", "cost", "expected"),
    [
        ("", b"", 2, ()),
        ("S()", b"", 5, ((),)),
        ("uu", struct.pack("<II", 7, 9), 6, (7, 9)),
        ("A(u)A(u)", struct.pack("<IIIIII", 2, 7, 9, 2, 11, 13), 14, ([7, 9], [11, 13])),
        ("A(S())B", struct.pack("<II", 0, 0), 9, ([], b"")),
        ("A(S())B", struct.pack("<II", 3, 0), 12, ([(), (), ()], b"")),
        (
            "A(A(S()))B",
            struct.pack("<IIII", 3, 4, 4, 4) + struct.pack("<I", 0),
            26,
            ([[()] * 4] * 3, b""),
        ),
    ],
)
def test_roots_siblings_and_zero_width_groups_share_the_exact_budget(
    monkeypatch: pytest.MonkeyPatch,
    format_string: str,
    payload: bytes,
    cost: int,
    expected: tuple,
) -> None:
    image = _image(format_string, payload)
    monkeypatch.setattr(tpl, "_MAX_WORK", cost)
    assert tpl.decode_tpl(image).values == expected
    monkeypatch.setattr(tpl, "_MAX_WORK", cost - 1)
    with pytest.raises(tpl.TplDecodeError, match="cumulative work"):
        tpl.decode_tpl(image)


def test_nested_zero_width_amplification_exceeds_default_cumulative_budget() -> None:
    # Each inner count passes the existing remaining-byte guard. The final blob
    # supplies those bytes without explaining the million empty result tuples.
    payload = (
        struct.pack("<I", 1000)
        + struct.pack("<I", 1000) * 1000
        + struct.pack("<I", 1000)
        + b"x" * 1000
    )
    image = _image("A(A(S()))B", payload)
    assert len(image) == 5027
    with pytest.raises(tpl.TplDecodeError, match="cumulative work"):
        tpl.decode_tpl(image)


def test_rejected_later_array_does_not_reset_sibling_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    image = _image("A(u)A(uu)", struct.pack("<IIIII", 2, 7, 9, 2, 11))
    monkeypatch.setattr(tpl, "_MAX_WORK", 15)
    with pytest.raises(tpl.TplDecodeError, match="cumulative work"):
        tpl.decode_tpl(image)


@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize(("count", "width"), [(0, 32), (1, 32), (64, 64), (1000, 1000)])
def test_empty_nested_arrays_do_not_repeat_wide_fixed_schema_discovery(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool, count: int, width: int
) -> None:
    fields = "S(" + "u" * width + ")" if wrapped else "u" * width
    payload = struct.pack("<I", count) + struct.pack("<I", 0) * count + struct.pack("<I", 0)
    image = _image(f"A(A({fields}))B", payload)
    cost = width + count + (11 if wrapped else 9)
    visits = 0
    fixed_formats = tpl._fixed_formats

    def observed(children: tuple) -> str | None:
        nonlocal visits
        visits += len(children)
        return fixed_formats(children)

    monkeypatch.setattr(tpl, "_fixed_formats", observed)
    monkeypatch.setattr(tpl, "_MAX_WORK", cost)
    assert tpl.decode_tpl(image).values == ([[] for _ in range(count)], b"")
    # Discovery work must fit the charged image work, rather than count * width.
    assert visits <= cost
    monkeypatch.setattr(tpl, "_MAX_WORK", cost - 1)
    with pytest.raises(tpl.TplDecodeError, match="cumulative work"):
        tpl.decode_tpl(image)


@pytest.mark.parametrize("payload", [b"", struct.pack("<I", 0)])
def test_empty_array_shortcut_keeps_structural_and_count_validation(payload: bytes) -> None:
    with pytest.raises(tpl.TplDecodeError, match="truncated|array count"):
        tpl.decode_tpl(_image("A()", payload))
