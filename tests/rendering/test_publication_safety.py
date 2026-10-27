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


@pytest.mark.parametrize("foreign", [False, True])
def test_interrupted_link_preserves_uncertain_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    link = os.link

    def interrupted(*args: Any, **kwargs: Any) -> None:
        if foreign:
            # The operation cannot distinguish this collision from its own
            # successful syscall followed by interruption before returning.
            link(source / "page.png", destination / "page.png")
        else:
            link(*args, **kwargs)
        raise KeyboardInterrupt

    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"image"
    assert not (destination / "text.json").exists()
    assert not source.exists()


def test_interruption_rolls_back_confirmed_links_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    (source / "second.png").write_bytes(b"second image")
    destination = tmp_path / "bundle"
    link = os.link
    installed: list[str] = []

    def interrupted(*args: Any, **kwargs: Any) -> None:
        link(*args, **kwargs)
        installed.append(args[0])
        if len(installed) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(KeyboardInterrupt):
        publish_bundle(source, destination)
    assert not (destination / installed[0]).exists()
    assert (destination / installed[1]).exists()
    assert not (destination / "text.json").exists()
    assert not source.exists()


def test_failed_link_preserves_foreign_entry_even_if_same_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    link = os.link

    def collision(*args: Any, **kwargs: Any) -> None:
        # A foreign hard link can have the same identity as the staging file.
        link(source / "page.png", destination / "page.png")
        raise FileExistsError("generated collision")

    monkeypatch.setattr(os, "link", collision)
    with pytest.raises(FileExistsError):
        publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"image"


def test_moved_source_parent_uses_anchored_stage_and_preserves_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_parent = tmp_path / "stage-parent"
    source_parent.mkdir()
    source = _sources(source_parent)
    destination = tmp_path / "bundle"
    moved = tmp_path / "moved-parent"
    link = os.link
    count = 0

    def moved_parent(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 1:
            source_parent.rename(moved)
            source_parent.mkdir()
            replacement = _sources(source_parent)
            (replacement / "page.png").write_bytes(b"foreign")
        link(*args, **kwargs)

    monkeypatch.setattr(os, "link", moved_parent)
    publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"image"
    assert (source / "page.png").read_bytes() == b"foreign"
    assert not (moved / "staging").exists()


def test_mismatched_source_identity_is_not_adopted_or_deleted(tmp_path: Path) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    with pytest.raises(GoodnotesError, match="staging directory changed"):
        publish_bundle(source, destination, expected_source_identity=(-1, -1))
    assert (source / "page.png").exists()
    assert not destination.exists()


def test_replaced_stage_name_is_not_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _sources(tmp_path)
    destination = tmp_path / "bundle"
    moved = tmp_path / "original-staging"
    link = os.link
    count = 0

    def replaced(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        if count == 1:
            source.rename(moved)
            source.mkdir()
            (source / "other").write_bytes(b"foreign")
        link(*args, **kwargs)

    monkeypatch.setattr(os, "link", replaced)
    publish_bundle(source, destination)
    assert (destination / "page.png").read_bytes() == b"image"
    assert (source / "other").read_bytes() == b"foreign"
    assert (moved / "page.png").exists()
