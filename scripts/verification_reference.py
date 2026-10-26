"""Development-only, in-place image comparison and explicit private acceptance checks.

Expected pages must be independent references. This module never creates a reference
through the production renderer. A generated PDF drawn with PyMuPDF tests geometry,
not compatibility with Goodnotes. Private Goodnotes exports provide a separate oracle.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pymupdf as fitz

REPOSITORY = Path(__file__).resolve().parents[1]
if __package__ in {None, ""}:
    sys.path.insert(0, str(REPOSITORY))
from goodnotes_cli.safety import path_contains  # noqa: E402
from scripts.verification_paths import temporary_parent  # noqa: E402

MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_PIXELS = 8_000_000
MAX_PAGES = 1_000
MAX_CASES = 20
MAX_REGIONS = 32
_TOLERANCES = ("max_mean_error", "max_changed_fraction", "min_foreground_retention")


class ReferenceInputError(ValueError):
    """A fixed, non-private error category suitable for an aggregate receipt."""


def _fraction(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ReferenceInputError("invalid_tolerance")
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ReferenceInputError("invalid_tolerance")
    return float(value)


def _integer(value: object, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ReferenceInputError("invalid_number")
    return value


def _regions(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_REGIONS:
        raise ReferenceInputError("invalid_regions")
    result = []
    for region in value:
        if not isinstance(region, dict) or set(region) != {"box", *_TOLERANCES}:
            raise ReferenceInputError("invalid_regions")
        box = region["box"]
        if not isinstance(box, list) or len(box) != 4:
            raise ReferenceInputError("invalid_regions")
        coordinates = [_fraction(number) for number in box]
        if coordinates[0] >= coordinates[2] or coordinates[1] >= coordinates[3]:
            raise ReferenceInputError("invalid_regions")
        result.append({"box": coordinates, **{key: _fraction(region[key]) for key in _TOLERANCES}})
    return result


def _regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ReferenceInputError("invalid_file")


def _raster(path: Path, page_number: int, dpi: int) -> fitz.Pixmap:
    _regular_file(path)
    if path.suffix.lower() == ".pdf":
        with fitz.open(path) as document:
            if not 1 <= len(document) <= MAX_PAGES or page_number > len(document):
                raise ReferenceInputError("invalid_page")
            page = document[page_number - 1]
            width = math.ceil(page.rect.width * dpi / 72)
            height = math.ceil(page.rect.height * dpi / 72)
            if width * height > MAX_PIXELS:
                raise ReferenceInputError("pixel_limit")
            return page.get_pixmap(dpi=dpi, colorspace=fitz.csRGB, alpha=False)
    if path.suffix.lower() != ".png" or page_number != 1:
        raise ReferenceInputError("invalid_image")
    # Inspect the PNG header before decoding to bound a compressed raster's allocation.
    with path.open("rb") as stream:
        header = stream.read(24)
    if header[:16] != b"\x89PNG\r\n\x1a\n\0\0\0\rIHDR":
        raise ReferenceInputError("invalid_image")
    width, height = int.from_bytes(header[16:20], "big"), int.from_bytes(header[20:24], "big")
    if width < 1 or height < 1 or width * height > MAX_PIXELS:
        raise ReferenceInputError("pixel_limit")
    pixmap = fitz.Pixmap(path)
    if pixmap.alpha:
        raise ReferenceInputError("transparent_image")
    return fitz.Pixmap(fitz.csRGB, pixmap) if pixmap.colorspace != fitz.csRGB else pixmap


def _measure(
    actual: fitz.Pixmap,
    reference: fitz.Pixmap,
    region: dict[str, Any],
    tolerance: float,
    background: fitz.Pixmap | None = None,
) -> dict[str, Any]:
    width, height = reference.width, reference.height
    x0, y0, x1, y1 = region["box"]
    left, top = math.floor(x0 * width), math.floor(y0 * height)
    right, bottom = math.ceil(x1 * width), math.ceil(y1 * height)
    actual_bytes, reference_bytes = actual.samples, reference.samples
    background_bytes = background.samples if background is not None else None
    count = (right - left) * (bottom - top)
    total_error = changed = foreground = retained = 0
    threshold = tolerance * 255
    for y in range(top, bottom):
        for x in range(left, right):
            offset = (y * width + x) * 3
            expected = reference_bytes[offset : offset + 3]
            observed = actual_bytes[offset : offset + 3]
            differences = [abs(a - b) for a, b in zip(expected, observed, strict=True)]
            total_error += sum(differences)
            differs = max(differences) > threshold
            changed += differs
            # A clean page separates required annotations from printed content.
            # Without one, retain the legacy white-background foreground rule.
            if background_bytes is None:
                required = min(expected) < 242
            else:
                clean = background_bytes[offset : offset + 3]
                required = max(abs(a - b) for a, b in zip(expected, clean, strict=True)) > threshold
            if required:
                foreground += 1
                is_retained = not differs and (background_bytes is not None or min(observed) < 242)
                retained += is_retained
    mean_error = total_error / (count * 3 * 255)
    changed_fraction = changed / count
    retention = retained / foreground if foreground else 0.0
    passed = (
        foreground > 0
        and mean_error <= region["max_mean_error"]
        and changed_fraction <= region["max_changed_fraction"]
        and retention >= region["min_foreground_retention"]
        and retained > 0
    )
    return {
        "status": "pass" if passed else "fail",
        "mean_error": mean_error,
        "changed_fraction": changed_fraction,
        "foreground_retention": retention,
        "reference_foreground_pixels": foreground,
    }


def compare_reference(
    actual: Path,
    reference: Path,
    *,
    regions: list[dict[str, Any]],
    dpi: int = 144,
    actual_page: int = 1,
    reference_page: int = 1,
    pixel_tolerance: float = 0.1,
    background: Path | None = None,
    background_page: int = 1,
) -> dict[str, Any]:
    """Compare full pages and declared regions without alignment or resampling.

    Full-page checks use the strictest declared regional tolerances and all page
    pixels. With an optional clean background, regional foreground is reference
    content absent from that background. Declare one region per required object.
    Pixel tolerance is the maximum per-channel RGB difference / 255.
    PNG dimensions must already match PDF raster DPI.
    Input errors raise ReferenceInputError; mismatches return status=fail.
    """
    checks = _regions(regions)
    dpi = _integer(dpi, 72, 300)
    actual_page = _integer(actual_page, 1, MAX_PAGES)
    reference_page = _integer(reference_page, 1, MAX_PAGES)
    background_page = _integer(background_page, 1, MAX_PAGES)
    tolerance = _fraction(pixel_tolerance)
    expected = _raster(reference, reference_page, dpi)
    observed = _raster(actual, actual_page, dpi)
    clean = _raster(background, background_page, dpi) if background is not None else None
    if (expected.width, expected.height) != (observed.width, observed.height) or (
        clean is not None and (expected.width, expected.height) != (clean.width, clean.height)
    ):
        return {"status": "fail", "reason": "dimensions", "checks": 0}
    whole = {
        "box": [0, 0, 1, 1],
        "max_mean_error": min(region["max_mean_error"] for region in checks),
        "max_changed_fraction": min(region["max_changed_fraction"] for region in checks),
        "min_foreground_retention": max(region["min_foreground_retention"] for region in checks),
    }
    results = [_measure(observed, expected, whole, tolerance)]
    results.extend(_measure(observed, expected, region, tolerance, clean) for region in checks)
    return {
        "status": "pass" if all(result["status"] == "pass" for result in results) else "fail",
        "checks": len(results),
        "regions": results,
    }


def _outside_repository(path: Path) -> None:
    try:
        inside_repository = path_contains(REPOSITORY, path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ReferenceInputError("repository_input") from exc
    if inside_repository:
        raise ReferenceInputError("repository_input")


def _manifest_path(base: Path, value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ReferenceInputError("invalid_path")
    path = Path(value).expanduser()
    path = path if path.is_absolute() else base / path
    _outside_repository(path)
    return path


def _read_cases(manifest: Path) -> tuple[list[dict[str, Any]], bytes]:
    _outside_repository(manifest)
    _regular_file(manifest)
    if manifest.stat().st_size > 256_000:
        raise ReferenceInputError("manifest_limit")
    manifest_bytes = manifest.read_bytes()
    data = json.loads(manifest_bytes)
    if (
        not isinstance(data, dict)
        or set(data) != {"version", "cases"}
        or type(data["version"]) is not int
        or data["version"] != 1
    ):
        raise ReferenceInputError("invalid_manifest")
    cases = data["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= MAX_CASES:
        raise ReferenceInputError("invalid_cases")
    result = []
    required = {
        "sandbox",
        "notebook",
        "page",
        "reference",
        "reference_page",
        "reference_provenance",
        "regions",
        "pixel_tolerance",
    }
    for case in cases:
        if not isinstance(case, dict) or not required <= set(case) <= required | {"dpi"}:
            raise ReferenceInputError("invalid_case")
        if case["reference_provenance"] != "Goodnotes-export":
            raise ReferenceInputError("invalid_provenance")
        if not isinstance(case["notebook"], str) or not case["notebook"]:
            raise ReferenceInputError("invalid_selector")
        sandbox = _manifest_path(manifest.parent, case["sandbox"])
        # The CLI performs authoritative sandbox validation before any rendering.
        # This precheck only requires the explicitly supplied, pre-existing copy.
        if sandbox.is_symlink() or not sandbox.is_dir():
            raise ReferenceInputError("invalid_sandbox")
        _regular_file(sandbox / "goodnotes-snapshot.json")
        reference = _manifest_path(manifest.parent, case["reference"])
        _regular_file(reference)
        if reference.suffix.lower() != ".pdf":
            raise ReferenceInputError("invalid_reference")
        result.append(
            {
                **case,
                "sandbox": sandbox,
                "reference": reference,
                "page": _integer(case["page"], 1, MAX_PAGES),
                "reference_page": _integer(case["reference_page"], 1, MAX_PAGES),
                "dpi": _integer(case.get("dpi", 144), 72, 300),
                "regions": _regions(case["regions"]),
                "pixel_tolerance": _fraction(case["pixel_tolerance"]),
            }
        )
    return result, manifest_bytes


def _corpus_fingerprint(manifest: Path, cases: list[dict[str, Any]]) -> str:
    """Hash content with length framing; never expose filenames or file contents."""
    digest = hashlib.sha256(b"goodnotes-private-corpus-v1\0")
    paths = [manifest]
    for case in cases:
        paths.extend([case["reference"], case["sandbox"] / "goodnotes-snapshot.json"])
    for path in paths:
        _regular_file(path)
        size = path.stat().st_size
        digest.update(size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            remaining = size
            while chunk := stream.read(min(1_048_576, remaining + 1)):
                remaining -= len(chunk)
                if remaining < 0:
                    raise ReferenceInputError("corpus_changed")
                digest.update(chunk)
        if remaining != 0:
            raise ReferenceInputError("corpus_changed")
    return digest.hexdigest()


def _run_cli(arguments: list[str], environment: dict[str, str], cwd: Path) -> str:
    try:
        process = subprocess.run(
            arguments, cwd=cwd, env=environment, capture_output=True, timeout=120, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return "blocked"
    if process.returncode == 0:
        return "pass"
    # Examine stable error codes only. Never return subprocess content.
    try:
        error = json.loads(process.stderr).get("error", {})
        if error.get("code") in {
            "native_runtime_unavailable",
            "cache_permission_denied",
            "source_permission_denied",
            "output_permission_denied",
        }:
            return "blocked"
    except (ValueError, AttributeError):
        pass
    return "fail"


def check_private_corpus(manifest: Path, cli: Path) -> dict[str, Any]:
    """Run only an explicit outside-repository corpus; return aggregate counts only.

    Manifest version 1 has cases with sandbox, notebook, page, reference,
    reference_page, declared reference_provenance="Goodnotes-export", regions,
    pixel_tolerance, and optional dpi=144. Paths may be relative to the manifest.
    No discovery, capture, registration, source-engine access or Goodnotes control
    occurs here. Existing copies/references remain read-only. Temporary CLI data,
    native scratch and outputs are isolated and removed after each run. A corpus
    fingerprint identifies manifest, reference and snapshot-marker bytes, not the
    producer of the references. Changes during proof invalidate the whole result.
    """
    report: dict[str, Any] = {
        "status": "blocked",
        "cases": 0,
        "passed": 0,
        "failed": 0,
        "blocked": 0,
        "comparisons": 0,
    }
    try:
        cases, manifest_bytes = _read_cases(manifest)
        report["corpus_fingerprint"] = _corpus_fingerprint(manifest, cases)
        if manifest.read_bytes() != manifest_bytes:
            raise ReferenceInputError("corpus_changed")
        temporary_directory_parent = temporary_parent(
            REPOSITORY, protected_roots=[case["sandbox"] for case in cases]
        )
    except (OSError, ValueError, TypeError):
        report["blocked"] = 1
        return report
    report["cases"] = len(cases)
    for case in cases:
        try:
            with tempfile.TemporaryDirectory(
                prefix="goodnotes-private-verification-", dir=temporary_directory_parent
            ) as directory:
                root = Path(directory)
                scratch = root / "scratch"
                scratch.mkdir()
                environment = os.environ.copy()
                environment.pop("PYTHONPATH", None)
                environment.pop("GOODNOTES_SANDBOX", None)
                environment["GOODNOTES_DATA_DIR"] = str(root / "data")
                environment["GOODNOTES_SCRATCH_DIR"] = str(scratch)
                environment["TMPDIR"] = str(scratch)
                command = [
                    str(cli.resolve()),
                    "--sandbox",
                    str(case["sandbox"].resolve()),
                    "--data-dir",
                    str(root / "data"),
                ]
                status = _run_cli([*command, "info", case["notebook"]], environment, root)
                if status == "pass":
                    background = root / "background.png"
                    status = _run_cli(
                        [
                            *command,
                            "render",
                            case["notebook"],
                            "--page",
                            str(case["page"]),
                            "--layer",
                            "background",
                            "--dpi",
                            str(case["dpi"]),
                            "--output",
                            str(background),
                        ],
                        environment,
                        root,
                    )
                if status == "pass":
                    for kind in ("render", "bundle"):
                        destination = root / ("render.png" if kind == "render" else "bundle")
                        selection = "--page" if kind == "render" else "--pages"
                        status = _run_cli(
                            [
                                *command,
                                kind,
                                case["notebook"],
                                selection,
                                str(case["page"]),
                                "--layer",
                                "native",
                                "--dpi",
                                str(case["dpi"]),
                                "--output",
                                str(destination),
                            ],
                            environment,
                            root,
                        )
                        if status != "pass":
                            break
                        image = (
                            destination
                            if kind == "render"
                            else destination / f"page-{case['page']:04d}.png"
                        )
                        result = compare_reference(
                            image,
                            case["reference"],
                            dpi=case["dpi"],
                            reference_page=case["reference_page"],
                            regions=case["regions"],
                            pixel_tolerance=case["pixel_tolerance"],
                            background=background,
                        )
                        report["comparisons"] += 1
                        if result["status"] != "pass":
                            status = "fail"
                            break
                report[{"pass": "passed", "fail": "failed", "blocked": "blocked"}[status]] += 1
        except (OSError, ValueError, RuntimeError):
            report["blocked"] += 1
    try:
        stable = _corpus_fingerprint(manifest, cases) == report["corpus_fingerprint"]
    except (OSError, ValueError):
        stable = False
    if not stable:
        report.update(
            status="fail", passed=0, failed=len(cases), blocked=0, reason="corpus_changed"
        )
        return report
    report["status"] = "fail" if report["failed"] else "blocked" if report["blocked"] else "pass"
    return report
