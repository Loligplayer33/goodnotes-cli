"""Focused tests of output contracts and row projection."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from goodnotes_cli.config import Settings
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.output import Projection, emit, serializable, snippet_hits


def test_unknown_fields_fail_when_row_array_is_empty() -> None:
    settings = Settings(fields="missing", projection=Projection("search"))

    with pytest.raises(GoodnotesError, match=r"hits\[\]"):
        emit({"hits": []}, settings)


def test_absent_optional_response_field_can_be_selected(capsys: pytest.CaptureFixture[str]) -> None:
    settings = Settings(
        fields="status,warnings",
        projection=Projection("sync"),
    )

    emit({"status": "ready"}, settings)

    assert json.loads(capsys.readouterr().out) == {"status": "ready"}


def test_bundle_numeric_pages_are_top_level_and_snapshot_is_retained(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        fields="pages,manifest",
        snapshot_id="generation-1",
        projection=Projection("bundle"),
    )

    emit(
        {
            "directory": "/tmp/bundle",
            "manifest": "/tmp/bundle/text.json",
            "pages": [1, 2],
            "layer": "background",
            "truncated": False,
        },
        settings,
    )

    assert json.loads(capsys.readouterr().out) == {
        "pages": [1, 2],
        "manifest": "/tmp/bundle/text.json",
        "snapshot": "generation-1",
    }


def test_bundle_partial_fields_can_be_selected(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(fields="status,failed_pages,warnings", projection=Projection("bundle"))

    emit(
        {
            "status": "partial",
            "failed_pages": [2],
            "warnings": ["A page image is unavailable."],
        },
        settings,
    )

    assert json.loads(capsys.readouterr().out) == {
        "status": "partial",
        "failed_pages": [2],
        "warnings": ["A page image is unavailable."],
    }


def test_row_projection_retains_sibling_pagination_and_snapshot(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(
        fields="id",
        snapshot_id="generation-1",
        projection=Projection("ls"),
    )

    emit({"items": [{"id": "item-1", "name": "Synthetic"}], "next_offset": 1}, settings)

    assert json.loads(capsys.readouterr().out) == {
        "items": [{"id": "item-1"}],
        "next_offset": 1,
        "snapshot": "generation-1",
    }


@dataclass
class _NestedOutput:
    path: Path
    optional: str | None


@dataclass
class _OutputRecord:
    id: str
    nested: _NestedOutput
    values: tuple[int, ...]


def test_serializable_walks_nested_dataclasses_and_retains_nulls() -> None:
    value = _OutputRecord("synthetic", _NestedOutput(Path("/tmp/file"), None), (1, 2))

    assert serializable(value) == {
        "id": "synthetic",
        "nested": {"path": "/tmp/file", "optional": None},
        "values": [1, 2],
    }


def test_snippet_offsets_map_casefold_expansions_to_original_text() -> None:
    original = ("ß" * 250) + "MATCH" + ("z" * 400)

    result = snippet_hits([{"text": original}], "MATCH")

    assert result[0]["text"] == original[150:470]
    assert "MATCH" in result[0]["text"]
    assert result[0]["snippet"] is True
