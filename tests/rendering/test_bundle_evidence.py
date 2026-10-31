"""Bundle text is read once and paired by public page identity."""

from typing import Any

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.models import Page, PageText, TextPart
from goodnotes_cli.presentation.json import serializable
from goodnotes_cli.rendering.bundle import _bundle_content, _read_bundle_text


def _text(number: int) -> dict[str, Any]:
    return serializable(
        PageText(f"page-{number}", "document", number, [TextPart("pdf", str(number))])
    )


@pytest.mark.parametrize("partial", [False, True])
def test_bundle_pairs_text_by_page_key_in_selected_order(partial: bool) -> None:
    texts = {1: _text(1), 3: _text(3)}
    rendered: list[dict[str, object]] = [
        {"document_id": "document", "page": 3},
        {"page": 1, "render_error": {"code": "generated"}}
        if partial
        else {"document_id": "document", "page": 1},
    ]
    manifest, truncated = _bundle_content(texts, "document", [3, 1], rendered, 100)
    assert [entry["page"] for entry in manifest] == [3, 1]
    assert [entry["text"]["page_id"] for entry in manifest] == ["page-3", "page-1"]
    assert [entry["text"]["parts"][0]["text"] for entry in manifest] == ["3", "1"]
    assert (manifest[1]["image"] is None) == partial
    assert not truncated


def test_bundle_text_read_preserves_one_cumulative_budget() -> None:
    calls: list[tuple[int, int]] = []

    def read(number: int, remaining: int) -> PageText:
        calls.append((number, remaining))
        return PageText(f"page-{number}", "document", number, [TextPart("pdf", "abc"[:remaining])])

    texts = _read_bundle_text(
        read, [Page(f"page-{n}", "document", n, str(n)) for n in (3, 1, 2)], 5
    )
    manifest, truncated = _bundle_content(
        texts, "document", [3, 1, 2], [{"document_id": "document", "page": n} for n in (3, 1, 2)], 5
    )
    assert calls == [(3, 5), (1, 2), (2, 0)]
    assert [entry["text"]["parts"][0]["text"] for entry in manifest] == ["abc", "ab", ""]
    assert not truncated


@pytest.mark.parametrize("keys", [[], [3], [1, 2, 3]])
def test_bundle_rejects_missing_or_extra_text(keys: list[int]) -> None:
    with pytest.raises(GoodnotesError, match="does not match the selected pages"):
        _bundle_content(
            {n: _text(n) for n in keys},
            "document",
            [3, 1],
            [{"document_id": "document", "page": n} for n in (3, 1)],
            100,
        )


@pytest.mark.parametrize("numbers", [[1, 3], [3], [3, 3]])
def test_bundle_rejects_reordered_missing_or_duplicate_renders(numbers: list[int]) -> None:
    with pytest.raises(GoodnotesError, match="selected pages|page identity"):
        _bundle_content(
            {1: _text(1), 3: _text(3)},
            "document",
            [3, 1],
            [{"document_id": "document", "page": n} for n in numbers],
            100,
        )


def test_bundle_rejects_partial_error_for_another_page() -> None:
    with pytest.raises(GoodnotesError, match="render has missing or inconsistent page identity"):
        _bundle_content(
            {1: _text(1)},
            "document",
            [1],
            [{"page": 2, "render_error": {"code": "generated"}}],
            100,
        )


def test_bundle_rejects_text_stored_under_another_page() -> None:
    with pytest.raises(GoodnotesError, match="text has missing or inconsistent page identity"):
        _bundle_content(
            {1: _text(2)}, "document", [1], [{"document_id": "document", "page": 1}], 100
        )


@pytest.mark.parametrize("error", [None, "generated", []])
def test_bundle_rejects_malformed_present_render_error(error: object) -> None:
    with pytest.raises(GoodnotesError, match="render error"):
        _bundle_content({1: _text(1)}, "document", [1], [{"page": 1, "render_error": error}], 100)


@pytest.mark.parametrize("document", [None, "another-document"])
@pytest.mark.parametrize("partial", [False, True])
def test_bundle_rejects_inconsistent_render_document(document: object, partial: bool) -> None:
    rendered: dict[str, object] = {"page": 1, "document_id": document}
    if partial:
        rendered["render_error"] = {"code": "generated"}
    with pytest.raises(GoodnotesError, match="document identity"):
        _bundle_content({1: _text(1)}, "document", [1], [rendered], 100)
