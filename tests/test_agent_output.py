"""Generated-data checks for concise agent evidence and inline bundles."""

from __future__ import annotations

import json
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from helpers import invoke_in_process
from test_cli import _tree_manifest, successful
from test_cli import invoke as invoke_full
from test_library import _create_snapshot

from goodnotes_cli.config import Settings
from goodnotes_cli.output import Projection, emit, output_contract


def invoke(
    arguments: list[str], tmp_path: Path, *, sandbox: Path | None = None
) -> subprocess.CompletedProcess[str]:
    """Exercise the public concise default, unlike the full-contract legacy helper."""
    return invoke_full(arguments, tmp_path, sandbox=sandbox, verbose=False)


@pytest.fixture
def generated_copy(tmp_path: Path) -> Path:
    sandbox = _create_snapshot(tmp_path / "generated-copy")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE attachments SET path = 'safe.pdf' WHERE id = 'attachment-unsafe'")
        db.execute("UPDATE templates SET page_number = 1")
    return sandbox.root


def _emit_brief(
    capsys: pytest.CaptureFixture[str], command: str, value: dict[str, Any]
) -> dict[str, Any]:
    emit(value, Settings(brief=True, projection=Projection(command)))
    return json.loads(capsys.readouterr().out)


def _native_metadata() -> dict[str, Any]:
    return {
        "page": 1,
        "page_id": "synthetic-page",
        "coverage": "partial",
        "full_fidelity_verified": False,
        "page_mapping": "observed-mapping",
        "native_objects": 7,
        "rendered_objects": 3,
        "rendered_strokes": 4,
        "rendered_paths": 1,
        "rendered_images": 2,
        "erased_objects": 1,
        "deleted_objects": 1,
        "unsupported_objects": 2,
        "unsupported_reasons": {"unknown-schema": 2},
        "native_image_body_field_6_absent_records": 1,
        "categories": {"pen": 7},
        "descriptor_variants": {"synthetic": 3},
        "warnings": [
            "Approximate ink.",
            "Unverified format.",
            "Some decoded native image records omit body field 6; "
            "its effect on application appearance or visibility is unverified.",
        ],
    }


