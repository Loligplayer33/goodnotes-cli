from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.rendering import publication
from goodnotes_cli.rendering.publication import _BundleStage, bundle_stage, publish_bundle


@contextmanager
def _sources(destination: Path) -> Generator[_BundleStage, None, None]:
    with bundle_stage(destination) as stage:
        (stage.path / "page.png").write_bytes(b"image")
        (stage.path / "text.json").write_text("{}")
        yield stage


def test_replaced_destination_survives_failed_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination, moved = tmp_path / "bundle", tmp_path / "moved"
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
    with pytest.raises(OSError, match="generated link failure"), _sources(destination) as stage:
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"other"
    assert not (moved / "page.png").exists()
    assert not stage.path.exists()


def test_rollback_preserves_replaced_entry_and_cleans_owned_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with pytest.raises(OSError), _sources(destination) as stage:
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"replacement"


def test_normal_failure_removes_owned_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
    with pytest.raises(OSError), _sources(destination) as stage:
        publish_bundle(stage, destination)
    assert not destination.exists()
    assert not stage.path.exists()


def test_moved_parent_rejected_at_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    parent = tmp_path / "parent"
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
    with pytest.raises(GoodnotesError, match="parent changed"), _sources(destination) as stage:
        publish_bundle(stage, destination)
    assert not (tmp_path / "moved-parent" / "bundle").exists()
    assert not (tmp_path / "moved-parent" / stage.path.name).exists()
    assert parent.is_dir()


def test_replaced_destination_rejected_before_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination, moved = tmp_path / "bundle", tmp_path / "moved"
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
    with pytest.raises(GoodnotesError, match="directory changed"), _sources(destination) as stage:
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"other"
    assert not list(moved.iterdir())


@pytest.mark.parametrize("foreign", [False, True])
def test_interrupted_link_preserves_uncertain_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, foreign: bool
) -> None:
    destination = tmp_path / "bundle"
    link = os.link
    with pytest.raises(KeyboardInterrupt), _sources(destination) as stage:

        def interrupted(*args: Any, **kwargs: Any) -> None:
            if foreign:
                link(stage.path / "page.png", destination / "page.png")
            else:
                link(*args, **kwargs)
            raise KeyboardInterrupt

        monkeypatch.setattr(os, "link", interrupted)
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"image"
    assert not (destination / "text.json").exists()
    assert not stage.path.exists()


def test_interruption_rolls_back_confirmed_links_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "bundle"
    link = os.link
    installed: list[str] = []

    def interrupted(*args: Any, **kwargs: Any) -> None:
        link(*args, **kwargs)
        installed.append(args[0])
        if len(installed) == 2:
            raise KeyboardInterrupt

    monkeypatch.setattr(os, "link", interrupted)
    with pytest.raises(KeyboardInterrupt), _sources(destination) as stage:
        (stage.path / "second.png").write_bytes(b"second image")
        publish_bundle(stage, destination)
    assert not (destination / installed[0]).exists()
    assert (destination / installed[1]).exists()
    assert not (destination / "text.json").exists()
    assert not stage.path.exists()


def test_failed_link_preserves_foreign_entry_even_if_same_inode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "bundle"
    link = os.link
    with pytest.raises(FileExistsError), _sources(destination) as stage:

        def collision(*args: Any, **kwargs: Any) -> None:
            link(stage.path / "page.png", destination / "page.png")
            raise FileExistsError("generated collision")

        monkeypatch.setattr(os, "link", collision)
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"image"


def test_moved_parent_uses_anchored_stage_and_preserves_replacement(
    tmp_path: Path,
) -> None:
    parent, moved = tmp_path / "parent", tmp_path / "moved-parent"
    destination = parent / "bundle"
    with pytest.raises(GoodnotesError, match="parent changed"), _sources(destination) as stage:
        parent.rename(moved)
        parent.mkdir()
        replacement = parent / stage.path.name
        replacement.mkdir()
        (replacement / "foreign").write_bytes(b"foreign")
        publish_bundle(stage, destination)
    assert (replacement / "foreign").read_bytes() == b"foreign"
    assert not (moved / stage.path.name).exists()
    assert not destination.exists()


