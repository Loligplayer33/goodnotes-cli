from __future__ import annotations

from typing import Any, cast

import pytest

from goodnotes_cli.native import NativeError
from goodnotes_cli.native.objects import decode_native_objects
from goodnotes_cli.rendering import overlay as a
from tests.support.native_oracle import (
    ATTACHMENT_ID,
    RESOURCE_ID,
    ROOT_ID,
    _FixtureSnapshot,
    _pattern_body,
    _pattern_metadata,
    _texture,
    field,
    native_records,
    page,
)


def _records(extra: bytes | None = None) -> list[tuple[bytes, bytes]]:
    return native_records(
        type_code=28,
        body=_pattern_body(_pattern_metadata()),
        extra_descriptor=field(4, RESOURCE_ID.encode()) if extra is None else extra,
    )


def test_nonempty_pattern_metadata_is_not_reported_as_an_empty_body_field() -> None:
    objects, metadata = decode_native_objects(_records(), ROOT_ID)

    assert len(objects) == 1
    assert metadata["rendered_paths"] == 1
    assert metadata["unmodeled_empty_body_fields"] == []


def test_pattern_resource_binding_is_verified_before_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(a, "load_native_image", lambda *_: pytest.fail("unexpected asset read"))
    for binding in (
        b"",
        field(4, ATTACHMENT_ID.encode()),
        field(4, RESOURCE_ID.encode()) + field(7, ATTACHMENT_ID.encode()),
    ):
        objects, metadata = decode_native_objects(_records(binding), ROOT_ID)
        assert objects == []
        assert metadata["unsupported_objects"] == 1


def test_pattern_resource_uses_shared_attachment_loader_and_retains_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requested = []

    def load(_sandbox: Any, document: str, resource: str) -> Any:
        requested.append((document, resource))
        return _texture()

    monkeypatch.setattr(a, "load_native_image", load)
    overlay = a.render_native_overlay(
        _FixtureSnapshot(),
        page(),
        width_points=72,
        height_points=72,
        record_reader=lambda _: _records(),
    )
    assert requested == [(page().document_id, RESOURCE_ID)]
    assert overlay.metadata["rendered_paths"] == 1
    assert overlay.metadata["rendered_images"] == 0
    assert overlay.metadata["unsupported_objects"] == 0
    assert overlay.metadata["coverage"] == "partial"
    assert any(
        "Pattern resolution" in warning for warning in cast(list[str], overlay.metadata["warnings"])
    )
    assert b"data:image/png;base64," in overlay.svg


@pytest.mark.parametrize("unavailable", [False, True])
def test_pattern_resource_failure_preserves_other_strokes_and_correct_counts(
    monkeypatch: pytest.MonkeyPatch, unavailable: bool
) -> None:
    def load(*_: Any) -> Any:
        if unavailable:
            raise NativeError(
                "native_data_unavailable",
                "Generated unavailable resource",
                operation="load_native_image",
            )
        return _texture()

    monkeypatch.setattr(a, "load_native_image", load)
    if not unavailable:
        monkeypatch.setattr(a, "_MAX_PAGE_IMAGE_BYTES", 1)
    records = _records() + native_records(
        logical_id="00000000-0000-0000-0000-000000000901",
        storage_id="00000000-0000-0000-0000-000000000902",
        sequence=2,
    )
    overlay = a.render_native_overlay(
        _FixtureSnapshot(),
        page(),
        width_points=72,
        height_points=72,
        record_reader=lambda _: records,
    )
    assert overlay.metadata["rendered_objects"] == 1
    assert overlay.metadata["rendered_strokes"] == 1
    assert overlay.metadata["rendered_paths"] == overlay.metadata["rendered_images"] == 0
    reason = "native-pattern-unavailable" if unavailable else "native-pattern-budget-exceeded"
    assert overlay.metadata["unsupported_reasons"] == {reason: 1}
    assert overlay.metadata["coverage"] == "partial"


def test_missing_pattern_only_is_an_error_without_a_background_substitute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def load(*_: Any) -> Any:
        raise NativeError(
            "native_data_unavailable",
            "Generated unavailable resource",
            operation="load_native_image",
        )

    monkeypatch.setattr(a, "load_native_image", load)
    with pytest.raises(NativeError, match="none could be rendered"):
        a.render_native_overlay(
            _FixtureSnapshot(),
            page(),
            width_points=72,
            height_points=72,
            record_reader=lambda _: _records(),
        )
