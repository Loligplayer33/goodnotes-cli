"""Generated study sessions through a selectable installed console executable.

These journeys exercise evidence selection and refresh, not Goodnotes app fidelity.
Only the synthetic writer edits source inputs; the target CLI only copies them.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest
from native_fixture import (
    NOTEBOOK,
    PAGE_IDS,
    GeneratedNativeStore,
    _add_image_attachment,
    _native_image,
    _solid_png,
    _stroke,
)
from native_fixture import generated_source as generated_source
from native_fixture import native_runtime as native_runtime


class StudyCLI:
    """Run a real console command outside the checkout and count bounded requests."""

    def __init__(self, root: Path, name: str) -> None:
        self.root, self.name = root, name
        self.cwd = root / "caller"
        self.cwd.mkdir()
        configured = os.environ.get("GOODNOTES_VERIFY_CLI")
        self.executable = (
            Path(configured) if configured else Path(sys.executable).with_name("goodnotes")
        )
        assert self.executable.is_absolute() and self.executable.is_file()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("GOODNOTES_") and key not in {"PYTHONPATH", "PYTHONHOME"}
        }
        # Runtime selection is the sole host-specific native setting we preserve.
        if runtime := os.environ.get("GOODNOTES_ROCKSDB_LIBRARY"):
            self.environment["GOODNOTES_ROCKSDB_LIBRARY"] = runtime
        self.cache = root / "cache"
        self.scratch = root / "scratch"
        self.scratch.mkdir()
        self.environment.update(
            GOODNOTES_DATA_DIR=str(self.cache),
            GOODNOTES_SCRATCH_DIR=str(self.scratch),
            PYTHONUNBUFFERED="1",
        )
        self.requests: list[dict[str, Any]] = []
        self.stdout_bytes = 0
        self.started = time.monotonic()

    def call(
        self,
        arguments: list[str],
        *,
        snapshot: str | None = None,
        error: str | None = None,
        pages: list[int] | None = None,
    ) -> dict[str, Any]:
        prefix = ["--expect-snapshot", snapshot] if snapshot else []
        result = subprocess.run(
            [str(self.executable), *prefix, *arguments],
            cwd=self.cwd,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=45,
        )
        self.stdout_bytes += len(result.stdout.encode())
        command = arguments[1] if arguments[0] == "--verbose" else arguments[0]
        self.requests.append({"command": command, "pages": pages or []})
        if error is not None:
            assert result.returncode == 1, result.stderr or result.stdout
            assert result.stdout == ""
            payload = json.loads(result.stderr)
            assert payload["error"]["code"] == error, payload
            assert len(result.stderr.splitlines()) == 1
            return payload
        assert result.returncode == 0, result.stderr or result.stdout
        assert result.stderr == ""
        assert len(result.stdout.splitlines()) == 1
        return json.loads(result.stdout)

    def sync(self, source: Path) -> dict[str, Any]:
        before = _manifest(source)
        captured = self.call(["--verbose", "sync", "--library", str(source)])
        assert _manifest(source) == before
        return captured

    def bundle(self, pages: list[int], snapshot: str, *, layer: str = "native") -> dict[str, Any]:
        return self.call(
            [
                "bundle",
                NOTEBOOK,
                "--pages",
                ",".join(map(str, pages)),
                "--dpi",
                "72",
                "--layer",
                layer,
            ],
            snapshot=snapshot,
            pages=pages,
        )

    def record_metrics(self) -> None:
        if not (configured := os.environ.get("GOODNOTES_VERIFY_METRICS")):
            return
        target = Path(configured)
        assert target.is_absolute()
        repository = Path(__file__).resolve().parents[1]
        assert not target.resolve().is_relative_to(repository)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a+") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            content = stream.read()
            payload = json.loads(content) if content else {}
            payload.setdefault("journeys", []).append(
                {
                    "name": self.name,
                    "duration_seconds": round(time.monotonic() - self.started, 3),
                    "tool_calls": len(self.requests),
                    "stdout_bytes": self.stdout_bytes,
                    "requests": self.requests,
                }
            )
            stream.seek(0)
            stream.truncate()
            json.dump(payload, stream, separators=(",", ":"))


@pytest.fixture
def study_cli(tmp_path: Path, request: pytest.FixtureRequest) -> Iterator[StudyCLI]:
    cli = StudyCLI(tmp_path, request.node.name)
    yield cli
    cli.record_metrics()


def _manifest(root: Path) -> tuple[tuple[str, int, int, int, int, str | None], ...]:
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


def _preserved(source: Path, captured: dict[str, Any]) -> tuple:
    return _manifest(source), _manifest(Path(captured["snapshot"]))


def _assert_preserved(cli: StudyCLI, source: Path, captured: dict[str, Any], before: tuple) -> None:
    assert (_manifest(source), _manifest(Path(captured["snapshot"]))) == before
    assert list(cli.scratch.iterdir()) == []


def _pixel(evidence: dict[str, Any], x: int, y: int) -> tuple[int, ...]:
    return fitz.Pixmap(evidence["image"]).pixel(x, y)[:3]


def _assert_evidence(evidence: dict[str, Any], page_id: str, *, strokes: int, images: int) -> None:
    assert evidence["page_id"] == page_id
    assert evidence["image_source"] == "copied-native-records-and-background"
    assert evidence["annotation_status"] == "native-reconstructed-approximate"
    assert evidence["includes_handwriting"] is (strokes > 0)
    native = evidence["native_annotations"]
    assert native["native_objects"] == strokes + images
    assert native["rendered_objects"] == strokes + images
    assert native["rendered_strokes"] == strokes
    assert native["rendered_images"] == images
    assert native["rendered_paths"] == 0
    assert native["erased_objects"] == native["deleted_objects"] == 0
    assert native["unsupported_objects"] == 0
    assert native["unsupported_reasons"] == {}
    assert native["full_fidelity_verified"] is False
    assert native["coverage"] in {"supported-pen-records", "partial"}
    assert evidence["warnings"]
    assert evidence["parts"]
    assert all(part["source"] == "pdf" for part in evidence["parts"])


def test_study_export_discuss_edit_refresh_selected_pages(
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    cli = study_cli
    _add_image_attachment(source, _solid_png((0, 240, 0)))
    store.write(_native_image(0))
    store.close()
    initial = cli.sync(source)
    first_id = initial["snapshot_id"]
    before = _preserved(source, initial)
    exported = cli.root / "complete-annotated.pdf"
    full_export = cli.call(
        ["--verbose", "export", NOTEBOOK, "--layer", "native", "--output", str(exported)],
        snapshot=first_id,
        pages=[1, 2],
    )
    assert full_export["pages"] == 2
    assert full_export["annotation_status"] == "native-reconstructed-approximate"
    assert full_export["includes_handwriting"] is True
    assert full_export["warnings"]
    assert [entry["page_id"] for entry in full_export["native_annotations"]] == list(PAGE_IDS)
    assert full_export["source"] == "copied-native-records-and-backgrounds"
    for native, images in zip(full_export["native_annotations"], [1, 0], strict=True):
        assert native["native_objects"] == native["rendered_objects"] == 1 + images
        assert native["rendered_strokes"] == 1
        assert native["rendered_images"] == images
        assert native["unsupported_objects"] == 0
        assert native["erased_objects"] == native["deleted_objects"] == 0
        assert native["full_fidelity_verified"] is False
    export_hash = hashlib.sha256(exported.read_bytes()).hexdigest()
    with fitz.open(exported) as pdf:
        assert len(pdf) == 2
        assert "Generated background 1" in pdf[0].get_text()
        assert "Generated background 2" in pdf[1].get_text()
        assert pdf[0].get_pixmap().pixel(45, 30)[:3] == (255, 0, 0)
        assert pdf[0].get_pixmap().pixel(75, 45)[:3] == (0, 240, 0)
        assert pdf[1].get_pixmap().pixel(60, 90)[:3] == (0, 0, 255)
    discussion = cli.bundle([1], first_id)
    assert discussion["pages"] == [1]
    assert discussion["snapshot"] == first_id
    selected = discussion["page_evidence"][0]
    assert selected["page_id"] == PAGE_IDS[0]
    _assert_evidence(selected, PAGE_IDS[0], strokes=1, images=1)
    assert _pixel(selected, 45, 30) == (255, 0, 0)
    assert _pixel(selected, 75, 45) == (0, 240, 0)
    assert "Generated background 1" in selected["parts"][0]["text"]
    _assert_preserved(cli, source, initial, before)

    # The generated writer models an edit arriving from another device. The
    # initial whole-notebook export stays unchanged; retrieve current selected evidence.
    edited_store = GeneratedNativeStore(store.runtime, source / "Databases" / "notes_main.db")
    try:
        edited_store.write(_stroke(0, offset_y=110))
    finally:
        edited_store.close()
    second = cli.sync(source)
    second_id = second["snapshot_id"]
    assert second_id != first_id
    after_edit = _preserved(source, second)
    current = cli.bundle([1], second_id)
    assert current["pages"] == [1]
    assert current["text_source"] == "pdf"
    assert current["truncated"] is False
    assert current["snapshot"] == second_id
    evidence = current["page_evidence"][0]
    _assert_evidence(evidence, PAGE_IDS[0], strokes=1, images=1)
    assert "Generated background 1" in evidence["parts"][0]["text"]
    assert _pixel(evidence, 45, 30) == (255, 255, 255)
    assert _pixel(evidence, 45, 90) == (255, 0, 0)
    assert _pixel(evidence, 75, 45) == (0, 240, 0)
    assert hashlib.sha256(exported.read_bytes()).hexdigest() == export_hash
    exports = [request for request in cli.requests if request["command"] == "export"]
    assert exports == [{"command": "export", "pages": [1, 2]}]
    assert cli.requests[-1] == {"command": "bundle", "pages": [1]}
    _assert_preserved(cli, source, second, after_edit)


def test_study_stable_identity_stale_snapshot_deleted_page_and_expired_artifact(
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    store.close()
    cli = study_cli
    first = cli.sync(source)
    first_id = first["snapshot_id"]
    before = _preserved(source, first)
    initial_pages = cli.call(["pages", NOTEBOOK], snapshot=first_id)
    assert [(page["id"], page["number"]) for page in initial_pages["pages"]] == [
        (PAGE_IDS[0], 1),
        (PAGE_IDS[1], 2),
    ]
    _assert_preserved(cli, source, first, before)

    inserted_id = "00000000-0000-4000-8000-000000000a00"
    with sqlite3.connect(source / "Databases" / "projection.sqlite") as db:
        db.execute(
            "UPDATE documents SET name = 'Renamed generated notes' WHERE id = ?", (NOTEBOOK,)
        )
        db.execute("INSERT INTO folders VALUES ('generated-folder', 'Generated course', 1, 0, 'a')")
        db.execute(
            "INSERT INTO folder_to_folder_items VALUES "
            "('generated-folder-link', 0, 'generated-folder', '', 'generated-root', 'a', 0)"
        )
        db.execute(
            "UPDATE folder_to_folder_items SET parent_folder_id = 'generated-folder' "
            "WHERE item_id = ?",
            (NOTEBOOK,),
        )
        # Insertion plus reorder makes the previous page 2 become current page 1.
        db.execute("UPDATE pages SET position = 'c' WHERE id = ?", (PAGE_IDS[0],))
        db.execute("UPDATE pages SET position = 'a' WHERE id = ?", (PAGE_IDS[1],))
        db.execute(
            "INSERT INTO pages VALUES (?, ?, 'generated-template-1', 'b', '', 0)",
            (inserted_id, NOTEBOOK),
        )
    second = cli.sync(source)
    second_id = second["snapshot_id"]
    assert second_id != first_id
    before = _preserved(source, second)
    stale = cli.call(["pages", NOTEBOOK], snapshot=first_id, error="stale_snapshot")
    assert stale["error"]["message"]
    info = cli.call(["info", NOTEBOOK], snapshot=second_id)
    assert info["id"] == NOTEBOOK
    assert info["path"] == "Generated course/Renamed generated notes"
    same = cli.call(["info", info["path"]], snapshot=second_id)
    assert same["id"] == NOTEBOOK
    reordered = cli.call(["pages", NOTEBOOK], snapshot=second_id)
    numbers = {page["id"]: page["number"] for page in reordered["pages"]}
    assert numbers == {PAGE_IDS[1]: 1, inserted_id: 2, PAGE_IDS[0]: 3}
    selected = cli.bundle([numbers[PAGE_IDS[0]], numbers[PAGE_IDS[1]]], second_id)
    assert selected["pages"] == [3, 1]
    red, blue = selected["page_evidence"]
    _assert_evidence(red, PAGE_IDS[0], strokes=1, images=0)
    _assert_evidence(blue, PAGE_IDS[1], strokes=1, images=0)
    assert "Generated background 1" in red["parts"][0]["text"]
    assert "Generated background 2" in blue["parts"][0]["text"]
    assert _pixel(red, 60, 30) == (255, 0, 0)
    assert _pixel(blue, 60, 90) == (0, 0, 255)
    _assert_preserved(cli, source, second, before)

    with sqlite3.connect(source / "Databases" / "projection.sqlite") as db:
        db.execute("UPDATE pages SET deleted = 1 WHERE id = ?", (PAGE_IDS[0],))
    third = cli.sync(source)
    third_id = third["snapshot_id"]
    assert third_id != second_id
    before = _preserved(source, third)
    remaining = cli.call(["pages", NOTEBOOK], snapshot=third_id)
    remaining_ids = {page["id"] for page in remaining["pages"]}
    assert remaining_ids == {PAGE_IDS[1], inserted_id}
    assert PAGE_IDS[0] not in remaining_ids  # The client must stop following this identity.
    missing = cli.call(
        ["bundle", NOTEBOOK, "--pages", "3"],
        snapshot=third_id,
        error="operation_failed",
        pages=[3],
    )
    assert "page" in missing["error"]["message"].lower()
    recovered = cli.bundle([1], third_id)
    assert recovered["page_evidence"][0]["page_id"] == PAGE_IDS[1]
    image = Path(recovered["page_evidence"][0]["image"])
    expected_hash = hashlib.sha256(image.read_bytes()).hexdigest()
    # Age only CLI-owned artifact leases, then let the target CLI perform real
    # expiry maintenance. Neither source nor completed snapshot is modified.
    expired = time.time() - 90_000
    for marker in (cli.cache / "artifacts" / "entries").glob("*/.artifact.json"):
        os.utime(marker, (expired, expired))
    cli.call(
        ["render", NOTEBOOK, "--page", "2", "--output", str(cli.root / "persistent.png")],
        snapshot=third_id,
        pages=[2],
    )
    # An artifact operation on another key triggers eviction before recovery.
    cli.call(["render", NOTEBOOK, "--page", "2", "--dpi", "72"], snapshot=third_id, pages=[2])
    assert not image.exists()
    repeated = cli.bundle([1], third_id)
    assert repeated["snapshot"] == third_id
    assert repeated["page_evidence"][0]["page_id"] == PAGE_IDS[1]
    assert (
        hashlib.sha256(Path(repeated["page_evidence"][0]["image"]).read_bytes()).hexdigest()
        == expected_hash
    )
    assert _pixel(repeated["page_evidence"][0], 60, 90) == (0, 0, 255)
    assert (cli.root / "persistent.png").is_file()
    _assert_preserved(cli, source, third, before)


@pytest.mark.parametrize("unavailable", ["runtime", "native-records", "annotated-registration"])
def test_study_visual_evidence_never_silently_falls_back(
    unavailable: str,
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    snapshot = captured["snapshot_id"]
    before = _preserved(source, captured)
    if unavailable == "runtime":
        cli.environment["GOODNOTES_ROCKSDB_LIBRARY"] = str(cli.root / "missing-engine.dylib")
        error, layer, page = "native_runtime_unavailable", "native", 1
    elif unavailable == "native-records":
        # Only an independently generated catalog page is added. It has a valid
        # PDF binding but deliberately has no native page root.
        with sqlite3.connect(source / "Databases" / "projection.sqlite") as db:
            db.execute(
                "INSERT INTO pages VALUES ('00000000-0000-4000-8000-000000000b00', "
                "?, 'generated-template-1', '3', '', 0)",
                (NOTEBOOK,),
            )
        captured = cli.sync(source)
        snapshot = captured["snapshot_id"]
        before = _preserved(source, captured)
        error, layer, page = "native_data_unavailable", "native", 3
    else:
        error, layer, page = "operation_failed", "annotated", 1
    failed = cli.call(
        ["bundle", NOTEBOOK, "--pages", str(page), "--layer", layer],
        snapshot=snapshot,
        error=error,
        pages=[page],
    )
    assert failed["error"]["message"]
    clean = cli.bundle([page], snapshot, layer="background")
    evidence = clean["page_evidence"][0]
    assert clean["layer"] == "background"
    assert evidence["includes_handwriting"] is False
    assert evidence["warnings"]
    assert "native_annotations" not in evidence
    assert _pixel(evidence, 60, 30) == (255, 255, 255)
    _assert_preserved(cli, source, captured, before)