def test_mismatched_source_identity_is_not_adopted(tmp_path: Path) -> None:
    destination = tmp_path / "bundle"
    with _sources(destination) as stage:
        with pytest.raises(GoodnotesError, match="staging directory changed"):
            publish_bundle(replace(stage, directory_identity=(-1, -1)), destination)
        assert (stage.path / "page.png").exists()
        assert not destination.exists()
    assert not stage.path.exists()


def test_replaced_stage_name_is_not_cleaned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination, moved = tmp_path / "bundle", tmp_path / "original-staging"
    link = os.link
    count = 0
    with _sources(destination) as stage:

        def replaced(*args: Any, **kwargs: Any) -> None:
            nonlocal count
            count += 1
            if count == 1:
                stage.path.rename(moved)
                stage.path.mkdir()
                (stage.path / "other").write_bytes(b"foreign")
            link(*args, **kwargs)

        monkeypatch.setattr(os, "link", replaced)
        publish_bundle(stage, destination)
    assert (destination / "page.png").read_bytes() == b"image"
    assert (stage.path / "other").read_bytes() == b"foreign"
    assert (moved / "page.png").exists()


def test_normal_publication_uses_private_directory_and_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "new-parent" / "bundle"
    names = []
    original = os.link

    def link(name: str, *args: Any, **kwargs: Any) -> None:
        names.append(name)
        original(name, *args, **kwargs)

    with _sources(destination) as stage:
        monkeypatch.setattr(os, "link", link)
        publish_bundle(stage, destination)
        assert stage.path.exists()
    assert names == ["page.png", "text.json"]
    assert destination.stat().st_mode & 0o777 == 0o700
    assert not stage.path.exists()
    assert destination.parent.is_dir()


@pytest.mark.parametrize("failure", ["parent-stat", "stage-stat", "stage-open", "stage-fstat"])
def test_stage_acquisition_failure_closes_every_returned_descriptor_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    destination = tmp_path / "parent" / "bundle"
    opened: list[int] = []
    closed: list[int] = []
    original_open, original_close, original_fstat, original_stat = (
        os.open,
        os.close,
        os.fstat,
        os.stat,
    )

    def open_directory(path: Any, *args: Any, **kwargs: Any) -> int:
        if failure == "stage-open" and str(path).startswith(".goodnotes-bundle-"):
            raise OSError("generated stage open failure")
        descriptor = original_open(path, *args, **kwargs)
        opened.append(descriptor)
        return descriptor

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    def fstat(descriptor: int) -> os.stat_result:
        if descriptor in opened:
            index = opened.index(descriptor)
            if (failure == "parent-stat" and index == 0) or (
                failure == "stage-fstat" and index == 1
            ):
                raise OSError("generated descriptor stat failure")
        return original_fstat(descriptor)

    def stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        if failure == "stage-stat" and str(path).startswith(".goodnotes-bundle-"):
            raise OSError("generated stage identity failure")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(publication.os, "open", open_directory)
    monkeypatch.setattr(publication.os, "close", close)
    monkeypatch.setattr(publication.os, "fstat", fstat)
    monkeypatch.setattr(publication.os, "stat", stat)
    with pytest.raises(OSError, match="generated"), bundle_stage(destination):
        pytest.fail("Failed stage acquisition yielded ownership")
    assert opened
    assert sorted(opened) == sorted(closed)


@pytest.mark.parametrize("primary_kind", ["none", "error", "interrupt"])
@pytest.mark.parametrize("close_kind", ["error", "interrupt"])
def test_stage_close_failure_attempts_remaining_releases_and_preserves_primary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, primary_kind: str, close_kind: str
) -> None:
    destination = tmp_path / "bundle"
    primary = (
        KeyboardInterrupt("primary") if primary_kind == "interrupt" else GoodnotesError("primary")
    )
    cleanup = KeyboardInterrupt("cleanup") if close_kind == "interrupt" else OSError("cleanup")
    original_close = os.close
    closed: list[int] = []
    expected = cleanup if primary_kind == "none" else primary
    with pytest.raises(type(expected)) as captured, _sources(destination) as stage:

        def close(descriptor: int) -> None:
            original_close(descriptor)
            if descriptor in {stage.directory_fd, stage.parent_fd}:
                closed.append(descriptor)
                if descriptor == stage.directory_fd:
                    raise cleanup

        monkeypatch.setattr(publication.os, "close", close)
        if primary_kind != "none":
            raise primary
    assert captured.value is expected
    assert closed == [stage.directory_fd, stage.parent_fd]
    assert not stage.path.exists()


