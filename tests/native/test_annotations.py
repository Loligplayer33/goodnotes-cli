"""Native overlay integration with independent generated records."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Any

import pymupdf as fitz
import pytest

from goodnotes_cli.native import NativeError
from goodnotes_cli.rendering import overlay as overlay_module
from goodnotes_cli.rendering.overlay import render_native_overlay
from tests.support.native_oracle import (
    LOGICAL_ID,
    ROOT_ID,
    _FixtureSnapshot,
    _image_records_without_field_6,
    field,
    framed,
    native_records,
    page,
    tpl,
)


def test_lasso_translation_and_rgba_survive_to_rendered_pixels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    body = (
        field(1, LOGICAL_ID.encode())
        + field(2, framed(tpl()))
        + field(4, field(1, 1.0) + field(4, 0.5))
        + field(6, field(1, 10.0) + field(2, 5.0))
    )
    monkeypatch.setattr(overlay_module, "read_page_records", lambda *_: native_records(body=body))
    overlay = render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert b"10.909091,13.636364" in overlay.svg
    assert b"#ff0000" in overlay.svg
    assert b"0.500000" in overlay.svg
    with fitz.open(stream=overlay.svg, filetype="svg") as document:
        pixmap = document[0].get_pixmap(dpi=132, alpha=False)
        red, green, blue = pixmap.pixel(30, 25)
    assert red >= 250
    assert 120 <= green <= 135 and 120 <= blue <= 135


def test_native_overlay_accepts_a_session_record_reader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def read_from_session(root_uuid: str) -> list[tuple[bytes, bytes]]:
        calls.append(root_uuid)
        return native_records()

    monkeypatch.setattr(
        overlay_module, "read_page_records", lambda *_: pytest.fail("opened one-shot store")
    )
    overlay = render_native_overlay(
        _FixtureSnapshot(),
        page(),
        width_points=72,
        height_points=72,
        record_reader=read_from_session,
    )
    assert calls == [ROOT_ID]
    assert overlay.metadata["rendered_strokes"] == 1


@pytest.mark.parametrize(
    "edit",
    [{"rotation": 90}, {"width": None}, {"height": 0}, {"width": math.inf}, {"id": "invalid"}],
)
def test_invalid_page_mapping_fails_before_native_read(
    monkeypatch: pytest.MonkeyPatch, edit: dict[str, Any]
) -> None:
    monkeypatch.setattr(
        overlay_module, "read_page_records", lambda *_: pytest.fail("opened native")
    )
    with pytest.raises(NativeError):
        render_native_overlay(
            _FixtureSnapshot(), replace(page(), **edit), width_points=72, height_points=72
        )


def test_successful_empty_native_lookup_renders_with_unverified_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(overlay_module, "read_page_records", lambda *_: [])
    result = render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert result.svg == b""
    assert result.metadata["annotation_status"] == "native-no-records-observed"
    assert result.metadata["coverage"] == "no-native-records-observed"
    assert result.metadata["native_records"] == result.metadata["rendered_objects"] == 0
    assert result.metadata["full_fidelity_verified"] is False
    assert "absence of annotations in Goodnotes is unverified" in str(result.metadata["warnings"])


@pytest.mark.parametrize("operation", ["read_native_copy", "inspect_native_store"])
def test_failed_native_read_never_becomes_an_empty_overlay(
    monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    expected = NativeError(
        "native_data_unavailable", "Generated read failure.", operation=operation
    )

    def fail(*_args: Any) -> Any:
        raise expected

    monkeypatch.setattr(overlay_module, "read_page_records", fail)
    with pytest.raises(NativeError) as result:
        render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    assert result.value is expected


def test_unknown_objects_only_fail_instead_of_rendering_a_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        overlay_module, "read_page_records", lambda *_: native_records(type_code=99)
    )
    with pytest.raises(NativeError, match="none could be rendered") as captured:
        render_native_overlay(_FixtureSnapshot(), page(), width_points=72, height_points=72)
    coverage = captured.value.details["native_annotations"]
    assert isinstance(coverage, dict)
    assert coverage["unsupported_objects"] == 1
    assert coverage["unsupported_reasons"] == {"unsupported-object-type": 1}


def test_issue_8_image_without_copied_attachment_fails_without_background_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = _image_records_without_field_6()

    def unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise NativeError(
            "native_data_unavailable",
            "Generated attachment is unavailable.",
            operation="load_native_image",
        )

    monkeypatch.setattr(overlay_module, "load_native_image", unavailable)

    with pytest.raises(NativeError, match="none could be rendered") as captured:
        render_native_overlay(
            _FixtureSnapshot(),
            page(),
            width_points=72,
            height_points=72,
            record_reader=lambda _root_uuid: records,
        )

    assert captured.value.code == "native_data_unavailable"
    assert captured.value.details["operation"] == "decode_native_page"
    native_annotations = captured.value.details["native_annotations"]
    assert isinstance(native_annotations, dict)
    assert native_annotations["unsupported_reasons"] == {"native-image-unavailable": 1}
    assert native_annotations["native_image_body_field_6_absent_records"] == 1
