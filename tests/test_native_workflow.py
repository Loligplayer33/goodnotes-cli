"""Generated native-store workflows exercised through the installed CLI."""

from __future__ import annotations

import hashlib
import json
import os
import select
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from native_fixture import LOGICAL_IDS as LOGICAL_IDS
from native_fixture import (
    NOTEBOOK,
    PAGE_IDS,
    GeneratedNativeStore,
    _add_image_attachment,
    _native_image,
    _solid_png,
    _stroke,
)
from native_fixture import STORAGE_IDS as STORAGE_IDS
from native_fixture import generated_source as generated_source
from native_fixture import native_runtime as native_runtime

from goodnotes_cli.cache import read_view
from goodnotes_cli.config import Settings
from goodnotes_cli.documents import render_page
from goodnotes_cli.library import Library


def _environment(tmp_path: Path) -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("GOODNOTES_SANDBOX", None)
    environment["GOODNOTES_DATA_DIR"] = str(tmp_path / "cache")
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    environment["GOODNOTES_SCRATCH_DIR"] = str(scratch)
    environment["PYTHONUNBUFFERED"] = "1"
    return environment


def _command(arguments: list[str], environment: dict[str, str]) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


def _manifest(root: Path) -> tuple:
    return tuple(
        (
            str(path.relative_to(root)),
            path.lstat().st_mode,
            path.lstat().st_size,
            path.lstat().st_mtime_ns,
            path.lstat().st_ctime_ns,
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None,
        )
        for path in [root, *sorted(root.rglob("*"))]
    )


def _pixel(path: Path, x: int, y: int) -> tuple[int, ...]:
    return fitz.Pixmap(str(path)).pixel(x, y)


def _native_render(page: int, output: Path) -> list[str]:
    return [
        "render",
        NOTEBOOK,
        "--page",
        str(page),
        "--layer",
        "native",
        "--dpi",
        "72",
        "--output",
        str(output),
    ]


