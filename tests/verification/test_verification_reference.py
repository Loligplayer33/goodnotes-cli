"""Independent geometry oracles and privacy boundaries for development verification."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.support.paths import PROJECT_ROOT  # noqa: E402

sys.path.insert(0, str(PROJECT_ROOT))

from goodnotes_cli.documents import render_page
from goodnotes_cli.library import Library
from goodnotes_cli.safety import Sandbox
from goodnotes_cli.snapshot import create_snapshot
from scripts.verification import reference as reference_checks
from tests.support.native_store_fixture import NOTEBOOK, GeneratedNativeStore
from tests.support.native_store_fixture import generated_source as generated_source
from tests.support.native_store_fixture import native_runtime as native_runtime


def _region(box: list[float] | None = None) -> dict[str, Any]:
    return {
        "box": box or [0, 0, 1, 1],
        "max_mean_error": 0.015,
        "max_changed_fraction": 0.05,
        "min_foreground_retention": 0.92,
    }


def _reference(path: Path, *, variant: str = "correct", page_number: int = 1) -> None:
    """Hand-draw fixture geometry, without decoding records or production helpers."""
    with fitz.open() as document:
        page = document.new_page(width=240 if variant != "dimensions" else 200, height=180)
        if variant != "blank":
            page.insert_text((10, 160), f"Generated background {page_number}")
        if variant not in {"blank", "missing"}:
            y = 30 if page_number == 1 else 90
            if variant == "shifted":
                y += 10
            color = (1, 0, 0) if page_number == 1 else (0, 0, 1)
            page.draw_line((30, y), (90, y), color=color, width=36 / 11, lineCap=1)
        document.save(path)


def _png(pdf: Path, png: Path, *, dpi: int = 144) -> None:
    with fitz.open(pdf) as document:
        document[0].get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False).save(png)


def _compare(actual: Path, expected: Path) -> dict[str, Any]:
    return reference_checks.compare_reference(
        actual, expected, regions=[_region([0.10, 0.13, 0.41, 0.20])], dpi=144
    )


def test_generated_native_output_matches_independently_drawn_reference(
    generated_source: tuple[Path, GeneratedNativeStore], tmp_path: Path
) -> None:
    source, _ = generated_source
    root = create_snapshot(source, tmp_path / "snapshot")
    image = tmp_path / "actual.png"
    executable = os.environ.get("GOODNOTES_VERIFY_CLI")
    if executable:
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment["GOODNOTES_DATA_DIR"] = str(tmp_path / "data")
        environment["GOODNOTES_SCRATCH_DIR"] = str(tmp_path / "scratch")
        (tmp_path / "scratch").mkdir()
        completed = subprocess.run(
            [
                str(Path(executable).resolve()),
                "--verbose",
                "--sandbox",
                str(root),
                "render",
                NOTEBOOK,
                "--page",
                "1",
                "--layer",
                "native",
                "--dpi",
                "144",
                "--output",
                str(image),
            ],
            cwd=tmp_path,
            env=environment,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        result = json.loads(completed.stdout)
    else:
        with Library(Sandbox.load(root)) as library:
            result = render_page(
                library.sandbox, library.pages(NOTEBOOK)[0], image, layer="native", dpi=144
            )
    native = result["native_annotations"]
    assert isinstance(native, dict)
    assert native["full_fidelity_verified"] is False
    expected = tmp_path / "expected.pdf"
    _reference(expected)
    comparison = _compare(image, expected)
    assert comparison["status"] == "pass", comparison
    assert comparison["checks"] == 2


@pytest.mark.parametrize("variant", ["missing", "shifted", "wrong_page", "blank"])
def test_same_rule_rejects_missing_shifted_wrong_page_and_blank(
    tmp_path: Path, variant: str
) -> None:
    expected, actual = tmp_path / "reference.pdf", tmp_path / "actual.pdf"
    _reference(expected)
    _reference(actual, variant=variant, page_number=2 if variant == "wrong_page" else 1)
    assert _compare(actual, expected)["status"] == "fail"


def test_png_and_pdf_and_page_selection_have_the_same_rule(tmp_path: Path) -> None:
    expected, png = tmp_path / "reference.pdf", tmp_path / "actual.png"
    _reference(expected)
    _png(expected, png)
    assert _compare(png, expected)["status"] == "pass"
    multi = tmp_path / "multi.pdf"
    other = tmp_path / "other.pdf"
    _reference(other, page_number=2)
    with fitz.open() as document, fitz.open(other) as first, fitz.open(expected) as second:
        document.insert_pdf(first)
        document.insert_pdf(second)
        document.save(multi)
    assert (
        reference_checks.compare_reference(png, multi, regions=[_region()], reference_page=2)[
            "status"
        ]
        == "pass"
    )
    assert (
        reference_checks.compare_reference(png, multi, regions=[_region()], reference_page=1)[
            "status"
        ]
        == "fail"
    )


def test_dimension_mismatch_is_not_resized_and_empty_reference_region_fails(tmp_path: Path) -> None:
    expected, actual = tmp_path / "reference.pdf", tmp_path / "actual.pdf"
    _reference(expected)
    _reference(actual, variant="dimensions")
    assert _compare(actual, expected) == {"status": "fail", "reason": "dimensions", "checks": 0}
    result = reference_checks.compare_reference(
        expected, expected, regions=[_region([0.5, 0.5, 0.9, 0.7])]
    )
    assert result["status"] == "fail"
    assert result["regions"][1]["reference_foreground_pixels"] == 0


def test_blank_cannot_pass_even_with_permissive_tolerances(tmp_path: Path) -> None:
    expected, actual = tmp_path / "reference.pdf", tmp_path / "blank.pdf"
    _reference(expected)
    _reference(actual, variant="blank")
    result = reference_checks.compare_reference(
        actual,
        expected,
        pixel_tolerance=1,
        regions=[
            {
                "box": [0, 0, 1, 1],
                "max_mean_error": 1,
                "max_changed_fraction": 1,
                "min_foreground_retention": 0,
            }
        ],
    )
    assert result["status"] == "fail"


def test_clean_background_excludes_dense_printing_with_per_mark_regions(
    tmp_path: Path,
) -> None:
    clean = tmp_path / "clean.pdf"
    expected = tmp_path / "expected.pdf"
    missing = tmp_path / "missing.pdf"
    for path, marks in ((clean, 0), (expected, 2), (missing, 1)):
        with fitz.open() as document:
            page = document.new_page(width=240, height=180)
            for x in range(10, 230, 8):
                page.draw_line((x, 8), (x, 170), color=(0, 0, 0), width=0.5)
            if marks:
                page.draw_line((35, 35), (180, 35), color=(1, 0, 0), width=4)
            if marks == 2:
                page.draw_line((45, 75), (52, 75), color=(1, 0, 0), width=4)
            document.save(path)
    large_mark = {
        "box": [0.12, 0.16, 0.78, 0.23],
        "max_mean_error": 1,
        "max_changed_fraction": 1,
        "min_foreground_retention": 0.8,
    }
    small_mark = {**large_mark, "box": [0.17, 0.39, 0.24, 0.45]}
    matched = reference_checks.compare_reference(
        expected, expected, regions=[large_mark, small_mark], background=clean
    )
    assert matched["status"] == "pass"
    assert (
        matched["regions"][1]["reference_foreground_pixels"]
        < matched["regions"][0]["reference_foreground_pixels"]
    )
    omitted = reference_checks.compare_reference(
        missing, expected, regions=[large_mark, small_mark], background=clean
    )
    assert omitted["regions"][0]["status"] == "pass"
    assert omitted["regions"][1]["status"] == "pass"
    assert omitted["regions"][2]["status"] == "fail"
    assert omitted["status"] == "fail"


def test_clean_background_dimensions_must_match(tmp_path: Path) -> None:
    expected, clean = tmp_path / "reference.pdf", tmp_path / "clean.pdf"
    _reference(expected)
    _reference(clean, variant="dimensions")
    assert reference_checks.compare_reference(
        expected, expected, regions=[_region()], background=clean
    ) == {"status": "fail", "reason": "dimensions", "checks": 0}


@pytest.mark.parametrize("bad", [-0.1, 1.1, float("nan"), float("inf"), True, "0.1"])
def test_tolerances_are_literal_finite_fractions(tmp_path: Path, bad: Any) -> None:
    with pytest.raises(reference_checks.ReferenceInputError):
        reference_checks.compare_reference(
            tmp_path / "unused", tmp_path / "unused", regions=[_region()], pixel_tolerance=bad
        )


@pytest.mark.parametrize("regions", [[], [{"box": [0, 0, 0, 1]}], [_region([0, 0, 2, 1])]])
def test_invalid_regions_rejected_before_reading_images(tmp_path: Path, regions: Any) -> None:
    with pytest.raises(reference_checks.ReferenceInputError):
        reference_checks.compare_reference(
            tmp_path / "unused", tmp_path / "unused", regions=regions
        )


def _private_manifest(tmp_path: Path) -> tuple[Path, dict[str, Any]]:
    sandbox = tmp_path / "copied-sandbox"
    sandbox.mkdir()
    (sandbox / "goodnotes-snapshot.json").write_text('{"format_version":1}')
    _reference(tmp_path / "reference.pdf")
    case = {
        "sandbox": "copied-sandbox",
        "notebook": "generated-private-selector",
        "page": 1,
        "reference": "reference.pdf",
        "reference_page": 1,
        "reference_provenance": "Goodnotes-export",
        "pixel_tolerance": 0.1,
        "regions": [_region([0.1, 0.13, 0.41, 0.20])],
    }
    data = {"version": 1, "cases": [case]}
    manifest = tmp_path / "private-manifest.json"
    manifest.write_text(json.dumps(data))
    return manifest, data


def test_private_failures_redact_subprocess_content_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    roots = []

    def fail(arguments: list[str], **kwargs: Any) -> Any:
        roots.append(kwargs["cwd"])
        assert "--sandbox" in arguments and "sync" not in arguments
        assert kwargs["env"]["GOODNOTES_DATA_DIR"].startswith(str(kwargs["cwd"]))
        assert kwargs["env"]["GOODNOTES_SCRATCH_DIR"].startswith(str(kwargs["cwd"]))
        assert "PYTHONPATH" not in kwargs["env"]

        class Failure:
            returncode = 1
            stderr = b'{"error":{"message":"PRIVATE_PATH PRIVATE_NAME PRIVATE_NOTE"}}'
            stdout = b"PRIVATE_ID PRIVATE_EMAIL"

        return Failure()

    monkeypatch.setattr(reference_checks.subprocess, "run", fail)
    result = reference_checks.check_private_corpus(manifest, tmp_path / "cli")
    fingerprint = result.pop("corpus_fingerprint")
    assert len(fingerprint) == 64
    assert result == {
        "status": "fail",
        "cases": 1,
        "passed": 0,
        "failed": 1,
        "blocked": 0,
        "comparisons": 0,
    }
    assert "PRIVATE" not in json.dumps(result)
    assert str(tmp_path) not in json.dumps(result)
    assert roots and all(not root.exists() for root in roots)


def test_private_cli_environment_isolated_with_selected_rocksdb_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    for key, value in {
        "GOODNOTES_SANDBOX": "/inherited/sandbox",
        "GOODNOTES_DATA_DIR": "/inherited/data",
        "GOODNOTES_LIBRARY_ROOT": "/inherited/library",
        "GOODNOTES_SCRATCH_DIR": "/inherited/scratch",
        "GOODNOTES_VERIFY_CLI": "/inherited/cli",
        "GOODNOTES_VERIFY_METRICS": "/inherited/metrics",
        "GOODNOTES_ROCKSDB_LIBRARY": "/selected/rocksdb",
        "PYTHONPATH": "/inherited/pythonpath",
        "PYTHONHOME": "/inherited/pythonhome",
        "PYTHONUSERBASE": "/inherited/userbase",
    }.items():
        monkeypatch.setenv(key, value)
    seen: dict[str, str] = {}

    def blocked(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
        seen.update(environment)
        return "blocked"

    monkeypatch.setattr(reference_checks, "_run_cli", blocked)

    result = reference_checks.check_private_corpus(manifest, tmp_path / "cli")

    assert result["status"] == "blocked"
    assert seen["GOODNOTES_ROCKSDB_LIBRARY"] == "/selected/rocksdb"
    assert seen["GOODNOTES_DATA_DIR"].endswith("/data")
    assert seen["GOODNOTES_SCRATCH_DIR"] == seen["TMPDIR"] == seen["TEMP"] == seen["TMP"]
    assert not any(key.startswith("PYTHON") for key in seen)
    assert not any(
        key.startswith("GOODNOTES_")
        for key in seen
        if key
        not in {
            "GOODNOTES_ROCKSDB_LIBRARY",
            "GOODNOTES_DATA_DIR",
            "GOODNOTES_SCRATCH_DIR",
        }
    )


def test_private_render_and_bundle_compare_both_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    clean = tmp_path / "clean.pdf"
    _reference(clean, variant="missing")
    seen = []

    def succeed(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
        seen.append((arguments, cwd))
        if "--output" in arguments:
            destination = Path(arguments[arguments.index("--output") + 1])
            if "bundle" in arguments:
                destination.mkdir()
                destination /= "page-0001.png"
            image_source = clean if "background" in arguments else tmp_path / "reference.pdf"
            _png(image_source, destination)
        return "pass"

    monkeypatch.setattr(reference_checks, "_run_cli", succeed)
    result = reference_checks.check_private_corpus(manifest, tmp_path / "cli")
    fingerprint = result.pop("corpus_fingerprint")
    assert len(fingerprint) == 64
    assert result == {
        "status": "pass",
        "cases": 1,
        "passed": 1,
        "failed": 0,
        "blocked": 0,
        "comparisons": 2,
    }
    assert len(seen) == 4
    assert all(not cwd.exists() for _, cwd in seen)


@pytest.mark.parametrize("location", ["repository", "snapshot", "source"])
@pytest.mark.parametrize("variable", ["TMPDIR", "TEMP", "TMP"])
def test_private_temp_root_rejected_before_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, location: str, variable: str
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    if location == "repository":
        root = tmp_path / "checkout"
        root.mkdir()
        monkeypatch.setattr(reference_checks, "REPOSITORY", root)
    elif location == "snapshot":
        root = tmp_path / "copied-sandbox"
    else:
        root = tmp_path / "source-Library"
        (root / "Databases").mkdir(parents=True)
    candidate = root / "scratch"
    candidate.mkdir()
    for name in ("TMPDIR", "TEMP", "TMP"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(variable, str(candidate))
    monkeypatch.setattr(
        reference_checks.tempfile,
        "TemporaryDirectory",
        lambda **kwargs: pytest.fail("temporary directory created"),
    )
    monkeypatch.setattr(reference_checks, "_run_cli", lambda *args: pytest.fail("CLI was invoked"))
    report = reference_checks.check_private_corpus(manifest, tmp_path / "cli")
    assert report["status"] == "blocked" and report["comparisons"] == 0
    assert "scratch" not in json.dumps(report)


@pytest.mark.parametrize("change", ["version", "provenance", "page", "regions", "unmarked"])
def test_private_invalid_input_blocks_before_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    manifest, data = _private_manifest(tmp_path)
    if change == "version":
        data["version"] = 2
    elif change == "provenance":
        data["cases"][0]["reference_provenance"] = "generated-with-production-renderer"
    elif change == "unmarked":
        (tmp_path / "copied-sandbox" / "goodnotes-snapshot.json").unlink()
    else:
        data["cases"][0][change] = 0 if change == "page" else []
    manifest.write_text(json.dumps(data))
    monkeypatch.setattr(reference_checks, "_run_cli", lambda *args: pytest.fail("CLI was invoked"))
    assert reference_checks.check_private_corpus(manifest, tmp_path / "cli")["status"] == "blocked"


def test_private_manifest_cannot_be_inside_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    monkeypatch.setattr(reference_checks, "REPOSITORY", tmp_path)
    result = reference_checks.check_private_corpus(manifest, tmp_path / "cli")
    assert result["status"] == "blocked" and result["cases"] == 0


@pytest.mark.parametrize("changed", ["manifest", "reference", "marker"])
def test_private_corpus_changes_invalidate_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, changed: str
) -> None:
    manifest, _ = _private_manifest(tmp_path)
    original_cases, _ = reference_checks._read_cases(manifest)
    before = reference_checks._corpus_fingerprint(manifest, original_cases)

    def alter(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
        if changed == "manifest":
            manifest.write_bytes(manifest.read_bytes() + b" ")
        elif changed == "reference":
            (tmp_path / "reference.pdf").write_bytes(b"changed generated reference")
        else:
            (tmp_path / "copied-sandbox" / "goodnotes-snapshot.json").write_text(
                '{"format_version":1,"generated_revision":2}'
            )
        return "fail"

    monkeypatch.setattr(reference_checks, "_run_cli", alter)
    result = reference_checks.check_private_corpus(manifest, tmp_path / "cli")
    assert result["status"] == "fail"
    assert result["reason"] == "corpus_changed"
    assert result["corpus_fingerprint"] == before
    assert result["passed"] == 0 and result["failed"] == 1


def test_large_png_header_rejected_before_allocation(tmp_path: Path) -> None:
    path = tmp_path / "large.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR" + (100_000).to_bytes(4, "big") * 2)
    with pytest.raises(reference_checks.ReferenceInputError, match="pixel_limit"):
        reference_checks.compare_reference(path, path, regions=[_region()])
