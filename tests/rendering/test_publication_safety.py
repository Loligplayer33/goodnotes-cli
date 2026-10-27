from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.rendering.publication import publish_bundle


def _sources(tmp_path: Path) -> Path:
    source = tmp_path / "staging"
    source.mkdir()
    (source / "page.png").write_bytes(b"image")
    (source / "text.json").write_text("{}")
    return source


def test_replaced_destination_survives_failed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    moved = tmp_path / "moved"
    link = os.link
    count = 0

    def racing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 2:
            destination.rename(moved)
            destination.mkdir()
            (destination / "page.png").write_bytes(b"other")
            raise OSError("generated link failure")
        link(*args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(OSError, match="generated link failure"):
        publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"other"
    assert not (moved / "page.png").exists()


def test_rollback_preserves_replaced_entry_and_cleans_owned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    link = os.link
    count = 0

    def racing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 2:
            (destination / "page.png").unlink()
            (destination / "page.png").write_bytes(b"replacement")
            raise OSError("generated link failure")
        link(*args, **kwargs)

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(OSError):
        publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"replacement"


def test_normal_failure_removes_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    link = os.link
    count = 0

    def failing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("generated link failure")
        link(*args, **kwargs)

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(OSError):
        publish_bundle(source, destination)
    assert not destination.exists()
    assert not source.exists()


def test_moved_parent_rejected_at_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = _sources(tmp_path)
    parent = tmp_path / "parent"
    parent.mkdir()
    destination = parent / "bundle"
    link = os.link
    count = 0

    def racing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        link(*args, **kwargs)
        count += 1
        if count == 2:
            parent.rename(tmp_path / "moved-parent")
            parent.mkdir()

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(GoodnotesError, match="parent changed"):
        publish_bundle(source, destination)
    assert not (tmp_path / "moved-parent" / "bundle").exists()


def test_replaced_destination_rejected_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    moved = tmp_path / "moved"
    link = os.link
    count = 0

    def racing_link(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        link(*args, **kwargs)
        count += 1
        if count == 2:
            destination.rename(moved)
            destination.mkdir()
            (destination / "page.png").write_bytes(b"other")

    monkeypatch.setattr(os, "link", racing_link)
    with pytest.raises(GoodnotesError, match="directory changed"):
        publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"other"
    assert not list(moved.iterdir())
