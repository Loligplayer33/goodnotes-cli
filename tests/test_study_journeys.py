"""Generated study sessions through a selectable installed console executable.

These journeys exercise evidence selection and refresh, not Goodnotes app fidelity.
Only the synthetic writer edits source inputs; the target CLI only copies them.
"""

from __future__ import annotations

import fcntl
import hashlib
import importlib.util
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
    UNSUPPORTED_PAGE_ID,
    GeneratedNativeStore,
    _add_generated_page,
    _add_image_attachment,
    _add_variant_attachments,
    _native_filled_path_records,
    _native_image,
    _native_image_layout_records,
    _native_ink_records,
    _native_open_two_point_shape_records,
    _native_unknown_object_type_records,
    _native_variable_brush_records,
    _native_variant_records,
    _solid_png,
    _stroke,
)
from native_fixture import generated_source as generated_source
from native_fixture import native_runtime as native_runtime

_REFERENCE_SPEC = importlib.util.spec_from_file_location(
    "verification_reference",
    Path(__file__).resolve().parents[1] / "scripts" / "verification_reference.py",
)
assert _REFERENCE_SPEC is not None and _REFERENCE_SPEC.loader is not None
_REFERENCE_MODULE = importlib.util.module_from_spec(_REFERENCE_SPEC)
_REFERENCE_SPEC.loader.exec_module(_REFERENCE_MODULE)
compare_reference = _REFERENCE_MODULE.compare_reference


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

    def bundle(
        self,
        pages: list[int],
        snapshot: str,
        *,
        layer: str = "native",
        output: Path | None = None,
        verbose: bool = False,
    ) -> dict[str, Any]:
        arguments = [
            "bundle",
            NOTEBOOK,
            "--pages",
            ",".join(map(str, pages)),
            "--dpi",
            "72",
            "--layer",
            layer,
        ]
        if verbose:
            arguments.insert(0, "--verbose")
        if output is not None:
            arguments.extend(["--output", str(output)])
        return self.call(
            arguments,
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


def _assert_no_cached_artifacts(cli: StudyCLI) -> None:
    artifact_root = cli.cache / "artifacts"
    assert all((artifact_root / name).is_dir() for name in ("entries", "blobs", ".staging"))
    assert all(
        not list((artifact_root / name).iterdir()) for name in ("entries", "blobs", ".staging")
    )


def _assert_native_partial(
    evidence: dict[str, Any], page_id: str, reason: str, *, x: int = 60, y: int = 30
) -> None:
    assert evidence["page_id"] == page_id
    assert evidence["image_source"] == "copied-native-records-and-background"
    assert evidence["annotation_status"] == "native-reconstructed-approximate"
    assert evidence["includes_handwriting"] is True
    native = evidence["native_annotations"]
    assert native["coverage"] == "partial"
    assert native["full_fidelity_verified"] is False
    assert native["native_objects"] == 2
    assert native["rendered_objects"] == 1
    assert native["rendered_strokes"] == 1
    assert native["unsupported_objects"] == 1
    assert native["unsupported_reasons"] == {reason: 1}
    assert any("could not be reconstructed" in warning for warning in evidence["warnings"])
    # The independent generated stroke remains visible even when its neighbor
    # has a future or otherwise unsupported representation.
    assert _pixel(evidence, x, y) == (255, 0, 0)


_NATIVE_REJECTIONS = (
    ("alternate-shape-envelope", "unsupported-native-geometry-envelope"),
    ("shape-with-transform", "unsupported-native-geometry-field-6"),
    ("unknown-object-type", "unsupported-object-type"),
)


def _native_rejection_records(page_id: str, variant: str) -> dict[bytes, bytes]:
    if variant == "unknown-object-type":
        return _native_unknown_object_type_records(page_id)
    return _native_variant_records(page_id, variant)


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


_NATIVE_SCHEMA_CASES = (
    "translated-ink",
    "highlighter-style-ink",
    "direct-identity-ink",
    "filled-path",
    "outlined-ellipse",
    "open-two-point-shape",
    "translated-open-two-point-shape",
    "variable-brush",
    "image-explicit-binding-present-field6",
    "image-explicit-binding-absent-field6",
    "image-explicit-binding-clipped-edge-field6",
    "image-body-resource-present-field6",
    "image-body-resource-absent-field6",
    "image-descriptor-resource-present-field6",
    "image-descriptor-resource-absent-field6",
    "image-issue-body-field-6-absent",
    "image-issue-descriptor-resource-without-attachment",
    "image-issue-combined-omitted-field-6",
)
_SCALE = 72 / 132


def _install_native_schema_case(source: Path, store: GeneratedNativeStore, case: str) -> None:
    if case == "translated-ink":
        records = _native_ink_records(
            PAGE_IDS[0],
            points=((110.0, 70.0), (180.0, 70.0)),
            width=6.0,
            color=(0.0, 0.8, 0.1, 1.0),
            translation=(20.0, 15.0),
        )
    elif case == "highlighter-style-ink":
        records = _native_ink_records(
            PAGE_IDS[0],
            points=((110.0, 135.0), (180.0, 135.0)),
            width=18.0,
            color=(1.0, 0.8, 0.0, 0.4),
            highlighter_style=True,
        )
    elif case == "direct-identity-ink":
        records = _native_ink_records(
            PAGE_IDS[0],
            points=((110.0, 110.0), (180.0, 110.0)),
            width=4.0,
            color=(0.5, 0.0, 0.5, 1.0),
            direct_identity=True,
        )
    elif case == "filled-path":
        records = _native_filled_path_records(PAGE_IDS[0])
    elif case == "outlined-ellipse":
        records = _native_variant_records(PAGE_IDS[0], "supported-ellipse-control")
    elif case in {"open-two-point-shape", "translated-open-two-point-shape"}:
        records = _native_open_two_point_shape_records(
            PAGE_IDS[0],
            points=((110.0, 120.0), (180.0, 120.0)),
            translation=(20.0, 25.0) if case == "translated-open-two-point-shape" else (0.0, 0.0),
        )
    elif case == "variable-brush":
        records = _native_variable_brush_records(PAGE_IDS[0])
    elif case.startswith("image-"):
        _add_variant_attachments(source)
        if case == "image-issue-body-field-6-absent":
            records = _native_variant_records(PAGE_IDS[0], "image-body-missing-field-6")
        elif case == "image-issue-descriptor-resource-without-attachment":
            records = _native_variant_records(PAGE_IDS[0], "descriptor-resource-without-attachment")
        elif case == "image-issue-combined-omitted-field-6":
            records = _native_variant_records(PAGE_IDS[0], "observed-combined-image-shape")
        else:
            if case.startswith("image-explicit-binding"):
                layout = "explicit-attachment"
            elif case.startswith("image-body-resource"):
                layout = "body-resource"
            elif case.startswith("image-descriptor-resource"):
                layout = "descriptor-resource"
            else:
                raise ValueError(f"Unknown generated image schema case: {case}")
            records = _native_image_layout_records(
                PAGE_IDS[0],
                descriptor_layout=layout,
                body_field_6=None if case.endswith("absent-field6") else 1,
                bounds=(420.0, 55.0, 55.0, 55.0)
                if case == "image-explicit-binding-clipped-edge-field6"
                else (110.0, 55.0, 55.0, 55.0),
            )
    else:
        raise ValueError(f"Unknown generated native schema case: {case}")
    store.write(records)


def _draw_reference_stroke(page: fitz.Page, *, color: tuple[float, float, float], y: float) -> None:
    page.draw_line((30, y), (90, y), color=color, width=36 / 11, lineCap=1)


def _draw_reference_case(page: fitz.Page, case: str, *, include_case: bool = True) -> None:
    _draw_reference_stroke(page, color=(1.0, 0.0, 0.0), y=30)
    if not include_case:
        return

    if case == "translated-ink":
        page.draw_line(
            (130 * _SCALE, 85 * _SCALE),
            (200 * _SCALE, 85 * _SCALE),
            color=(0.0, 0.8, 0.1),
            width=6 * _SCALE,
            lineCap=1,
        )
    elif case == "highlighter-style-ink":
        page.draw_line(
            (110 * _SCALE, 135 * _SCALE),
            (180 * _SCALE, 135 * _SCALE),
            color=(1.0, 0.8, 0.0),
            width=18 * _SCALE,
            lineCap=1,
            stroke_opacity=0.4,
        )
    elif case == "direct-identity-ink":
        page.draw_line(
            (110 * _SCALE, 110 * _SCALE),
            (180 * _SCALE, 110 * _SCALE),
            color=(0.5, 0.0, 0.5),
            width=4 * _SCALE,
            lineCap=1,
        )
    elif case == "filled-path":
        page.draw_rect(
            fitz.Rect(150 * _SCALE, 90 * _SCALE, 180 * _SCALE, 120 * _SCALE),
            color=None,
            fill=(0.1, 0.7, 0.2),
            fill_opacity=0.8,
        )
    elif case == "outlined-ellipse":
        center_x, center_y, radius_x, radius_y = 170.0, 115.0, 25.0, 15.0
        page.draw_oval(
            fitz.Rect(
                (center_x - radius_y) * _SCALE,
                (center_y - radius_x) * _SCALE,
                (center_x + radius_y) * _SCALE,
                (center_y + radius_x) * _SCALE,
            ),
            color=(0.1, 0.3, 0.5),
            width=2.0 * _SCALE,
            stroke_opacity=0.75,
        )
    elif case in {"open-two-point-shape", "translated-open-two-point-shape"}:
        dx, dy = (20.0, 25.0) if case == "translated-open-two-point-shape" else (0.0, 0.0)
        page.draw_line(
            ((110.0 + dx) * _SCALE, (120.0 + dy) * _SCALE),
            ((180.0 + dx) * _SCALE, (120.0 + dy) * _SCALE),
            color=(0.4, 0.2, 0.8),
            width=3.0 * _SCALE,
            lineCap=1,
            stroke_opacity=0.75,
        )
    elif case == "variable-brush":
        points = ((100.0, 130.0), (120.0, 130.0), (160.0, 130.0))
        widths = (2.0, 2.0, 4.0)
        left = tuple(
            (x * _SCALE, (y + width / 2) * _SCALE)
            for (x, y), width in zip(points, widths, strict=True)
        )
        right = tuple(
            (x * _SCALE, (y - width / 2) * _SCALE)
            for (x, y), width in reversed(tuple(zip(points, widths, strict=True)))
        )
        shape = page.new_shape()
        shape.draw_polyline([*left, *right])
        shape.finish(color=None, fill=(0.2, 0.4, 0.6), closePath=True, fill_opacity=0.5)
        shape.commit()
        for (x, y), width in zip((points[0], points[-1]), (widths[0], widths[-1]), strict=True):
            page.draw_oval(
                fitz.Rect(
                    (x - width / 2) * _SCALE,
                    (y - width / 2) * _SCALE,
                    (x + width / 2) * _SCALE,
                    (y + width / 2) * _SCALE,
                ),
                color=None,
                fill=(0.2, 0.4, 0.6),
                fill_opacity=0.5,
            )
    elif case.startswith("image-"):
        image_box = (
            fitz.Rect(420 * _SCALE, 55 * _SCALE, 475 * _SCALE, 110 * _SCALE)
            if case == "image-explicit-binding-clipped-edge-field6"
            else fitz.Rect(110 * _SCALE, 55 * _SCALE, 165 * _SCALE, 110 * _SCALE)
        )
        page.insert_image(
            image_box,
            stream=_solid_png((0, 240, 0)),
        )
    else:
        raise ValueError(f"Unknown generated native schema reference: {case}")


def _write_schema_reference(
    source: Path, destination: Path, case: str, *, include_case: bool
) -> None:
    with fitz.open(source / "Attachments" / "generated.pdf") as document:
        _draw_reference_case(document[0], case, include_case=include_case)
        _draw_reference_stroke(document[1], color=(0.0, 0.0, 1.0), y=90)
        document.save(destination)


def _region(box: list[float]) -> dict[str, Any]:
    return {
        "box": box,
        "max_mean_error": 0.025,
        "max_changed_fraction": 0.08,
        "min_foreground_retention": 0.9,
    }


def _schema_regions(case: str, page: int) -> list[dict[str, Any]]:
    if page == 2:
        return [_region([0.10, 0.47, 0.40, 0.54])]
    case_boxes = {
        "translated-ink": [0.28, 0.25, 0.47, 0.30],
        "highlighter-style-ink": [0.23, 0.36, 0.43, 0.46],
        "direct-identity-ink": [0.23, 0.30, 0.43, 0.37],
        "filled-path": [0.30, 0.25, 0.43, 0.38],
        "outlined-ellipse": [0.34, 0.26, 0.44, 0.44],
        "open-two-point-shape": [0.25, 0.33, 0.40, 0.36],
        "translated-open-two-point-shape": [0.28, 0.43, 0.47, 0.46],
        "variable-brush": [0.21, 0.36, 0.39, 0.42],
    }
    if case == "image-explicit-binding-clipped-edge-field6":
        case_box = [0.93, 0.15, 1.0, 0.35]
    else:
        case_box = [0.24, 0.15, 0.39, 0.35] if case.startswith("image-") else case_boxes[case]
    return [_region([0.10, 0.13, 0.40, 0.21]), _region(case_box)]


def _compare_schema_page(
    actual: Path, reference: Path, background: Path, case: str, page: int
) -> dict[str, Any]:
    return compare_reference(
        actual,
        reference,
        regions=_schema_regions(case, page),
        dpi=72,
        reference_page=page,
        background=background,
        background_page=page,
        pixel_tolerance=0.1,
    )


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


def test_study_supported_ellipse_control_renders_through_installed_cli(
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    store.write(_native_variant_records(PAGE_IDS[0], "supported-ellipse-control"))
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    before = _preserved(source, captured)

    bundle = cli.bundle([1], captured["snapshot_id"])
    evidence = bundle["page_evidence"][0]
    native = evidence["native_annotations"]
    assert evidence["annotation_status"] == "native-reconstructed-approximate"
    assert evidence["image_source"] == "copied-native-records-and-background"
    assert evidence["includes_handwriting"] is True
    assert native["native_objects"] == native["rendered_objects"] == 2
    assert native["rendered_strokes"] == 2
    assert native["rendered_paths"] == 1
    assert native["unsupported_objects"] == 0
    assert native["unsupported_reasons"] == {}
    assert native["full_fidelity_verified"] is False
    assert native["coverage"] == "partial"

    pixmap = fitz.Pixmap(evidence["image"])
    ellipse_region = [pixmap.pixel(x, y)[:3] for x in range(80, 108) for y in range(43, 82)]
    assert any(pixel != (255, 255, 255) for pixel in ellipse_region)
    _assert_preserved(cli, source, captured, before)


@pytest.mark.parametrize("case", _NATIVE_SCHEMA_CASES)
@pytest.mark.parametrize("output_mode", ["artifact-cache", "persistent"])
def test_study_supported_native_schema_matrix_matches_independent_references(
    case: str,
    output_mode: str,
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    _install_native_schema_case(source, store, case)
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    snapshot = captured["snapshot_id"]
    before = _preserved(source, captured)

    expected = cli.root / f"expected-{case}.pdf"
    omitted = cli.root / f"omitted-{case}.pdf"
    clean_background = source / "Attachments" / "generated.pdf"
    _write_schema_reference(source, expected, case, include_case=True)
    _write_schema_reference(source, omitted, case, include_case=False)
    missing_image = cli.root / f"omitted-{case}.png"
    with fitz.open(omitted) as document:
        document[0].get_pixmap(dpi=72, colorspace=fitz.csRGB, alpha=False).save(missing_image)
    omission = _compare_schema_page(missing_image, expected, clean_background, case, 1)
    assert omission["status"] == "fail", omission
    assert omission["regions"][2]["status"] == "fail", omission

    output = None
    if output_mode == "persistent":
        output = cli.root / "persistent-schema" / case
        output.parent.mkdir(parents=True)
    first = cli.bundle([1, 2], snapshot, output=output)
    assert first["pages"] == [1, 2]
    assert first["layer"] == "native"
    first_pages = first["page_evidence"]
    assert [entry["page_id"] for entry in first_pages] == list(PAGE_IDS)
    first_native = first_pages[0]["native_annotations"]
    image_case = case.startswith("image-")
    path_case = case in {
        "filled-path",
        "outlined-ellipse",
        "open-two-point-shape",
        "translated-open-two-point-shape",
    }
    assert first_native["native_objects"] == first_native["rendered_objects"] == 2, json.dumps(
        first_native, sort_keys=True
    )
    assert first_native["rendered_strokes"] == (1 if image_case else 2)
    assert first_native["rendered_paths"] == int(path_case)
    assert first_native["rendered_images"] == int(image_case)
    assert first_native["unsupported_objects"] == 0
    assert first_native["unsupported_reasons"] == {}
    assert first_native["full_fidelity_verified"] is False
    absent_image_field_6 = case.startswith("image-") and (
        case.endswith("absent-field6")
        or case
        in {
            "image-issue-body-field-6-absent",
            "image-issue-combined-omitted-field-6",
        }
    )
    assert first_native["native_image_body_field_6_absent_records"] == int(absent_image_field_6)
    assert first_native["coverage"] in {
        "supported-pen-records",
        "supported-native-records",
        "partial",
    }
    assert first_pages[0]["image_source"] == "copied-native-records-and-background"
    assert first_pages[0]["annotation_status"] == "native-reconstructed-approximate"
    assert first_pages[0]["includes_handwriting"] is True
    assert first_pages[0]["warnings"]
    if absent_image_field_6:
        assert first_native["coverage"] == "partial"
        assert (
            "Some decoded native image records omit body field 6; its effect on application "
            "appearance or visibility is unverified." in first_pages[0]["warnings"]
        )
    assert _assert_evidence(first_pages[1], PAGE_IDS[1], strokes=1, images=0) is None

    first_page_comparison = _compare_schema_page(
        Path(first_pages[0]["image"]), expected, clean_background, case, 1
    )
    second_page_comparison = _compare_schema_page(
        Path(first_pages[1]["image"]), expected, clean_background, case, 2
    )
    assert first_page_comparison["status"] == "pass", first_page_comparison
    assert second_page_comparison["status"] == "pass", second_page_comparison

    if output_mode == "artifact-cache":
        first_images = [Path(entry["image"]) for entry in first_pages]
        first_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in first_images]
        entries = sorted(path.name for path in (cli.cache / "artifacts" / "entries").iterdir())
        second = cli.bundle([1, 2], snapshot)
        second_pages = second["page_evidence"]
        assert [Path(entry["image"]) for entry in second_pages] == first_images
        assert [
            hashlib.sha256(Path(entry["image"]).read_bytes()).hexdigest() for entry in second_pages
        ] == first_hashes
        assert [entry["native_annotations"] for entry in second_pages] == [
            entry["native_annotations"] for entry in first_pages
        ]
        assert [entry["warnings"] for entry in second_pages] == [
            entry["warnings"] for entry in first_pages
        ]
        assert (
            sorted(path.name for path in (cli.cache / "artifacts" / "entries").iterdir()) == entries
        )
    else:
        assert output is not None and output.is_dir()
        manifest = json.loads((output / "text.json").read_text())
        assert [entry["page"] for entry in manifest["pages"]] == [1, 2]
        stored_native = manifest["pages"][0]["render"]["native_annotations"]
        assert stored_native["native_image_body_field_6_absent_records"] == int(
            absent_image_field_6
        )
        if absent_image_field_6:
            assert (
                "Some decoded native image records omit body field 6; its effect on application "
                "appearance or visibility is unverified."
                in manifest["pages"][0]["render"]["warnings"]
            )
        assert not list(output.parent.glob(".goodnotes-bundle-*"))

    _assert_preserved(cli, source, captured, before)


def test_study_omitted_image_field_6_metadata_survives_render_export_and_cached_bundle(
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from goodnotes_cli import documents
    from goodnotes_cli.artifacts import ArtifactCache
    from goodnotes_cli.library import Library
    from goodnotes_cli.safety import Sandbox

    source, store = generated_source
    _add_variant_attachments(source)
    store.write(
        _native_image_layout_records(
            PAGE_IDS[0], descriptor_layout="body-resource", body_field_6=None
        )
    )
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    snapshot = captured["snapshot_id"]
    before = _preserved(source, captured)
    sandbox = Sandbox.load(captured["snapshot"])

    def render_stale_background(
        sandbox: Any,
        page: Any,
        output_path: Path,
        *,
        layer: str,
        dpi: int,
        pdf_cache: Any,
        registered: dict[str, Any],
        record_reader: Any,
    ) -> dict[str, object]:
        return original_render(
            sandbox,
            page,
            output_path,
            layer="background",
            dpi=dpi,
            pdf_cache=pdf_cache,
            registered=registered,
            record_reader=None,
        )

    original_render = documents._render_page_with_resources
    stale_version = documents._RENDER_CACHE_VERSION - 1
    with monkeypatch.context() as stale:
        stale.setattr(documents, "_RENDER_CACHE_VERSION", stale_version)
        stale.setattr(documents, "_render_page_with_resources", render_stale_background)
        with Library(sandbox) as library:
            with ArtifactCache(cli.cache / "artifacts", sandbox.root) as cache:
                old_entry = documents.cached_render_pages(
                    sandbox, library.pages(NOTEBOOK)[:1], cache, layer="native", dpi=72
                )[0]
    stale_path = old_entry["path"]
    assert isinstance(stale_path, str)
    stale_image = Path(stale_path)
    assert fitz.Pixmap(stale_image).pixel(50, 30)[:3] == (255, 255, 255)

    rendered = cli.call(
        ["--verbose", "render", NOTEBOOK, "--page", "1", "--layer", "native", "--dpi", "72"],
        snapshot=snapshot,
        pages=[1],
    )
    render_native = rendered["native_annotations"]
    expected_notice = (
        "Some decoded native image records omit body field 6; its effect on application "
        "appearance or visibility is unverified."
    )
    assert render_native["native_image_body_field_6_absent_records"] == 1
    assert expected_notice in rendered["warnings"]
    assert Path(rendered["path"]) != stale_image
    assert fitz.Pixmap(rendered["path"]).pixel(50, 30)[:3] == (255, 0, 0)

    exported = cli.call(
        [
            "--verbose",
            "export",
            NOTEBOOK,
            "--pages",
            "1",
            "--layer",
            "native",
            "--output",
            str(cli.root / "omitted-field6.pdf"),
        ],
        snapshot=snapshot,
        pages=[1],
    )
    export_native = exported["native_annotations"][0]
    assert export_native["native_image_body_field_6_absent_records"] == 1
    assert expected_notice in export_native["warnings"]
    assert expected_notice in exported["warnings"]
    with fitz.open(exported["path"]) as document:
        assert document[0].get_pixmap(dpi=72).pixel(50, 30)[:3] == (255, 0, 0)

    bundle = cli.bundle([1], snapshot)
    evidence = bundle["page_evidence"][0]
    assert evidence["native_annotations"]["native_image_body_field_6_absent_records"] == 1
    assert expected_notice in evidence["warnings"]
    replay = cli.bundle([1], snapshot)
    assert replay["page_evidence"][0]["native_annotations"] == evidence["native_annotations"]
    assert replay["page_evidence"][0]["warnings"] == evidence["warnings"]
    _assert_preserved(cli, source, captured, before)


@pytest.mark.parametrize(
    ("variant", "reason"),
    _NATIVE_REJECTIONS,
)
@pytest.mark.parametrize("output_mode", ["artifact-cache", "persistent"])
def test_study_unsupported_native_variants_keep_partial_mixed_page_output(
    variant: str,
    reason: str,
    output_mode: str,
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    store.write(_native_rejection_records(PAGE_IDS[0], variant))
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    before = _preserved(source, captured)

    output = None
    if output_mode == "persistent":
        output = cli.root / "persistent-mixed" / variant
        output.parent.mkdir(parents=True)
    first = cli.bundle([1], captured["snapshot_id"], output=output)
    first_evidence = first["page_evidence"][0]
    _assert_native_partial(first_evidence, PAGE_IDS[0], reason)

    if output_mode == "artifact-cache":
        first_image = Path(first_evidence["image"])
        first_hash = hashlib.sha256(first_image.read_bytes()).hexdigest()
        cached_entries = sorted(
            path.name for path in (cli.cache / "artifacts" / "entries").iterdir()
        )
        second = cli.bundle([1], captured["snapshot_id"])
        second_evidence = second["page_evidence"][0]
        _assert_native_partial(second_evidence, PAGE_IDS[0], reason)
        assert Path(second_evidence["image"]) == first_image
        assert hashlib.sha256(first_image.read_bytes()).hexdigest() == first_hash
        assert (
            sorted(path.name for path in (cli.cache / "artifacts" / "entries").iterdir())
            == cached_entries
        )
        assert second_evidence["native_annotations"] == first_evidence["native_annotations"]
        assert second_evidence["warnings"] == first_evidence["warnings"]
    else:
        assert output is not None and output.is_dir()
        assert Path(first_evidence["image"]).is_file()
        manifest = json.loads((output / "text.json").read_text())
        assert manifest["pages"][0]["render"]["native_annotations"]["unsupported_reasons"] == {
            reason: 1
        }
        assert not list(output.parent.glob(".goodnotes-bundle-*"))

    _assert_preserved(cli, source, captured, before)


@pytest.mark.parametrize(
    ("variant", "reason"),
    _NATIVE_REJECTIONS,
)
@pytest.mark.parametrize("output_mode", ["artifact-cache", "persistent"])
@pytest.mark.parametrize(
    ("pages", "failed_index"),
    [([3, 1, 2], 0), ([1, 3, 2], 1)],
    ids=["failed-first", "failed-middle"],
)
def test_study_unsupported_only_native_variants_fail_with_page_context_and_cleanup(
    variant: str,
    reason: str,
    output_mode: str,
    pages: list[int],
    failed_index: int,
    generated_source: tuple[Path, GeneratedNativeStore],
    study_cli: StudyCLI,
) -> None:
    source, store = generated_source
    _add_generated_page(source)
    store.write(_native_rejection_records(UNSUPPORTED_PAGE_ID, variant))
    store.close()
    cli = study_cli
    captured = cli.sync(source)
    snapshot = captured["snapshot_id"]
    before = _preserved(source, captured)
    persistent_parent = cli.root / "persistent-failures"
    if output_mode == "persistent":
        persistent_parent.mkdir()

    failed_output = (
        persistent_parent / f"all-failed-{variant}" if output_mode == "persistent" else None
    )
    failed_arguments = ["bundle", NOTEBOOK, "--pages", "3", "--dpi", "72", "--layer", "native"]
    if failed_output is not None:
        failed_arguments.extend(["--output", str(failed_output)])
    failed = cli.call(
        failed_arguments,
        snapshot=snapshot,
        error="native_data_unavailable",
        pages=[3],
    )
    details = failed["error"]["details"]
    assert details["page"] == 3
    native = details["native_annotations"]
    assert native["coverage"] == "partial"
    assert native["full_fidelity_verified"] is False
    assert native["native_objects"] == 1
    assert native["rendered_objects"] == 0
    assert native["unsupported_objects"] == 1
    assert native["unsupported_reasons"] == {reason: 1}

    if output_mode == "artifact-cache":
        _assert_no_cached_artifacts(cli)
        output = None
    else:
        assert failed_output is not None and not failed_output.exists()
        assert not list(persistent_parent.iterdir())
        output = persistent_parent / f"partial-{variant}-{pages[0]}"

    partial = cli.bundle(pages, snapshot, output=output)
    assert partial["status"] == "partial"
    assert partial["pages"] == pages
    assert partial["failed_pages"] == [3]
    assert partial["layer"] == "native"
    assert partial["text_source"] == "pdf"
    expected_warning = (
        "Native rendering failed for page 3; source-labelled text evidence remains available, "
        "but no image was produced."
    )
    assert expected_warning in partial["warnings"]
    evidence = partial["page_evidence"]
    assert [row["page"] for row in evidence] == pages
    failed_row = evidence[failed_index]
    assert failed_row["page"] == 3
    assert failed_row["image"] is None
    assert failed_row["render_error"]["code"] == "native_data_unavailable"
    assert failed_row["render_error"]["message"]
    assert failed_row["render_error"]["details"]["page"] == 3
    failed_native = failed_row["render_error"]["details"]["native_annotations"]
    assert failed_native["unsupported_reasons"] == {reason: 1}
    assert failed_row["parts"]
    assert all(part["source"] == "pdf" for part in failed_row["parts"])
    assert failed_row["warnings"]
    assert (
        "Native image rendering failed for page 3; no image is available." in failed_row["warnings"]
    )
    assert "render" not in failed_row
    assert (
        not {
            "image_source",
            "annotation_status",
            "includes_handwriting",
            "native_annotations",
        }
        & failed_row.keys()
    )

    successful_rows = [row for index, row in enumerate(evidence) if index != failed_index]
    assert [row["page"] for row in successful_rows] == [page for page in pages if page != 3]
    for row in successful_rows:
        assert row["image"]
        assert row["parts"]
        assert row["warnings"]
        assert row["annotation_status"] == "native-reconstructed-approximate"
        assert row["native_annotations"]["unsupported_objects"] == 0
        assert Path(row["image"]).is_file()
        if row["page"] == 1:
            assert _pixel(row, 60, 30) == (255, 0, 0)
        else:
            assert _pixel(row, 60, 90) == (0, 0, 255)

    if output_mode == "artifact-cache":
        first_images = {row["page"]: row["image"] for row in successful_rows}
        replay = cli.bundle(pages, snapshot)
        assert replay["failed_pages"] == [3]
        replay_failed = replay["page_evidence"][failed_index]
        assert replay_failed["image"] is None
        assert replay_failed["render_error"] == failed_row["render_error"]
        assert {
            row["page"]: row["image"]
            for index, row in enumerate(replay["page_evidence"])
            if index != failed_index
        } == first_images
        verbose = cli.bundle(pages, snapshot, verbose=True)
    else:
        assert output is not None and output.is_dir()
        manifest = json.loads((output / "text.json").read_text())
        assert manifest["status"] == "partial"
        assert manifest["failed_pages"] == [3]
        assert expected_warning in manifest["warnings"]
        assert [row["page"] for row in manifest["pages"]] == pages
        manifest_failed = manifest["pages"][failed_index]
        assert manifest_failed["image"] is None
        assert manifest_failed["render_error"] == failed_row["render_error"]
        assert manifest_failed["text"]["parts"] == failed_row["parts"]
        assert (
            "Native image rendering failed for page 3; no image is available."
            in manifest_failed["warnings"]
        )
        assert "render" not in manifest_failed
        assert sorted(path.name for path in output.glob("page-*.png")) == [
            f"page-{page:04d}.png" for page in sorted(set(pages) - {3})
        ]
        assert not list(output.parent.glob(".goodnotes-bundle-*"))
        verbose = cli.bundle(pages, snapshot, verbose=True)

    assert verbose["status"] == "partial"
    assert verbose["failed_pages"] == [3]
    assert [row["page"] for row in verbose["page_evidence"]] == pages
    verbose_failed = verbose["page_evidence"][failed_index]
    assert verbose_failed["image"] is None
    assert verbose_failed["render_error"] == failed_row["render_error"]
    assert verbose_failed["text"]["parts"] == failed_row["parts"]
    for row in successful_rows:
        verbose_row = verbose["page_evidence"][pages.index(row["page"])]
        assert verbose_row["image"]
        assert verbose_row["render"]["native_annotations"]["unsupported_objects"] == 0
        assert verbose_row["text"]["parts"] == row["parts"]

    healthy = cli.bundle([2], snapshot)
    assert healthy["pages"] == [2]
    assert healthy["page_evidence"][0]["image"]
    assert _pixel(healthy["page_evidence"][0], 60, 90) == (0, 0, 255)

    _assert_preserved(cli, source, captured, before)


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