@pytest.mark.parametrize("generated_source", [False, True], indirect=True)
def test_native_png_pdf_bundle_preserve_sources_and_readonly_cache(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    # This workflow never writes new native records after fixture setup. Close the
    # producer before the source baseline so its append-only RocksDB LOG cannot
    # advance independently while the reader-isolation assertions run. Other tests
    # that exercise a live writer deliberately keep the store open.
    store.close()
    original = _manifest(source)
    captured = _command(["--verbose", "sync", "--library", str(source)], environment)
    brief_refresh = _command(["sync", "--library", str(source)], environment)
    assert brief_refresh["snapshot_id"] == captured["snapshot_id"]
    assert brief_refresh["captured_at"] == captured["captured_at"]
    assert brief_refresh["source_changed_during_refresh"] is False
    assert "snapshot" not in brief_refresh
    assert "capture_stats" not in brief_refresh
    assert _manifest(source) == original
    cache = tmp_path / "cache"
    modes = {path: stat.S_IMODE(path.stat().st_mode) for path in [cache, *cache.rglob("*")]}
    try:
        for path, mode in sorted(modes.items(), key=lambda pair: len(pair[0].parts), reverse=True):
            path.chmod(mode & ~0o222)
        before = _manifest(cache)
        red_png = tmp_path / "red.png"
        rendered = _command(["--verbose", *_native_render(1, red_png)], environment)
        assert rendered["snapshot"] == captured["snapshot_id"]
        assert rendered["native_annotations"]["rendered_strokes"] == 1
        assert rendered["native_annotations"]["coverage"] == (
            "partial" if store.direct_identity else "supported-pen-records"
        )
        if store.direct_identity:
            assert rendered["native_annotations"]["unmodeled_descriptor_fields"] == [14]
            assert rendered["native_annotations"]["unmodeled_empty_body_fields"] == [20]
        assert rendered["native_annotations"]["full_fidelity_verified"] is False
        assert rendered["includes_handwriting"] is True
        assert _pixel(red_png, 60, 30)[:3] == (255, 0, 0)
        assert _pixel(red_png, 60, 90)[:3] == (255, 255, 255)
        output_pdf = tmp_path / "selected.pdf"
        exported = _command(
            [
                "export",
                NOTEBOOK,
                "--pages",
                "2,1",
                "--layer",
                "native",
                "--output",
                str(output_pdf),
            ],
            environment,
        )
        assert exported["selected_pages"] == [2, 1]
        assert [entry["page"] for entry in exported["native_annotations"]] == [2, 1]
        with fitz.open(output_pdf) as pdf:
            assert len(pdf) == 2
            assert "Generated background 2" in pdf[0].get_text()
            assert pdf[0].get_pixmap().pixel(60, 90)[:3] == (0, 0, 255)
            assert pdf[1].get_pixmap().pixel(60, 30)[:3] == (255, 0, 0)
        bundle = _command(
            [
                "--verbose",
                "bundle",
                NOTEBOOK,
                "--pages",
                "2",
                "--dpi",
                "72",
                "--output",
                str(tmp_path / "bundle"),
            ],
            environment,
        )
        manifest = json.loads(Path(bundle["manifest"]).read_text())
        entry = manifest["pages"][0]
        assert entry["page"] == 2
        assert entry["render"]["native_annotations"]["rendered_strokes"] == 1
        assert _pixel(Path(bundle["directory"]) / entry["image"], 60, 90)[:3] == (0, 0, 255)
        inline = _command(
            [
                "--expect-snapshot",
                captured["snapshot_id"],
                "bundle",
                NOTEBOOK,
                "--pages",
                "2,1",
                "--dpi",
                "72",
                "--max-chars",
                "15",
                "--output",
                str(tmp_path / "inline-bundle"),
            ],
            environment,
        )
        full_inline = json.loads(Path(inline["manifest"]).read_text())
        assert inline["layer"] == "native"
        assert inline["document_id"] == NOTEBOOK
        assert inline["snapshot"] == full_inline["snapshot"] == captured["snapshot_id"]
        assert inline["captured_at"] == full_inline["captured_at"]
        assert inline["text_source"] == full_inline["text_source"] == "pdf"
        assert inline["truncated"] is full_inline["truncated"] is True
        assert inline["pages"] == [2, 1]
        assert [page["page_id"] for page in inline["page_evidence"]] == [
            PAGE_IDS[1],
            PAGE_IDS[0],
        ]
        for evidence, full in zip(inline["page_evidence"], full_inline["pages"], strict=True):
            assert Path(evidence["image"]).is_absolute()
            assert evidence["parts"] == full["text"]["parts"]
            assert all(part["source"] == "pdf" for part in evidence["parts"])
            assert evidence["image_source"] == full["render"]["source"]
            assert evidence["includes_handwriting"] is True
            assert evidence["annotation_status"] == "native-reconstructed-approximate"
            assert evidence["native_annotations"]["rendered_objects"] == 1
            assert evidence["native_annotations"]["rendered_strokes"] == 1
            assert evidence["native_annotations"]["unsupported_objects"] == 0
            assert evidence["native_annotations"]["full_fidelity_verified"] is False
            assert evidence["native_annotations"]["coverage"] == (
                "partial" if store.direct_identity else "supported-pen-records"
            )
            assert set(evidence["warnings"]) == set(
                full["render"]["warnings"] + full["text"]["warnings"]
            )
            assert "descriptor_variants" in full["render"]["native_annotations"]
            assert "descriptor_variants" not in evidence["native_annotations"]
            if store.direct_identity:
                assert full["render"]["native_annotations"]["unmodeled_descriptor_fields"] == [14]
                assert any("field 14" in warning for warning in evidence["warnings"])
        assert _pixel(Path(inline["page_evidence"][0]["image"]), 60, 90)[:3] == (0, 0, 255)
        assert _pixel(Path(inline["page_evidence"][1]["image"]), 60, 30)[:3] == (255, 0, 0)
        assert _manifest(cache) == before
        assert _manifest(source) == original
        assert not list((tmp_path / "scratch").iterdir())
    finally:
        for path, mode in sorted(modes.items(), key=lambda pair: len(pair[0].parts)):
            path.chmod(mode)


@pytest.mark.parametrize("generated_source", [{"image_only": True}], indirect=True)
def test_native_png_only_image_renders_in_render_and_default_bundle(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, store = generated_source
    png = _solid_png((0, 240, 0))
    _add_image_attachment(source, png)
    store.write(_native_image(0))
    environment = _environment(tmp_path)
    captured = _command(["sync", "--library", str(source)], environment)

    rendered_path = tmp_path / "native-image-only.png"
    rendered = _command(_native_render(1, rendered_path), environment)
    annotations = rendered["native_annotations"]
    assert annotations["rendered_objects"] == 1
    assert annotations["rendered_images"] == 1
    assert annotations["rendered_strokes"] == 0
    assert annotations["unsupported_objects"] == 0
    assert annotations["coverage"] == "partial"
    assert rendered["includes_handwriting"] is False
    assert rendered["snapshot"] == captured["snapshot_id"]
    assert _pixel(rendered_path, 65, 35)[:3] == (0, 240, 0)

    bundle_directory = tmp_path / "native-image-bundle"
    bundle = _command(
        [
            "bundle",
            NOTEBOOK,
            "--pages",
            "1",
            "--dpi",
            "72",
            "--output",
            str(bundle_directory),
        ],
        environment,
    )
    manifest = json.loads(Path(bundle["manifest"]).read_text())
    entry = manifest["pages"][0]
    assert bundle["layer"] == "native"
    assert entry["render"]["native_annotations"]["rendered_images"] == 1
    assert entry["render"]["native_annotations"]["rendered_strokes"] == 0
    assert entry["render"]["includes_handwriting"] is False
    assert _pixel(bundle_directory / entry["image"], 65, 35)[:3] == (0, 240, 0)


@pytest.mark.parametrize(
    ("image_sequence", "expected_pixel"),
    [(0, (255, 0, 0)), (2, (0, 240, 0))],
)
def test_native_image_and_ink_keep_creation_order(
    image_sequence: int,
    expected_pixel: tuple[int, int, int],
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
) -> None:
    source, store = generated_source
    _add_image_attachment(source, _solid_png((0, 240, 0)))
    store.write(_native_image(0, sequence=image_sequence))
    environment = _environment(tmp_path)
    _command(["sync", "--library", str(source)], environment)

    output = tmp_path / f"native-order-{image_sequence}.png"
    rendered = _command(_native_render(1, output), environment)

    assert rendered["native_annotations"]["rendered_strokes"] == 1
    assert rendered["native_annotations"]["rendered_images"] == 1
    assert _pixel(output, 65, 30)[:3] == expected_pixel


def _watch_event(process: subprocess.Popen[str], *, timeout: float = 15) -> dict:
    assert process.stdout is not None
    ready, _, _ = select.select([process.stdout], [], [], timeout)
    assert ready, "The watcher did not report an event before the deadline"
    line = process.stdout.readline()
    assert line, f"Watcher exited before reporting an event, return code {process.poll()}"
    event = json.loads(line)
    assert event.get("event") != "error", event
    return event


def test_native_reader_keeps_old_generation_until_render_finishes(
    generated_source: tuple[Path, GeneratedNativeStore],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    monkeypatch.setenv("GOODNOTES_SCRATCH_DIR", environment["GOODNOTES_SCRATCH_DIR"])
    cache = tmp_path / "cache"
    first = _command(["--verbose", "sync", "--library", str(source)], environment)
    refresher: subprocess.Popen[str] | None = None
    try:
        with read_view(Settings(data_dir=cache)) as sandbox:
            original_snapshot = _manifest(sandbox.root)
            store.write(_stroke(0, offset_y=110))
            source_after_edit = _manifest(source)
            refresher = subprocess.Popen(
                [sys.executable, "-m", "goodnotes_cli", "sync", "--library", str(source)],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                completed = [
                    path
                    for path in (cache / "snapshots").iterdir()
                    if not path.name.startswith(".")
                    and (path / "goodnotes-snapshot.json").is_file()
                ]
                if len(completed) == 2:
                    break
                assert refresher.poll() is None, "Refresh exited while its old reader was pinned"
                time.sleep(0.01)
            else:
                pytest.fail("Refresh did not capture its replacement generation")
            assert refresher.poll() is None
            pointer = json.loads((cache / "active.json").read_text())
            assert pointer["snapshot_id"] == first["snapshot_id"]
            old_image = tmp_path / "pinned-old-page.png"
            with Library(sandbox) as library:
                rendered = render_page(
                    sandbox, library.pages(NOTEBOOK)[0], old_image, layer="native", dpi=72
                )
            native_metadata = rendered["native_annotations"]
            assert isinstance(native_metadata, dict)
            assert native_metadata["rendered_strokes"] == 1
            assert _pixel(old_image, 60, 30)[:3] == (255, 0, 0)
            assert _pixel(old_image, 60, 90)[:3] == (255, 255, 255)
            assert _manifest(sandbox.root) == original_snapshot
            assert _manifest(source) == source_after_edit
        stdout, stderr = refresher.communicate(timeout=15)
        assert refresher.returncode == 0, stderr
        replacement = json.loads(stdout)
        assert replacement["snapshot_id"] != first["snapshot_id"]
        assert not Path(first["snapshot"]).exists()
        fresh_image = tmp_path / "fresh-page.png"
        fresh = _command(_native_render(1, fresh_image), environment)
        assert fresh["snapshot"] == replacement["snapshot_id"]
        assert _pixel(fresh_image, 60, 90)[:3] == (255, 0, 0)
        assert not list((tmp_path / "scratch").iterdir())
    finally:
        if refresher is not None and refresher.poll() is None:
            refresher.kill()
            refresher.communicate(timeout=15)


def test_watch_refreshes_native_wal_edits_moves_and_erasure_without_manual_sync(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, store = generated_source
    environment = _environment(tmp_path)
    watcher = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "goodnotes_cli",
            "--verbose",
            "watch",
            "--library",
            str(source),
            "--interval",
            "0.05",
            "--debounce",
            "0.05",
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = _watch_event(watcher)
        assert ready["event"] == "ready"
        first = tmp_path / "first.png"
        _command(_native_render(1, first), environment)
        assert _pixel(first, 60, 30)[:3] == (255, 0, 0)

        # The writer remains open. These changes are committed to the native WAL,
        # while the projection and PDF attachment stay unchanged.
        store.write(_stroke(0, offset_y=110))
        moved_event = _watch_event(watcher)
        assert moved_event["event"] == "updated"
        assert moved_event["snapshot_id"] != ready["snapshot_id"]
        assert moved_event["capture_stats"]["cache_reused_files"] >= 2
        moved = tmp_path / "moved.png"
        moved_result = _command(_native_render(1, moved), environment)
        assert moved_result["snapshot"] == moved_event["snapshot_id"]
        assert _pixel(moved, 60, 30)[:3] == (255, 255, 255)
        assert _pixel(moved, 60, 90)[:3] == (255, 0, 0)

        store.write(_stroke(0, erased=True))
        erased_event = _watch_event(watcher)
        assert erased_event["event"] == "updated"
        erased = tmp_path / "erased.png"
        erased_result = _command(_native_render(1, erased), environment)
        assert erased_result["native_annotations"]["erased_objects"] == 1
        assert erased_result["native_annotations"]["rendered_strokes"] == 0
        assert erased_result["includes_handwriting"] is False
        assert _pixel(erased, 60, 30)[:3] == (255, 255, 255)
        assert _pixel(erased, 60, 90)[:3] == (255, 255, 255)
        generations = [
            path
            for path in (tmp_path / "cache" / "snapshots").iterdir()
            if path.is_dir() and not path.name.startswith(".")
        ]
        assert len(generations) == 1
    finally:
        watcher.send_signal(signal.SIGINT)
        stdout, stderr = watcher.communicate(timeout=15)
        assert watcher.returncode == 130, (watcher.returncode, stdout, stderr)
        assert stderr == ""
        assert not list((tmp_path / "cache" / "snapshots").glob(".staging-*"))
        assert not list((tmp_path / "scratch").iterdir())
