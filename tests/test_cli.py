"""Subprocess tests of the installed agent command contract."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from test_library import _create_snapshot


@pytest.fixture
def library_copy(tmp_path: Path) -> Path:
    sandbox = _create_snapshot(tmp_path / "snapshot")
    pdf = pymupdf.open()
    for number in range(1, 13):
        page = pdf.new_page()
        page.insert_text((50, 50), f"Synthetic PDF page {number}")
    pdf.save(sandbox.attachments_dir / "safe.pdf")
    pdf.close()
    shutil.copyfile(sandbox.attachments_dir / "safe.pdf", sandbox.attachments_dir / "fallback.pdf")
    with sqlite3.connect(sandbox.projection_path) as db:
        db.execute("UPDATE attachments SET path = 'safe.pdf' WHERE id = 'attachment-unsafe'")
    return sandbox.root


def invoke(
    arguments: list[str], tmp_path: Path, *, sandbox: Path | None = None
) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("GOODNOTES_SANDBOX", None)
    environment["GOODNOTES_DATA_DIR"] = str(tmp_path / "state")
    prefix = ["--sandbox", str(sandbox)] if sandbox is not None else []
    return subprocess.run(
        [sys.executable, "-m", "goodnotes_cli", *prefix, *arguments],
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
        check=False,
    )


def successful(result: subprocess.CompletedProcess[str]) -> dict:
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert len(result.stdout.splitlines()) == 1
    return json.loads(result.stdout)


def test_schema_needs_no_library(tmp_path: Path) -> None:
    data = successful(invoke(["schema"], tmp_path))
    assert data["page_numbers"] == "one-based"
    assert data["limits"]["read_pages"] == 20


def test_schema_explains_field_projection_without_loading_notes(tmp_path: Path) -> None:
    reading = successful(invoke(["schema", "read"], tmp_path))
    assert reading["output"]["projection"] == "pages[]"
    assert "parts" in reading["output"]["fields"]
    assert "page_id" in reading["output"]["fields"]
    pages = successful(invoke(["schema", "pages"], tmp_path))
    assert "number" in pages["output"]["fields"]
    assert "recognition" in pages["output"]["optional_fields"]
    assert not (tmp_path / "state").exists()


def test_field_errors_show_the_projection_level_and_valid_keys(
    library_copy: Path, tmp_path: Path
) -> None:
    result = invoke(
        ["--fields", "pages,snapshot", "read", "doc-hostile", "--page", "1"],
        tmp_path,
        sandbox=library_copy,
    )
    assert result.returncode == 1
    assert result.stdout == ""
    message = json.loads(result.stderr)["error"]["message"]
    assert "pages[]" in message
    assert "page_id" in message
    assert "parts" in message
    assert "retained automatically" in message


def test_top_level_field_selection_accepts_and_retains_snapshot(
    library_copy: Path, tmp_path: Path
) -> None:
    data = successful(
        invoke(["--fields", "id,snapshot", "info", "doc-hostile"], tmp_path, sandbox=library_copy)
    )
    assert data == {"id": "doc-hostile", "snapshot": library_copy.name}


def _assert_invalid_fields_are_rejected_before_write_command(
    writer: str, library_copy: Path, tmp_path: Path
) -> None:
    annotated_pdf = tmp_path / "annotated.pdf"
    document = pymupdf.open()
    for _ in range(3):
        document.new_page()
    document.save(annotated_pdf)
    document.close()

    output = tmp_path / "output.pdf"
    bundle_parent = tmp_path / "new-parent"
    if writer == "sync":
        arguments = ["sync", "--library", str(library_copy / "Library")]
    elif writer == "snapshot":
        output = tmp_path / "new-snapshot"
        arguments = ["snapshot", str(library_copy / "Library"), str(output)]
    elif writer == "attach-pdf":
        arguments = ["attach-pdf", "doc-hostile", str(annotated_pdf)]
    elif writer == "export":
        arguments = ["export", "doc-hostile", "--output", str(output)]
    elif writer == "render":
        output = tmp_path / "output.png"
        arguments = ["render", "doc-hostile", "--output", str(output)]
    else:
        output = bundle_parent / "bundle"
        arguments = ["bundle", "doc-hostile", "--output", str(output)]

    result = invoke(
        ["--fields", "definitely-not-a-field", *arguments],
        tmp_path,
        sandbox=library_copy if writer != "sync" and writer != "snapshot" else None,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"
    assert not output.exists()
    assert not (tmp_path / "state").exists()
    assert not (library_copy.parent / ".goodnotes-cli").exists()
    assert not bundle_parent.exists()


@pytest.mark.parametrize("writer", ["sync", "snapshot", "attach-pdf", "export", "render", "bundle"])
def test_invalid_fields_preflight_all_writers(
    writer: str, library_copy: Path, tmp_path: Path
) -> None:
    _assert_invalid_fields_are_rejected_before_write_command(writer, library_copy, tmp_path)


@pytest.mark.parametrize("writer", ["export", "render", "attach-pdf", "bundle"])
def test_top_level_writers_can_project_snapshot(
    writer: str, library_copy: Path, tmp_path: Path
) -> None:
    annotated_pdf = tmp_path / "annotated.pdf"
    document = pymupdf.open()
    for _ in range(3):
        document.new_page()
    document.save(annotated_pdf)
    document.close()

    schema = successful(invoke(["schema", writer], tmp_path))
    assert "snapshot" in schema["output"]["fields"]

    if writer == "export":
        arguments = ["export", "doc-hostile", "--output", str(tmp_path / "out.pdf")]
    elif writer == "render":
        arguments = ["render", "doc-hostile", "--output", str(tmp_path / "out.png")]
    elif writer == "attach-pdf":
        arguments = ["attach-pdf", "doc-hostile", str(annotated_pdf)]
    else:
        arguments = ["bundle", "doc-hostile", "--output", str(tmp_path / "bundle")]

    data = successful(invoke(["--fields", "snapshot", *arguments], tmp_path, sandbox=library_copy))

    assert data == {"snapshot": library_copy.name}


def test_sync_projection_can_select_an_absent_optional_warning_key(
    library_copy: Path, tmp_path: Path
) -> None:
    data = successful(
        invoke(
            [
                "--fields",
                "status,warnings",
                "sync",
                "--library",
                str(library_copy / "Library"),
            ],
            tmp_path,
        )
    )

    assert data["status"] == "ready"
    assert "warnings" not in data
    assert (tmp_path / "state" / "active.json").is_file()


def test_unavailable_search_index_returns_scoped_coverage_and_pdf_fallback(
    library_copy: Path, tmp_path: Path
) -> None:
    (library_copy / "Library/Databases/fts.sqlite").unlink()

    result = invoke(
        ["search", "missing phrase", "--notebook", "doc-hostile"],
        tmp_path,
        sandbox=library_copy,
    )

    assert result.returncode == 1
    assert result.stdout == ""
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "operation_failed"
    assert error["details"]["coverage"] == "stored-index"
    details = error["details"]["coverage_detail"]
    assert details["scope"]["notebook_id"] == "doc-hostile"
    assert any("scan-pdfs" in step for step in details["next_steps"])
    assert error["details"]["warnings"]


@pytest.mark.parametrize(
    ("query", "notebook", "message"),
    [
        ("", "doc-hostile", "Search query must not be empty"),
        ("phrase", "missing-notebook", "No active, reachable item matches"),
    ],
)
def test_unavailable_index_does_not_mask_search_input_errors(
    library_copy: Path, tmp_path: Path, query: str, notebook: str, message: str
) -> None:
    (library_copy / "Library/Databases/fts.sqlite").unlink()

    result = invoke(
        ["search", query, "--notebook", notebook],
        tmp_path,
        sandbox=library_copy,
    )

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert message in error["message"]
    assert "details" not in error


def test_invalid_bundle_page_selection_does_not_create_parent(
    library_copy: Path, tmp_path: Path
) -> None:
    parent = tmp_path / "not-created"
    result = invoke(
        ["bundle", "doc-hostile", "--pages", "0", "--output", str(parent / "bundle")],
        tmp_path,
        sandbox=library_copy,
    )

    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"
    assert not parent.exists()


@pytest.mark.parametrize("command", ["search", "scan-pdfs"])
def test_search_scope_options_are_exclusive_before_loading_a_library(
    command: str, tmp_path: Path
) -> None:
    result = invoke([command, "term", "--folder", "/", "--notebook", "Notebook"], tmp_path)
    assert result.returncode == 1
    assert "Use --folder or --notebook" in json.loads(result.stderr)["error"]["message"]
    assert not (tmp_path / "state").exists()


def test_export_selects_pages_in_listed_order(library_copy: Path, tmp_path: Path) -> None:
    target = tmp_path / "selected.pdf"
    data = successful(
        invoke(
            ["export", "doc-hostile", "--pages", "3,1", "--output", str(target)],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert data["pages"] == 2
    assert data["selected_pages"] == [3, 1]
    assert data["includes_handwriting"] is False
    with pymupdf.open(target) as document:
        assert document.page_count == 2
        assert "Synthetic PDF page 1" in document[0].get_text()
        assert "Synthetic PDF page 9" in document[1].get_text()


@pytest.mark.parametrize("expression", ["", "0", "1,1", "3-1", "1,4"])
def test_export_rejects_invalid_page_selections_without_creating_output(
    expression: str, library_copy: Path, tmp_path: Path
) -> None:
    target = tmp_path / "invalid.pdf"
    result = invoke(
        ["export", "doc-hostile", "--pages", expression, "--output", str(target)],
        tmp_path,
        sandbox=library_copy,
    )
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"
    assert not target.exists()


def test_notebook_search_does_not_confuse_metadata_with_text_coverage(
    library_copy: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(library_copy / "Library/Databases/fts.sqlite") as db:
        db.execute("DELETE FROM pdf_text_content")
    data = successful(
        invoke(
            ["search", "Synthetic", "--notebook", "doc-hostile", "--source", "pdf"],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert data["hits"] == []
    assert data["coverage"] == "stored-index"
    details = data["coverage_detail"]
    assert details["scope"]["notebook_id"] == "doc-hostile"
    assert details["active_pages"] == 3
    assert details["page_meta"]["mapped_active_pages"] == 3
    assert details["sources"]["pdf"]["pages_with_stored_nonempty_text"] == 0
    assert details["sources"]["pdf"]["metadata_without_nonempty_text_pages"] == 2
    assert details["completeness"] == "unknown"
    assert "scan-pdfs" in json.dumps(details["next_steps"])


def test_notebook_pdf_scan_distinguishes_window_end_from_complete_coverage(
    library_copy: Path, tmp_path: Path
) -> None:
    arguments = ["scan-pdfs", "Synthetic PDF", "--notebook", "doc-hostile", "--limit", "100"]
    first = successful(invoke([*arguments, "--page-limit", "2"], tmp_path, sandbox=library_copy))
    assert first["scope"]["notebook_id"] == "doc-hostile"
    assert first["total_pages"] == 3
    assert first["pages_scanned"] == 2
    assert first["next_page_offset"] == 2
    assert first["stop_reason"] == "page-limit"
    assert first["scan_complete"] is False
    assert first["coverage_complete"] is False
    final = successful(invoke([*arguments, "--page-offset", "2"], tmp_path, sandbox=library_copy))
    assert final["scan_complete"] is True
    assert final["coverage_complete"] is False
    assert final["next_page_offset"] is None
    complete = successful(invoke(arguments, tmp_path, sandbox=library_copy))
    assert complete["coverage_complete"] is True
    assert complete["extraction_issues"] == []
    assert [hit["page"] for hit in complete["hits"]] == [1, 2, 3]


@pytest.mark.parametrize("command", ["search", "scan-pdfs"])
def test_notebook_passed_as_folder_has_actionable_error(
    command: str, library_copy: Path, tmp_path: Path
) -> None:
    result = invoke([command, "phrase", "--folder", "doc-hostile"], tmp_path, sandbox=library_copy)
    assert result.returncode == 1
    assert "--notebook" in json.loads(result.stderr)["error"]["message"]


def test_recognition_discovery_is_metadata_only_and_marks_pending_empty_pages(
    library_copy: Path, tmp_path: Path
) -> None:
    with sqlite3.connect(library_copy / "Library/Databases/projection.sqlite") as db:
        db.execute("ALTER TABLE pages ADD COLUMN needs_index INTEGER")
        db.execute("UPDATE pages SET needs_index = 1 WHERE id = 'page-30'")
    data = successful(
        invoke(
            ["--fields", "number,recognition", "pages", "doc-hostile", "--with-recognition"],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert data["annotation_count_source"] == "projection.notes_items"
    assert data["snapshot"] == library_copy.name
    assert [page["recognition"]["text_rows"] for page in data["pages"]] == [2, 1, 0]
    assert data["pages"][0]["recognition"]["availability"] == "stored-text"
    assert data["pages"][2]["recognition"]["index_state"] == "pending"
    assert "Straße" not in json.dumps(data)
    reading = successful(
        invoke(
            ["read", "doc-hostile", "--page", "3", "--source", "handwriting"],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert reading["pages"][0]["parts"] == []
    assert "index" in " ".join(reading["pages"][0]["warnings"]).casefold()


def test_schema_covers_every_registered_command(tmp_path: Path) -> None:
    from typer.core import TyperGroup
    from typer.main import get_command

    from goodnotes_cli.cli import app

    data = successful(invoke(["schema"], tmp_path))
    group = get_command(app)
    assert isinstance(group, TyperGroup)
    assert set(data["commands"]) == set(group.commands)
    assert "--version" in data["global_options"]
    assert "--help" in data["global_options"]
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize(
    "command_name",
    [
        "sync",
        "snapshot",
        "ls",
        "tree",
        "info",
        "pages",
        "read",
        "search",
        "scan-pdfs",
        "export",
        "render",
        "attach-pdf",
        "bundle",
        "doctor",
        "schema",
    ],
)
def test_targeted_schema_and_help_need_no_library(command_name: str, tmp_path: Path) -> None:
    data = successful(invoke(["schema", command_name], tmp_path))
    assert data["command"] == command_name
    assert any("--help" in p["flags"] for p in data["options"])
    assert any(p.get("envvar") == "GOODNOTES_DATA_DIR" for p in data["global_options"])
    help_result = invoke([command_name, "--help"], tmp_path)
    assert help_result.returncode == 0
    assert "Example:" in help_result.stdout
    assert not (tmp_path / "state").exists()


def test_targeted_schema_describes_actual_bounds_and_parameter_kinds(tmp_path: Path) -> None:
    read = successful(invoke(["schema", "read"], tmp_path))
    budget = next(p for p in read["options"] if p["name"] == "max_chars")
    assert (budget["default"], budget["min"], budget["max"]) == (12000, 1, 1000000)
    assert read["arguments"][0]["name"] == "notebook"
    listing = successful(invoke(["schema", "ls"], tmp_path))
    assert listing["arguments"] == []
    folder = next(p for p in listing["options"] if p["name"] == "folder")
    assert folder["flags"] == ["--folder"]
    assert folder["default"] == "/"
    capture = successful(invoke(["schema", "snapshot"], tmp_path))
    assert [p["name"] for p in capture["arguments"]] == ["library", "destination"]
    sync = successful(invoke(["schema", "sync"], tmp_path))
    force = next(p for p in sync["options"] if p["name"] == "force")
    assert force["flags"] == ["--force", "--no-force"]


def test_unknown_schema_target_is_structured_argument_error(tmp_path: Path) -> None:
    result = invoke(["schema", "missing"], tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_arguments"


def test_missing_library_is_structured_stderr(tmp_path: Path) -> None:
    result = invoke(["ls"], tmp_path)
    assert result.returncode == 1
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "operation_failed"


@pytest.mark.parametrize("arguments", [["read"], ["ls", "--limit", "0"], ["unknown"]])
def test_argument_errors_are_structured(arguments: list[str], tmp_path: Path) -> None:
    result = invoke(arguments, tmp_path)
    assert result.returncode == 2
    assert result.stdout == ""
    assert json.loads(result.stderr)["error"]["code"] == "invalid_arguments"


def test_listing_paginates_and_projects_fields(library_copy: Path, tmp_path: Path) -> None:
    first = successful(
        invoke(["--fields", "id,kind", "ls", "--limit", "1"], tmp_path, sandbox=library_copy)
    )
    second = successful(
        invoke(["ls", "--limit", "1", "--offset", "1"], tmp_path, sandbox=library_copy)
    )
    assert set(first["items"][0]) == {"id", "kind"}
    assert first["next_offset"] == 1
    assert first["items"][0]["id"] != second["items"][0]["id"]


def test_read_reports_budget_and_preserves_handwriting(library_copy: Path, tmp_path: Path) -> None:
    data = successful(
        invoke(
            ["read", "doc-hostile", "--page", "1", "--source", "handwriting", "--max-chars", "4"],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert data["truncated"] is True
    assert sum(len(part["text"]) for page in data["pages"] for part in page["parts"]) <= 4
    raw = successful(
        invoke(
            ["read", "doc-hostile", "--page", "1", "--source", "handwriting"],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert any("|" in part["text"] for part in raw["pages"][0]["parts"])


@pytest.mark.parametrize("expression", ["0", "1-30", "2-1", "1,1", "1,,2"])
def test_bad_page_ranges_fail(library_copy: Path, tmp_path: Path, expression: str) -> None:
    result = invoke(["read", "doc-hostile", "--pages", expression], tmp_path, sandbox=library_copy)
    assert result.returncode == 1
    assert json.loads(result.stderr)["error"]["message"]


def test_background_render_and_bundle(library_copy: Path, tmp_path: Path) -> None:
    result = successful(
        invoke(
            ["render", "doc-hostile", "--page", "1", "--output", str(tmp_path / "page.png")],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert result["includes_handwriting"] is False
    image = pymupdf.Pixmap(tmp_path / "page.png")
    assert image.width > 0
    bundle = successful(
        invoke(
            ["bundle", "doc-hostile", "--pages", "1-2", "--output", str(tmp_path / "bundle")],
            tmp_path,
            sandbox=library_copy,
        )
    )
    manifest = json.loads(Path(bundle["manifest"]).read_text())
    assert [page["page"] for page in manifest["pages"]] == [1, 2]
    assert all((tmp_path / "bundle" / page["image"]).is_file() for page in manifest["pages"])


def test_annotated_requests_fail_without_registration(library_copy: Path, tmp_path: Path) -> None:
    target = tmp_path / "annotated.pdf"
    result = invoke(
        ["export", "doc-hostile", "--layer", "annotated", "--output", str(target)],
        tmp_path,
        sandbox=library_copy,
    )
    assert result.returncode == 1
    assert not target.exists()
    assert json.loads(result.stderr)["error"]["message"]


def test_attach_export_annotated_and_no_overwrite(library_copy: Path, tmp_path: Path) -> None:
    exported = tmp_path / "goodnotes-export.pdf"
    document = pymupdf.open()
    for number in range(1, 4):
        page = document.new_page()
        page.insert_text((50, 50), f"User annotation page {number}")
    document.save(exported)
    document.close()
    registered = successful(
        invoke(["attach-pdf", "doc-hostile", str(exported)], tmp_path, sandbox=library_copy)
    )
    assert registered["registered"] is True
    output = tmp_path / "full.pdf"
    result = successful(
        invoke(
            ["export", "doc-hostile", "--layer", "annotated", "--output", str(output)],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert result["layer"] == "annotated"
    with pymupdf.open(output) as pdf:
        assert pdf.page_count == 3
        assert "User annotation page 2" in pdf[1].get_text()
    duplicate = invoke(
        ["export", "doc-hostile", "--output", str(output)], tmp_path, sandbox=library_copy
    )
    assert duplicate.returncode == 1


def test_sync_and_active_pointer_use_only_supplied_copy(library_copy: Path, tmp_path: Path) -> None:
    data = successful(invoke(["sync", "--library", str(library_copy / "Library")], tmp_path))
    assert Path(data["snapshot"]).is_relative_to(tmp_path / "state")
    listing = successful(invoke(["ls", "--limit", "1"], tmp_path))
    assert listing["items"]


def test_bundle_budget_and_published_paths(library_copy: Path, tmp_path: Path) -> None:
    data = successful(
        invoke(
            [
                "bundle",
                "doc-hostile",
                "--pages",
                "1-2",
                "--max-chars",
                "5",
                "--output",
                str(tmp_path / "limited"),
            ],
            tmp_path,
            sandbox=library_copy,
        )
    )
    manifest = json.loads(Path(data["manifest"]).read_text())
    assert data["truncated"] is True
    assert manifest["truncated"] is True
    assert (
        sum(len(part["text"]) for entry in manifest["pages"] for part in entry["text"]["parts"])
        <= 5
    )
    assert all(Path(entry["render"]["path"]).is_file() for entry in manifest["pages"])


def test_bundle_publisher_does_not_replace_racing_directory(tmp_path: Path) -> None:
    from goodnotes_cli.cli import publish_bundle

    temporary = tmp_path / "temporary"
    temporary.mkdir()
    (temporary / "text.json").write_text("{}")
    destination = tmp_path / "output"
    destination.mkdir()
    original_inode = destination.stat().st_ino
    with pytest.raises(FileExistsError):
        publish_bundle(temporary, destination)
    assert destination.stat().st_ino == original_inode
    assert not list(destination.iterdir())


def test_bundle_projects_top_level_fields_with_numeric_pages(
    library_copy: Path, tmp_path: Path
) -> None:
    data = successful(
        invoke(
            [
                "--fields",
                "pages,manifest",
                "bundle",
                "doc-hostile",
                "--pages",
                "1-2",
                "--output",
                str(tmp_path / "projected-bundle"),
            ],
            tmp_path,
            sandbox=library_copy,
        )
    )
    assert set(data) == {"pages", "manifest", "snapshot"}
    assert data["pages"] == [1, 2]
    assert Path(data["manifest"]).is_file()


def test_failed_refresh_preserves_active_view(library_copy: Path, tmp_path: Path) -> None:
    successful(invoke(["sync", "--library", str(library_copy / "Library")], tmp_path))
    pointer = tmp_path / "state" / "active.json"
    before = pointer.read_bytes()
    (library_copy / "Library" / "Databases" / "projection.sqlite").unlink()
    failed = invoke(["sync", "--library", str(library_copy / "Library")], tmp_path)
    assert failed.returncode == 1
    assert pointer.read_bytes() == before
    assert successful(invoke(["ls", "--limit", "1"], tmp_path))["items"]


def test_large_search_cursor_is_accepted(library_copy: Path, tmp_path: Path) -> None:
    data = successful(
        invoke(["search", "needle", "--offset", "10100"], tmp_path, sandbox=library_copy)
    )
    assert data["hits"] == []
    assert data["coverage"] == "stored-index"


def test_pdf_scan_has_page_cursor(library_copy: Path, tmp_path: Path) -> None:
    data = successful(
        invoke(["scan-pdfs", "Synthetic PDF", "--page-limit", "1"], tmp_path, sandbox=library_copy)
    )
    assert data["pages_scanned"] == 1
    assert data["next_page_offset"] == 1
    assert data["hits"][0]["source"] == "pdf"


def test_render_loads_read_view_only_once(
    library_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from goodnotes_cli.cli import app
    from goodnotes_cli.config import Settings
    from goodnotes_cli.safety import Sandbox

    calls = []

    def load_once(self: Settings) -> Sandbox:
        calls.append(True)
        if len(calls) > 1:
            raise AssertionError("A running command reloaded the active pointer")
        return Sandbox.load(library_copy)

    monkeypatch.setattr(Settings, "load_sandbox", load_once)
    result = CliRunner().invoke(
        app,
        [
            "--sandbox",
            str(library_copy),
            "render",
            "doc-hostile",
            "--output",
            str(tmp_path / "pinned.png"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(calls) == 1


def test_snapshot_generation_guard(library_copy: Path, tmp_path: Path) -> None:
    first = successful(invoke(["ls", "--limit", "1"], tmp_path, sandbox=library_copy))
    current = first["snapshot"]
    matching = successful(
        invoke(
            ["--expect-snapshot", current, "ls", "--offset", "1"], tmp_path, sandbox=library_copy
        )
    )
    assert matching["snapshot"] == current
    stale = invoke(["--expect-snapshot", "old-generation", "ls"], tmp_path, sandbox=library_copy)
    assert stale.returncode == 1
    assert json.loads(stale.stderr)["error"]["code"] == "stale_snapshot"
    assert stale.stdout == ""


def test_sync_reuses_unchanged_and_force_rebuilds(library_copy: Path, tmp_path: Path) -> None:
    arguments = ["sync", "--library", str(library_copy / "Library")]
    first = successful(invoke(arguments, tmp_path))
    unchanged = successful(invoke(arguments, tmp_path))
    assert unchanged["status"] == "unchanged"
    assert first["snapshot"] == unchanged["snapshot"]
    forced = successful(invoke([*arguments, "--force"], tmp_path))
    assert forced["status"] == "ready"
    assert first["snapshot"] != forced["snapshot"]
    assert not Path(first["snapshot"]).exists()