@pytest.mark.parametrize("command", ["sync", "watch"])
def test_brief_refresh_preserves_freshness_and_failures(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    value = {
        "status": "ready",
        "snapshot": "/synthetic/cache/generation",
        "snapshot_id": "generation",
        "checked_at": "synthetic-check-time",
        "captured_at": None,
        "source_changed_during_refresh": True,
        "warnings": ["Source changed."],
        "capture_stats": {"source_captured_bytes": 123},
        "event": "error",
        "iteration": 2,
        "error": {"code": "source_busy", "details": {"remedy": "retry"}},
        "retry_in_seconds": 4,
    }
    result = _emit_brief(capsys, command, value)
    assert result == {
        key: item for key, item in value.items() if key not in {"snapshot", "capture_stats"}
    }


@pytest.mark.parametrize("command", ["ls", "tree"])
def test_brief_list_preserves_identity_and_pagination(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    row = {
        "id": "synthetic-document",
        "name": "Notes",
        "kind": "notebook",
        "path": "Course/Notes",
        "page_count": 4,
        "parent_id": "synthetic-parent",
        "updated_at": 0,
    }
    value = {"items": [row], "next_offset": 5, "snapshot": "generation", "warnings": []}
    result = _emit_brief(capsys, command, value)
    assert result["items"] == [
        {key: item for key, item in row.items() if key not in {"parent_id", "updated_at"}}
    ]
    assert result["next_offset"] == 5
    assert result["snapshot"] == "generation"
    assert result["warnings"] == []


def test_brief_pages_preserves_recognition_unknowns(capsys: pytest.CaptureFixture[str]) -> None:
    recognition = {
        "source": "handwriting",
        "availability": "unavailable",
        "text_rows": None,
        "text_chars": None,
        "index_state": "unknown",
    }
    result = _emit_brief(
        capsys,
        "pages",
        {
            "pages": [
                {
                    "id": "synthetic-page",
                    "document_id": "synthetic-doc",
                    "number": 1,
                    "recognition": recognition,
                    "attachment_path": "/synthetic/background.pdf",
                    "annotation_count": 0,
                }
            ],
            "annotation_count_source": "projection.notes_items",
            "next_offset": None,
        },
    )
    assert result["document_id"] == "synthetic-doc"
    assert result["pages"] == [
        {"id": "synthetic-page", "number": 1, "annotation_count": 0, "recognition": recognition}
    ]
    assert result["annotation_count_source"] == "projection.notes_items"


def test_brief_read_keeps_raw_parts_and_one_document_identity(
    capsys: pytest.CaptureFixture[str],
) -> None:
    parts = [
        {
            "source": "handwriting",
            "text": "raw|candidate",
            "engine": None,
            "priority": "primary",
            "truncated": True,
        }
    ]
    pages = [
        {
            "document_id": "synthetic-doc",
            "page_id": f"synthetic-page-{number}",
            "page": number,
            "parts": parts,
            "warnings": ["Stored recognition is unverified."],
            "truncated": True,
        }
        for number in (2, 1)
    ]
    result = _emit_brief(
        capsys, "read", {"pages": pages, "truncated": True, "snapshot": "generation"}
    )
    assert result["document_id"] == "synthetic-doc"
    assert [page["page"] for page in result["pages"]] == [2, 1]
    assert all("document_id" not in page for page in result["pages"])
    assert all(page["parts"] == parts for page in result["pages"])
    assert all(page["warnings"] == pages[0]["warnings"] for page in result["pages"])
    assert result["truncated"] is True


def test_brief_render_preserves_partial_coverage_and_distinct_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    native = _native_metadata()
    result = _emit_brief(
        capsys,
        "render",
        {
            "path": "/synthetic/page.png",
            "document_id": "synthetic-doc",
            "page": 1,
            "source_page": None,
            "dpi": 150,
            "layer": "native",
            "source": "copied-native-records",
            "includes_handwriting": True,
            "annotation_status": "native-reconstructed-approximate",
            "captured_at": None,
            "snapshot": "generation",
            "warnings": ["Approximate ink."],
            "native_annotations": native,
        },
    )
    assert result["warnings"] == native["warnings"]
    assert result["captured_at"] is None
    assert result["source_page"] is None
    assert result["native_annotations"] == {
        key: value
        for key, value in native.items()
        if key not in {"warnings", "categories", "descriptor_variants"}
    }
    assert result["includes_handwriting"] is True
    assert result["native_annotations"]["rendered_strokes"] == 4
    assert result["native_annotations"]["rendered_paths"] == 1
    assert result["native_annotations"]["rendered_images"] == 2
    assert result["native_annotations"]["native_image_body_field_6_absent_records"] == 1
    assert "dpi" not in result


def test_brief_export_keeps_native_page_attribution(capsys: pytest.CaptureFixture[str]) -> None:
    native = _native_metadata()
    value = {"path": "/synthetic/export.pdf", "native_annotations": [native], "warnings": []}
    result = _emit_brief(capsys, "export", value)
    assert result["native_annotations"][0]["page_id"] == "synthetic-page"
    assert result["native_annotations"][0]["warnings"] == native["warnings"]
    assert result["warnings"] == native["warnings"]


def test_brief_unknown_profile_retains_all_data(capsys: pytest.CaptureFixture[str]) -> None:
    value = {"hits": [], "coverage_detail": {"completeness": "unknown"}, "warnings": ["No index."]}
    assert _emit_brief(capsys, "search", value) == value


@pytest.mark.parametrize("verbose", [False, True])
def test_fields_selects_full_contract_with_or_without_verbose(
    verbose: bool, generated_copy: Path, tmp_path: Path
) -> None:
    result = successful(
        invoke(
            (["--verbose"] if verbose else [])
            + ["--fields", "id,parent_id,updated_at", "info", "doc-hostile"],
            tmp_path,
            sandbox=generated_copy,
        )
    )
    assert set(result) == {"id", "parent_id", "updated_at", "snapshot"}
    assert result["id"] == "doc-hostile"


def test_fields_bypasses_internal_concise_profile(capsys: pytest.CaptureFixture[str]) -> None:
    emit(
        {"id": "synthetic", "parent_id": None},
        Settings(brief=True, fields="parent_id", projection=Projection("info")),
    )
    assert json.loads(capsys.readouterr().out) == {"parent_id": None}


@pytest.mark.parametrize("verbose", [False, True])
def test_bundle_returns_bounded_evidence_and_actual_images(
    verbose: bool,
    generated_copy: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "inline-bundle"
    result = successful(
        invoke(
            (["--verbose"] if verbose else [])
            + [
                "bundle",
                "doc-hostile",
                "--pages",
                "3,1",
                "--max-chars",
                "5",
                "--layer",
                "background",
                "--source",
                "all",
                "--output",
                str(output),
            ],
            tmp_path,
            sandbox=generated_copy,
        )
    )
    saved = json.loads((output / "text.json").read_text())
    assert result["pages"] == [3, 1]
    assert result["truncated"] is True
    assert [page["page"] for page in result["page_evidence"]] == [3, 1]
    assert all(
        Path(page["image"]).is_absolute() and Path(page["image"]).is_file()
        for page in result["page_evidence"]
    )
    assert all(not Path(page["image"]).is_absolute() for page in saved["pages"])
    assert all("render" in page and "text" in page for page in saved["pages"])
    assert result["snapshot"] == saved["snapshot"]
    assert result["captured_at"] == saved["captured_at"]
    if not verbose:
        assert result["document_id"] == "doc-hostile"
        for inline, full in zip(result["page_evidence"], saved["pages"], strict=True):
            assert inline["parts"] == full["text"]["parts"]
            assert inline["page_id"] == full["text"]["page_id"]
            assert inline["truncated"] == full["text"]["truncated"]
            assert inline["image_source"] == full["render"]["source"]
            assert inline["includes_handwriting"] == full["render"]["includes_handwriting"]
            assert inline["annotation_status"] == full["render"]["annotation_status"]
            assert set(inline["warnings"]) == set(
                full["text"]["warnings"] + full["render"]["warnings"]
            )
            assert "document_id" not in inline
    else:
        assert result["page_evidence"] == [
            {**page, "image": str(output / page["image"])} for page in saved["pages"]
        ]


def test_brief_inline_native_coverage_preserves_unknown_values_and_all_warnings(
    capsys: pytest.CaptureFixture[str],
) -> None:
    native = _native_metadata()
    value = {
        "manifest": "/synthetic/text.json",
        "pages": [1],
        "layer": "native",
        "truncated": False,
        "snapshot": "generation",
        "captured_at": None,
        "page_evidence": [
            {
                "page": 1,
                "image": "/synthetic/page.png",
                "render": {
                    "source": "copied-native-records",
                    "includes_handwriting": None,
                    "annotation_status": "native-reconstructed-approximate",
                    "warnings": ["Approximate ink."],
                    "native_annotations": native,
                },
                "text": {
                    "document_id": "synthetic-doc",
                    "page_id": "synthetic-page",
                    "parts": [],
                    "warnings": ["Stored recognition is unavailable."],
                    "truncated": False,
                },
            }
        ],
    }
    result = _emit_brief(capsys, "bundle", value)
    page = result["page_evidence"][0]
    assert page["includes_handwriting"] is None
    assert page["warnings"] == [
        "Approximate ink.",
        "Unverified format.",
        "Some decoded native image records omit body field 6; "
        "its effect on application appearance or visibility is unverified.",
        "Stored recognition is unavailable.",
    ]
    assert page["native_annotations"]["coverage"] == "partial"
    assert page["native_annotations"]["full_fidelity_verified"] is False
    assert page["native_annotations"]["unsupported_reasons"] == {"unknown-schema": 2}
    assert page["native_annotations"]["native_objects"] == 7
    assert page["native_annotations"]["native_image_body_field_6_absent_records"] == 1
    assert result["captured_at"] is None
    assert result["document_id"] == "synthetic-doc"


def test_brief_partial_bundle_preserves_page_error_and_unknown_image_metadata(
    capsys: pytest.CaptureFixture[str],
) -> None:
    warning = (
        "Native rendering failed for page 2; source-labelled text evidence remains "
        "available, but no image was produced."
    )
    render_error = {
        "code": "native_data_unavailable",
        "message": "The copied page has native records, but none could be rendered.",
        "details": {
            "operation": "decode_native_page",
            "page": 2,
            "path": "",
            "remedy": "Inspect the reported coverage before evaluating handwritten notes.",
        },
    }
    result = _emit_brief(
        capsys,
        "bundle",
        {
            "manifest": "/synthetic/bundle/text.json",
            "pages": [1, 2],
            "layer": "native",
            "text_source": "pdf",
            "truncated": False,
            "snapshot": "generation",
            "captured_at": None,
            "status": "partial",
            "failed_pages": [2],
            "warnings": [warning],
            "page_evidence": [
                {
                    "page": 1,
                    "image": "/synthetic/bundle/page-0001.png",
                    "render": {
                        "source": "copied-native-records-and-background",
                        "includes_handwriting": True,
                        "annotation_status": "native-reconstructed-approximate",
                        "warnings": ["Approximate ink."],
                        "native_annotations": _native_metadata(),
                    },
                    "text": {
                        "document_id": "synthetic-doc",
                        "page_id": "synthetic-page-1",
                        "parts": [],
                        "warnings": [],
                        "truncated": False,
                    },
                },
                {
                    "page": 2,
                    "image": None,
                    "render_error": render_error,
                    "warnings": [
                        "Native image rendering failed for page 2; no image is available."
                    ],
                    "text": {
                        "document_id": "synthetic-doc",
                        "page_id": "synthetic-page-2",
                        "parts": [{"source": "pdf", "text": "Page text remains available."}],
                        "warnings": ["Stored recognition is unavailable."],
                        "truncated": False,
                    },
                },
            ],
        },
    )

    failed_page = result["page_evidence"][1]
    assert result["status"] == "partial"
    assert result["failed_pages"] == [2]
    assert result["warnings"] == [warning]
    assert failed_page["image"] is None
    assert failed_page["render_error"] == render_error
    assert failed_page["parts"] == [{"source": "pdf", "text": "Page text remains available."}]
    assert failed_page["warnings"] == [
        "Native image rendering failed for page 2; no image is available.",
        "Stored recognition is unavailable.",
    ]
    assert "image_source" not in failed_page
    assert "annotation_status" not in failed_page
    assert "includes_handwriting" not in failed_page
    assert result["document_id"] == "synthetic-doc"


def test_bundle_schema_documents_partial_page_evidence() -> None:
    contract = output_contract("bundle")
    assert contract is not None
    assert contract["optional_fields"]["status"].startswith("Present as 'partial'")
    assert "failed_pages" in contract["optional_fields"]
    profile = contract["default_output"]
    assert "render_error" in profile["page_evidence"]["fields"]
    assert "null" in profile["page_evidence"]["image"]
    assert "Structured native rendering error" in profile["page_evidence"]["render_error"]


def test_brief_render_export_and_bundle_schemas_include_image_field_6_absence() -> None:
    for command in ("render", "export", "bundle"):
        contract = output_contract(command)
        assert contract is not None
        profile = contract["default_output"]
        if command == "bundle":
            fields = profile["page_evidence"]["native_annotations"]["fields"]
        else:
            fields = profile["native_annotations"]["fields"]
        assert "native_image_body_field_6_absent_records" in fields


def test_default_bundle_requires_native_data_without_background_fallback(
    generated_copy: Path, tmp_path: Path
) -> None:
    output = tmp_path / "default-bundle"
    before = _tree_manifest(generated_copy)
    result = invoke(
        ["bundle", "doc-hostile", "--output", str(output)], tmp_path, sandbox=generated_copy
    )
    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "native_data_unavailable"
    assert "--layer background" in error["details"]["remedy"]
    assert not output.exists()
    assert _tree_manifest(generated_copy) == before


def test_default_read_keeps_all_text_sources(generated_copy: Path, tmp_path: Path) -> None:
    result = successful(
        invoke(["read", "doc-hostile", "--page", "1"], tmp_path, sandbox=generated_copy)
    )
    assert result["document_id"] == "doc-hostile"
    assert {part["source"] for part in result["pages"][0]["parts"]} == {
        "pdf",
        "handwriting",
        "textbox",
    }


@pytest.mark.parametrize("source", ["pdf", "handwriting", "textbox", "all"])
def test_bundle_source_filters_text_only_and_retains_selection(
    source: str,
    generated_copy: Path,
    tmp_path: Path,
) -> None:
    result = successful(
        invoke(
            [
                "bundle",
                "doc-hostile",
                "--pages",
                "1",
                "--source",
                source,
                "--layer",
                "background",
                "--max-chars",
                "20",
                "--output",
                str(tmp_path / "filtered-bundle"),
            ],
            tmp_path,
            sandbox=generated_copy,
        )
    )
    manifest = json.loads(Path(result["manifest"]).read_text())
    parts = result["page_evidence"][0]["parts"]
    assert parts == manifest["pages"][0]["text"]["parts"]
    assert parts
    assert sum(len(part["text"]) for part in parts) <= 20
    assert result["text_source"] == manifest["text_source"] == source
    if source == "all":
        assert parts[0]["source"] == "handwriting"
    else:
        assert all(part["source"] == source for part in parts)
    assert result["page_evidence"][0]["annotation_status"] == "not-included"
    assert Path(result["page_evidence"][0]["image"]).is_file()


def test_invalid_bundle_source_fails_before_cache_or_output_access(tmp_path: Path) -> None:
    output = tmp_path / "new-parent" / "bundle"
    result = invoke(
        ["bundle", "doc-hostile", "--source", "invalid", "--output", str(output)], tmp_path
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "--source must be" in json.loads(result.stderr)["error"]["message"]
    assert not (tmp_path / "state").exists()
    assert not output.parent.exists()


def test_schema_advertises_concise_defaults_and_verbose_without_a_library(tmp_path: Path) -> None:
    result = successful(invoke_in_process(["schema", "bundle"], tmp_path))
    assert "page_evidence" in result["output"]["fields"]
    assert "text_source" in result["output"]["fields"]
    assert result["output"]["default_output"]["mode"] == "concise"
    assert result["output"]["default_output"]["full_metadata_option"] == "--verbose"
    assert (
        result["output"]["default_output"]["page_evidence"]["parts"]
        == "Unchanged source-labelled text parts."
    )
    assert not any("--inline" in option["flags"] for option in result["options"])
    assert not any("--brief" in option["flags"] for option in result["global_options"])
    assert any(option["flags"] == ["--verbose"] for option in result["global_options"])
    defaults = {option["flags"][0]: option["default"] for option in result["options"]}
    assert defaults["--layer"] == "native"
    assert defaults["--source"] == "pdf"
    assert not (tmp_path / "state").exists()


def test_page_evidence_projection_uses_full_contract(
    generated_copy: Path,
    tmp_path: Path,
) -> None:
    output = tmp_path / "new-parent" / "bundle"
    result = successful(
        invoke(
            [
                "--fields",
                "page_evidence",
                "bundle",
                "doc-hostile",
                "--layer",
                "background",
                "--source",
                "all",
                "--output",
                str(output),
            ],
            tmp_path,
            sandbox=generated_copy,
        )
    )
    assert set(result) == {"page_evidence", "snapshot"}
    assert "render" in result["page_evidence"][0]
    assert "text" in result["page_evidence"][0]


@pytest.mark.parametrize(
    "arguments",
    [["--brief", "schema"], ["bundle", "doc-hostile", "--inline", "--output", "/synthetic/unused"]],
)
def test_removed_flags_have_no_hidden_aliases(arguments: list[str], tmp_path: Path) -> None:
    result = invoke(arguments, tmp_path)
    assert result.returncode == 2
    assert not (tmp_path / "state").exists()
