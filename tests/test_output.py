"""Focused tests of output contracts and row projection."""

from __future__ import annotations

import json

import pytest

from goodnotes_cli.config import Settings
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.output import Projection, emit


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
