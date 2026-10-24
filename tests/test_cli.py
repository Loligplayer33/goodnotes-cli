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
