"""Public defaults should guide a bounded, visual notebook workflow."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from tests.support.cli import invoke_in_process, successful
from tests.support.library import _create_snapshot


def test_primary_help_and_schema_share_a_small_command_index(tmp_path: Path) -> None:
    index = successful(invoke_in_process(["schema"], tmp_path))
    assert set(index["commands"]) == {
        "sync",
        "ls",
        "info",
        "pages",
        "read",
        "search",
        "bundle",
        "export",
        "doctor",
        "schema",
    }
    assert "attach-pdf" in index["advanced_commands"]
    assert "render" in index["advanced_commands"]
    help_text = invoke_in_process(["--help"], tmp_path).stdout
    assert "attach-pdf" not in help_text
    assert "bundle" in help_text
    targeted = successful(invoke_in_process(["schema", "attach-pdf"], tmp_path))
    assert targeted["command"] == "attach-pdf"
    assert not (tmp_path / "state").exists()


def test_source_and_visual_defaults_are_consistent_without_loading_notes(tmp_path: Path) -> None:
    for command in ("read", "search", "bundle"):
        contract = successful(invoke_in_process(["schema", command], tmp_path))
        defaults = {option["name"]: option["default"] for option in contract["options"]}
        assert defaults["source"] == "text"
    for command in ("render", "export", "bundle"):
        contract = successful(invoke_in_process(["schema", command], tmp_path))
        defaults = {option["name"]: option["default"] for option in contract["options"]}
        assert defaults["layer"] == "native"
    read = successful(invoke_in_process(["schema", "read"], tmp_path))
    assert next(option for option in read["options"] if option["name"] == "page")["advanced"]
    sync = successful(invoke_in_process(["schema", "sync"], tmp_path))
    assert (
        "during this refresh only" in sync["output"]["semantics"]["source_changed_during_refresh"]
    )


def test_folder_filter_applies_before_pagination(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "generated-copy")
    prefix = ["--sandbox", str(sandbox.root), "ls"]
    all_items = successful(invoke_in_process(prefix, tmp_path))["items"]
    folders = [item for item in all_items if item["kind"] == "folder"]
    page = successful(invoke_in_process([*prefix, "--kind", "folder", "--limit", "1"], tmp_path))
    assert page["items"] == folders[:1]
    assert page["next_offset"] == (1 if len(folders) > 1 else None)
    invalid = invoke_in_process([*prefix, "--kind", "invalid"], tmp_path)
    assert invalid.returncode == 1
    assert "--kind" in json.loads(invalid.stderr)["error"]["message"]


def test_recognition_is_available_for_projection_without_an_extra_flag(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "generated-copy")
    result = successful(
        invoke_in_process(
            [
                "--sandbox",
                str(sandbox.root),
                "--fields",
                "number,recognition",
                "pages",
                "doc-hostile",
            ],
            tmp_path,
        )
    )
    assert result["pages"]
    assert all("recognition" in page for page in result["pages"])
    assert all("text" not in page["recognition"] for page in result["pages"])


def test_access_only_rejects_notebook_scope_without_reading_a_copy(tmp_path: Path) -> None:
    result = invoke_in_process(["doctor", "--access-only", "--notebook", "Notes"], tmp_path)
    assert result.returncode == 1
    assert "--notebook" in json.loads(result.stderr)["error"]["message"]
    assert not (tmp_path / "state").exists()


def test_notebook_diagnostics_project_scoped_fields_and_historical_unknowns(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "generated-copy")
    result = successful(
        invoke_in_process(
            [
                "--sandbox",
                str(sandbox.root),
                "--fields",
                "scope,page_index,provenance",
                "doctor",
                "--notebook",
                "doc-hostile",
            ],
            tmp_path,
        )
    )
    assert result["scope"]["notebook_id"] == "doc-hostile"
    assert result["page_index"]["active_pages"] == 3
    assert result["provenance"]["producer"] is None
    assert result["provenance"]["source_app"] is None
    assert result["provenance"]["library_format"] is None
    assert len(result["provenance"]["cli"]["source_sha256"]) == 64
    assert "active_notebooks" not in result

    schema = successful(invoke_in_process(["schema", "doctor"], tmp_path))
    assert "page_index" in schema["output"]["variants"]["notebook"]["fields"]
    assert "orphaned_folders" in schema["output"]["fields"]
    invalid = invoke_in_process(
        [
            "--sandbox",
            str(sandbox.root),
            "--fields",
            "active_pages",
            "doctor",
            "--notebook",
            "doc-hostile",
        ],
        tmp_path,
    )
    assert invalid.returncode == 1
    assert "Unknown output fields" in json.loads(invalid.stderr)["error"]["message"]


def test_pages_do_not_turn_an_unavailable_annotation_count_into_zero(tmp_path: Path) -> None:
    sandbox = _create_snapshot(tmp_path / "generated-copy")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("DROP TABLE notes_items")
    result = successful(
        invoke_in_process(["--sandbox", str(sandbox.root), "pages", "doc-hostile"], tmp_path)
    )
    assert result["annotation_count_source"] is None
    assert all(page["annotation_count"] is None for page in result["pages"])