@pytest.mark.parametrize("failed_close", ["destination", "stage", "parent"])
def test_cleanup_only_failure_keeps_complete_output_and_never_removes_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failed_close: str
) -> None:
    destination = tmp_path / "new-parent" / "bundle"
    original_close, original_rmdir = os.close, os.rmdir
    closed: list[int] = []
    parent_removals: list[Path] = []
    expected = OSError("generated close failure")
    injected = False
    with pytest.raises(OSError) as captured, _sources(destination) as stage:

        def close(descriptor: int) -> None:
            nonlocal injected
            original_close(descriptor)
            if descriptor in {stage.directory_fd, stage.parent_fd}:
                name = "stage" if descriptor == stage.directory_fd else "parent"
            else:
                name = "destination"
            closed.append(descriptor)
            if name == failed_close and not injected:
                injected = True
                raise expected

        def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == destination.parent:
                parent_removals.append(Path(path))
            original_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(publication.os, "close", close)
        monkeypatch.setattr(publication.os, "rmdir", rmdir)
        publish_bundle(stage, destination)
    assert captured.value is expected
    assert closed.count(stage.directory_fd) == closed.count(stage.parent_fd) == 1
    assert len(closed) >= 3
    assert parent_removals == []
    assert (destination / "text.json").read_text() == "{}"
    assert not stage.path.exists()


def test_completed_publication_never_removes_parent_if_output_disappears_during_failed_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination = tmp_path / "new-parent" / "bundle"
    original_close, original_rmdir = os.close, os.rmdir
    removals: list[Path] = []
    expected = KeyboardInterrupt("generated destination close interrupt")
    with pytest.raises(KeyboardInterrupt) as captured, _sources(destination) as stage:

        def close(descriptor: int) -> None:
            original_close(descriptor)
            if descriptor not in {stage.directory_fd, stage.parent_fd} and destination.exists():
                assert stage.publication_completed
                for child in destination.iterdir():
                    child.unlink()
                destination.rmdir()
                raise expected

        def rmdir(path: Any, *args: Any, **kwargs: Any) -> None:
            if Path(path) == destination.parent:
                removals.append(Path(path))
            original_rmdir(path, *args, **kwargs)

        monkeypatch.setattr(publication.os, "close", close)
        monkeypatch.setattr(publication.os, "rmdir", rmdir)
        publish_bundle(stage, destination)
    assert captured.value is expected
    assert not destination.exists()
    assert destination.parent.is_dir()
    assert removals == []
    assert not stage.path.exists()


def test_stage_replacement_during_acquisition_is_preserved_and_original_descriptors_close(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    destination, moved = tmp_path / "bundle", tmp_path / "original-stage"
    original_open, original_close = os.open, os.close
    opened, closed = [], []
    replacement: Path | None = None

    def open_directory(path: Any, *args: Any, **kwargs: Any) -> int:
        nonlocal replacement
        descriptor = original_open(path, *args, **kwargs)
        opened.append(descriptor)
        if str(path).startswith(".goodnotes-bundle-"):
            replacement = destination.parent / path
            replacement.rename(moved)
            replacement.mkdir()
            (replacement / "foreign").write_bytes(b"generated foreign")
        return descriptor

    def close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    monkeypatch.setattr(publication.os, "open", open_directory)
    monkeypatch.setattr(publication.os, "close", close)
    with (
        pytest.raises(GoodnotesError, match="staging directory changed"),
        bundle_stage(destination),
    ):
        pytest.fail("Replacement stage reached rendering")
    assert replacement is not None
    assert (replacement / "foreign").read_bytes() == b"generated foreign"
    assert moved.is_dir()
    assert sorted(opened) == sorted(closed)
    assert not destination.exists()


def test_failed_operation_removes_only_new_empty_leaf_parent(tmp_path: Path) -> None:
    destination = tmp_path / "created-parent" / "bundle"
    with pytest.raises(GoodnotesError), _sources(destination):
        raise GoodnotesError("generated render failure")
    assert not destination.parent.exists()
