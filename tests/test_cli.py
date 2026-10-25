"""CLI contract tests; subprocesses cover behavior that needs process boundaries."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pymupdf
import pytest
from helpers import invoke_in_process
from test_library import _add_repeated_fallback_pages, _create_snapshot

import goodnotes_cli.cache as cache_module
from goodnotes_cli.config import Settings
from goodnotes_cli.errors import CacheAccessError


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
    arguments: list[str],
    tmp_path: Path,
    *,
    sandbox: Path | None = None,
    data_dir: Path | None = None,
    verbose: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Exercise the full response contract unless a test requests concise defaults."""
    environment = dict(os.environ)
    environment.pop("GOODNOTES_SANDBOX", None)
    environment["GOODNOTES_DATA_DIR"] = str(data_dir or tmp_path / "state")
    prefix = ["--sandbox", str(sandbox)] if sandbox is not None else []
    if verbose:
        prefix.append("--verbose")
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


def test_watch_runs_as_json_lines_and_leaves_unchanged_checks_quiet(
    library_copy: Path, tmp_path: Path
) -> None:
    before = _tree_manifest(library_copy)
    result = invoke(
        [
            "watch",
            "--library",
            str(library_copy / "Library"),
            "--interval",
            "0.05",
            "--debounce",
            "0",
            "--iterations",
            "3",
        ],
        tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    events = [json.loads(line) for line in result.stdout.splitlines()]
    assert len(events) == 1
    assert events[0]["event"] == "ready"
    assert events[0]["iteration"] == 1
    assert events[0]["captured_at"]
    assert events[0]["capture_stats"]["source_captured_files"] > 0
    assert _tree_manifest(library_copy) == before
    assert len(list((tmp_path / "state" / "snapshots").glob("20*"))) == 1


@pytest.mark.parametrize("command", ["sync", "watch"])
def test_refresh_rejects_global_sandbox_instead_of_selecting_default_source(
    command: str, library_copy: Path, tmp_path: Path
) -> None:
    response = invoke([command], tmp_path, sandbox=library_copy)
    assert response.returncode == 1
    assert response.stdout == ""
    assert "omit the global --sandbox" in json.loads(response.stderr)["error"]["message"]
    assert not (tmp_path / "state").exists()


def test_doctor_access_only_reports_uninitialized_cache_without_creating_it(tmp_path: Path) -> None:
    response = successful(invoke(["doctor", "--access-only"], tmp_path))
    assert response["scope"] == "filesystem-access"
    assert response["status"] == "issues"
    assert not (tmp_path / "state").exists()


def test_doctor_access_only_honors_explicit_sandbox(library_copy: Path, tmp_path: Path) -> None:
    before = _tree_manifest(library_copy)
    response = successful(invoke(["doctor", "--access-only"], tmp_path, sandbox=library_copy))
    assert response["sandbox"] == str(library_copy)
    assert response["cache_dir"] is None
    assert not (tmp_path / "state").exists()
    assert _tree_manifest(library_copy) == before


def test_doctor_access_paths_require_access_only(tmp_path: Path) -> None:
    response = invoke(["doctor", "--library", str(tmp_path / "source")], tmp_path)
    assert response.returncode == 1
    assert "require --access-only" in json.loads(response.stderr)["error"]["message"]


def test_bounded_watch_ending_in_error_exits_unsuccessfully(tmp_path: Path) -> None:
    response = invoke(
        ["watch", "--library", str(tmp_path / "missing-source"), "--iterations", "1"],
        tmp_path,
    )
    assert response.returncode == 1
    assert response.stderr == ""
    event = json.loads(response.stdout)
    assert event["event"] == "error"
    assert event["error"]["message"]


def test_bundle_output_permission_error_retains_operation_and_path(
    library_copy: Path, tmp_path: Path
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("A root process can write despite directory mode bits")
    parent = tmp_path / "denied-output"
    parent.mkdir(mode=0o700)
    parent.chmod(0o500)
    try:
        response = invoke(
            [
                "bundle",
                "doc-hostile",
                "--layer",
                "background",
                "--source",
                "all",
                "--output",
                str(parent / "bundle"),
            ],
            tmp_path,
            sandbox=library_copy,
        )
    finally:
        parent.chmod(0o700)
    assert response.returncode == 1
    error = json.loads(response.stderr)["error"]
    assert error["code"] == "output_permission_denied"
    assert error["details"]["operation"] == "write_bundle_output"
    assert Path(error["details"]["path"]).is_relative_to(parent)
    assert not list(parent.iterdir())


def _tree_manifest(root: Path) -> tuple[tuple[object, ...], ...]:
    entries: list[tuple[object, ...]] = []
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        info = path.lstat()
        is_file = stat.S_ISREG(info.st_mode)
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if is_file else None
        entries.append(
            (
                path.relative_to(root).as_posix() if path != root else ".",
                stat.S_IFMT(info.st_mode),
                stat.S_IMODE(info.st_mode),
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
                digest,
            )
        )
    return tuple(entries)


def _top_level_manifest(root: Path) -> tuple[tuple[object, ...], ...]:
    return tuple(
        sorted(
            (
                path.name,
                path.lstat().st_mode,
                path.lstat().st_size,
                path.lstat().st_mtime_ns,
                path.lstat().st_ctime_ns,
                os.readlink(path) if path.is_symlink() else None,
            )
            for path in root.iterdir()
        )
    )


def _resolution_error_path(path: Path, *, runtime_fallback: Path | None = None) -> Path:
    fallback = runtime_fallback or path
    try:
        path.resolve(strict=True)
    except OSError as exc:
        if exc.filename is not None:
            return Path(os.fsdecode(exc.filename))
        return fallback
    except RuntimeError:
        return fallback
    pytest.fail(f"strict path resolution unexpectedly succeeded: {path}")


def _set_tree_readonly(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o555)
        elif stat.S_ISREG(mode):
            path.chmod(0o444)
    root.chmod(0o555)


def _restore_tree_writable(root: Path) -> None:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            path.chmod(0o700)
        elif stat.S_ISREG(mode):
            path.chmod(0o600)
    root.chmod(0o700)


def test_schema_needs_no_library(tmp_path: Path) -> None:
    data = successful(invoke_in_process(["schema"], tmp_path))
    assert data["page_numbers"] == "one-based"
    assert data["limits"]["read_pages"] == 20


def test_schema_explains_field_projection_without_loading_notes(tmp_path: Path) -> None:
    reading = successful(invoke_in_process(["schema", "read"], tmp_path))
    assert reading["output"]["projection"] == "pages[]"
    assert "parts" in reading["output"]["fields"]
    assert "page_id" in reading["output"]["fields"]
    pages = successful(invoke_in_process(["schema", "pages"], tmp_path))
    assert "number" in pages["output"]["fields"]
    assert "recognition" in pages["output"]["optional_fields"]
    assert not (tmp_path / "state").exists()


def test_doctor_schema_documents_access_only_projection(tmp_path: Path) -> None:
    data = successful(invoke_in_process(["schema", "doctor"], tmp_path))
    output = data["output"]
    assert "active_pages" in output["fields"]
    assert "status" in output["optional_fields"]
    assert "status" not in output["variants"]["default"]["fields"]
    assert "status" in output["variants"]["access_only"]["fields"]
    assert "active_pages" not in output["variants"]["access_only"]["fields"]


def test_doctor_access_only_fields_are_preflighted_and_projected(tmp_path: Path) -> None:
    invalid = invoke_in_process(["--fields", "status", "doctor"], tmp_path)
    assert invalid.returncode == 1
    assert "Unknown output fields: status" in json.loads(invalid.stderr)["error"]["message"]
    assert not (tmp_path / "state").exists()

    invalid_variant = invoke_in_process(
        ["--fields", "active_pages", "doctor", "--access-only"], tmp_path
    )
    assert invalid_variant.returncode == 1
    assert (
        "Unknown output fields: active_pages"
        in json.loads(invalid_variant.stderr)["error"]["message"]
    )
    assert not (tmp_path / "state").exists()

    result = successful(
        invoke_in_process(["--fields", "status,checks", "doctor", "--access-only"], tmp_path)
    )
    assert result["status"] == "issues"
    assert isinstance(result["checks"], list)
    assert not (tmp_path / "state").exists()


def test_cli_module_import_does_not_load_pymupdf(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        "import sys; import goodnotes_cli.cli; assert 'pymupdf' not in sys.modules",
    ]
    result = subprocess.run(
        command,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_main_fallback_preserves_structured_error_details(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from goodnotes_cli import cli

    def fail(*args: object, **kwargs: object) -> None:
        raise CacheAccessError(
            "cache_invalid",
            "Malformed selected cache.",
            operation="read_test_snapshot",
            path="/synthetic/cache",
        )

    from goodnotes_cli.errors import CacheAccessError

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()

    assert exit_info.value.code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["code"] == "cache_invalid"
    assert error["details"]["operation"] == "read_test_snapshot"
    assert error["details"]["path"] == "/synthetic/cache"


def test_main_keeps_broken_pipe_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    from goodnotes_cli import cli

    def fail(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "app", fail)
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 130


def test_command_broken_pipe_reaches_process_exit_handling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from goodnotes_cli import cli

    def fail(*args: object, **kwargs: object) -> None:
        raise BrokenPipeError

    monkeypatch.setattr(cli, "emit", fail)
    monkeypatch.setattr(sys, "argv", ["goodnotes", "schema"])
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 130


def test_repository_query_sqlite_failure_is_structured_as_cache_error(
    library_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodnotes_cli.library import Library

    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("synthetic query failure")

    monkeypatch.setattr(Library, "list_items", fail)
    result = invoke_in_process(["ls"], tmp_path, sandbox=library_copy)

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cache_invalid"
    assert error["details"]["operation"] == "query_snapshot_library"
    assert error["details"]["path"] == str(library_copy)
    assert error["details"]["remedy"]


def test_document_sqlite_failure_is_not_classified_as_cache_access(
    library_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from goodnotes_cli import documents

    def fail(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("synthetic document failure")

    monkeypatch.setattr(documents, "export_pdf", fail)
    result = invoke_in_process(
        ["export", "doc-hostile", "--output", str(tmp_path / "output.pdf")],
        tmp_path,
        sandbox=library_copy,
    )

    assert result.returncode == 1
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "operation_failed"
    assert "details" not in error


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
        arguments = [
            "bundle",
            "doc-hostile",
            "--layer",
            "background",
            "--source",
            "all",
            "--output",
            str(tmp_path / "bundle"),
        ]

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

    data = successful(invoke_in_process(["schema"], tmp_path))
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
    data = successful(invoke_in_process(["schema", command_name], tmp_path))
    assert data["command"] == command_name
    assert any("--help" in p["flags"] for p in data["options"])
    assert any(p.get("envvar") == "GOODNOTES_DATA_DIR" for p in data["global_options"])
    help_result = invoke_in_process([command_name, "--help"], tmp_path)
    assert help_result.returncode == 0
    assert "Example:" in help_result.stdout
    assert not (tmp_path / "state").exists()


def test_targeted_schema_describes_actual_bounds_and_parameter_kinds(tmp_path: Path) -> None:
    read = successful(invoke_in_process(["schema", "read"], tmp_path))
    budget = next(p for p in read["options"] if p["name"] == "max_chars")
    assert (budget["default"], budget["min"], budget["max"]) == (12000, 1, 1000000)
    assert read["arguments"][0]["name"] == "notebook"
    listing = successful(invoke_in_process(["schema", "ls"], tmp_path))
    assert listing["arguments"] == []
    folder = next(p for p in listing["options"] if p["name"] == "folder")
    assert folder["flags"] == ["--folder"]
    assert folder["default"] == "/"
    capture = successful(invoke_in_process(["schema", "snapshot"], tmp_path))
    assert [p["name"] for p in capture["arguments"]] == ["library", "destination"]
    sync = successful(invoke_in_process(["schema", "sync"], tmp_path))
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
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cache_not_initialized"
    details = error["details"]
    assert {"operation", "path", "remedy"} <= details.keys()
    assert details["path"] == str(tmp_path / "state")
    assert "sync" in details["remedy"].casefold()


@pytest.mark.parametrize(
    ("case", "expected_operation"),
    [
        ("read", "resolve_cache_directory"),
        ("sync", "prepare_cache_directory"),
        ("explicit_sandbox", "resolve_explicit_sandbox"),
    ],
)
def test_symlink_loop_in_cache_ancestor_returns_structured_error_without_mutation(
    library_copy: Path, tmp_path: Path, case: str, expected_operation: str
) -> None:
    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)
    data_dir = loop_a / "cache"
    explicit_sandbox = data_dir / "snapshots" / "generated-snapshot"
    source_before = _tree_manifest(library_copy)
    fixture_before = _top_level_manifest(tmp_path)

    if case == "sync":
        arguments = ["sync", "--library", str(library_copy / "Library")]
        result = invoke(arguments, tmp_path, data_dir=data_dir)
        # The writer reports the requested cache path if parent setup fails.
        expected_path = data_dir
    elif case == "explicit_sandbox":
        arguments = ["ls"]
        result = invoke(arguments, tmp_path, sandbox=explicit_sandbox)
        expected_path = _resolution_error_path(explicit_sandbox, runtime_fallback=explicit_sandbox)
    else:
        arguments = ["ls"]
        result = invoke(arguments, tmp_path, data_dir=data_dir)
        expected_path = _resolution_error_path(data_dir, runtime_fallback=data_dir)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cache_invalid"
    details = error["details"]
    assert details["operation"] == expected_operation
    assert details["path"] == str(expected_path)
    assert {"operation", "path", "remedy"} <= details.keys()
    assert _tree_manifest(library_copy) == source_before
    assert _top_level_manifest(tmp_path) == fixture_before
    assert not (tmp_path / "state").exists()


def test_symlink_loop_in_managed_snapshot_pointer_returns_structured_error(
    library_copy: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    cache_module.refresh_cache(library_copy / "Library", state)
    snapshots = state / "snapshots"
    loop_a, loop_b = snapshots / "cycle-a", snapshots / "cycle-b"
    loop_a.symlink_to(loop_b, target_is_directory=True)
    loop_b.symlink_to(loop_a, target_is_directory=True)
    pointer = state / "active.json"
    data = json.loads(pointer.read_text())
    pointer_target = loop_a / "child"
    data["snapshot"] = str(pointer_target)
    pointer.write_text(json.dumps(data))
    pointer_before = pointer.read_bytes()
    source_before = _tree_manifest(library_copy)
    entries_before = _top_level_manifest(snapshots)

    result = invoke(["ls"], tmp_path)

    assert result.returncode == 1
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    error = json.loads(result.stderr)["error"]
    assert error["code"] == "cache_invalid"
    assert error["details"]["operation"] == "resolve_read_view"
    assert error["details"]["path"] == str(state)
    assert error["details"]["remedy"]
    assert pointer.read_bytes() == pointer_before
    assert _top_level_manifest(snapshots) == entries_before
    assert _tree_manifest(library_copy) == source_before


@pytest.mark.parametrize(
    ("failure", "expected_path"),
    [("runtime", "state"), ("symlink_loop", "failure")],
)
def test_read_view_wraps_runtime_and_os_symlink_resolution_errors(
    library_copy: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
    expected_path: str,
) -> None:
    state = tmp_path / "state"
    cache_module.refresh_cache(library_copy / "Library", state)
    failed_path = state / "snapshots" / "cycle-a"

    def fail_snapshot_resolution(_settings: Settings) -> None:
        if failure == "runtime":
            raise RuntimeError("synthetic symlink resolution loop")
        raise OSError(errno.ELOOP, "Too many levels of symbolic links", str(failed_path))

    monkeypatch.setattr(Settings, "load_sandbox", fail_snapshot_resolution)
    settings = Settings(data_dir=state)

    with pytest.raises(CacheAccessError) as raised:
        with cache_module.read_view(settings):
            pytest.fail("the read view should reject the synthetic resolution failure")

    error = raised.value
    assert error.code == "cache_invalid"
    assert error.details["operation"] == "resolve_read_view"
    assert error.details["path"] == str(state if expected_path == "state" else failed_path)
    assert error.details["remedy"]


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


def test_read_reuses_one_pdf_document_across_selected_pages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sandbox = _create_snapshot(tmp_path / "snapshot", with_fts=False)
    _add_repeated_fallback_pages(sandbox, 3)
    actual_open = pymupdf.open
    opened: list[pymupdf.Document] = []

    def record_pdf_open(path: str | Path) -> pymupdf.Document:
        document = actual_open(path)
        opened.append(document)
        return document

    monkeypatch.setattr(pymupdf, "open", record_pdf_open)
    result = successful(
        invoke_in_process(
            ["read", "doc-hostile", "--pages", "4-6", "--source", "pdf"],
            tmp_path,
            sandbox=sandbox.root,
        )
    )

    assert len(result["pages"]) == 3
    assert len(opened) == 1
    assert opened[0].is_closed


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
            [
                "bundle",
                "doc-hostile",
                "--pages",
                "1-2",
                "--layer",
                "background",
                "--source",
                "all",
                "--output",
                str(tmp_path / "bundle"),
            ],
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


def test_managed_read_commands_work_with_a_readonly_cache(
    library_copy: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    captured = cache_module.refresh_cache(library_copy / "Library", state)
    assert captured["captured_at"] is not None
    source_before = _tree_manifest(library_copy)
    export_path = tmp_path / "outside-cache.pdf"
    commands = [
        ["ls", "--limit", "1"],
        ["tree", "--depth", "2"],
        ["info", "doc-hostile"],
        ["pages", "doc-hostile", "--with-recognition"],
        ["read", "doc-hostile", "--page", "1", "--source", "handwriting"],
        ["search", "generated query", "--notebook", "doc-hostile"],
        ["scan-pdfs", "Synthetic PDF page", "--notebook", "doc-hostile", "--page-limit", "1"],
        ["doctor"],
        ["export", "doc-hostile", "--pages", "1", "--output", str(export_path)],
    ]

    try:
        _set_tree_readonly(state)
        if hasattr(os, "geteuid") and os.geteuid() != 0:
            try:
                writable_lock = os.open(state / cache_module._VIEW_LOCK, os.O_RDWR)
            except PermissionError:
                pass
            else:
                os.close(writable_lock)
                pytest.fail("read-only fixture unexpectedly allowed a writable lock open")
        cache_before = _tree_manifest(state)
        doctor_result: dict | None = None
        for arguments in commands:
            result = invoke(arguments, tmp_path)
            assert result.returncode == 0, f"{arguments}: {result.stderr}"
            assert result.stderr == ""
            assert len(result.stdout.splitlines()) == 1
            data = json.loads(result.stdout)
            if arguments[0] == "doctor":
                doctor_result = data

        assert doctor_result is not None
        storage = doctor_result["storage"]
        assert storage["cache_dir"] == str(state.resolve())
        assert storage["snapshot_path"] == captured["snapshot"]
        assert storage["captured_at"] == captured["captured_at"]
        assert export_path.is_file()
        assert export_path.parent == tmp_path
        assert _tree_manifest(state) == cache_before
        assert _tree_manifest(library_copy) == source_before
    finally:
        _restore_tree_writable(state)


@pytest.mark.parametrize(
    ("target_name", "expected_operation"),
    [
        ("projection.sqlite", "open_snapshot_database"),
        ("fts.sqlite", "open_snapshot_database"),
        ("snapshot-root", "resolve_read_view"),
    ],
)
def test_unreadable_managed_database_has_structured_permission_error(
    library_copy: Path, tmp_path: Path, target_name: str, expected_operation: str
) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("chmod-based unreadable-file checks require a non-root process")

    state = tmp_path / "state"
    result = cache_module.refresh_cache(library_copy / "Library", state)
    snapshot_root = Path(str(result["snapshot"]))
    database_path = snapshot_root / "Library" / "Databases" / target_name
    permission_target = snapshot_root if target_name == "snapshot-root" else database_path
    expected_path = (
        snapshot_root / "goodnotes-snapshot.json"
        if target_name == "snapshot-root"
        else database_path
    )
    if target_name != "snapshot-root":
        assert database_path.is_file()
    lock_fd = os.open(state / cache_module._VIEW_LOCK, os.O_RDONLY)
    os.close(lock_fd)
    source_before = _tree_manifest(library_copy)
    pointer_before = (state / "active.json").read_bytes()
    database_before = hashlib.sha256(
        (snapshot_root / "Library" / "Databases" / "projection.sqlite").read_bytes()
    ).hexdigest()
    original_mode = stat.S_IMODE(permission_target.stat().st_mode)

    try:
        permission_target.chmod(0)
        response = invoke(["doctor"], tmp_path)
        assert response.returncode == 1
        assert response.stdout == ""
        error = json.loads(response.stderr)["error"]
        assert error["code"] == "cache_permission_denied"
        details = error["details"]
        assert details["path"] == str(expected_path)
        assert details["operation"] == expected_operation
        assert {"operation", "path", "remedy"} <= details.keys()
    finally:
        permission_target.chmod(original_mode)

    projection_path = snapshot_root / "Library" / "Databases" / "projection.sqlite"
    assert hashlib.sha256(projection_path.read_bytes()).hexdigest() == database_before
    assert (state / "active.json").read_bytes() == pointer_before
    assert _tree_manifest(library_copy) == source_before


def test_corrupt_managed_projection_has_structured_cache_error(
    library_copy: Path, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    result = cache_module.refresh_cache(library_copy / "Library", state)
    database_path = Path(str(result["snapshot"])) / "Library" / "Databases" / "projection.sqlite"
    database_path.chmod(0o600)
    database_path.write_bytes(b"generated invalid SQLite bytes")
    source_before = _tree_manifest(library_copy)
    pointer_before = (state / "active.json").read_bytes()

    response = invoke(["doctor"], tmp_path)

    assert response.returncode == 1
    assert response.stdout == ""
    error = json.loads(response.stderr)["error"]
    assert error["code"] == "cache_invalid"
    details = error["details"]
    assert details["path"] == str(database_path)
    assert details["operation"] == "read_snapshot_catalog"
    assert {"operation", "path", "remedy"} <= details.keys()
    assert (state / "active.json").read_bytes() == pointer_before
    assert _tree_manifest(library_copy) == source_before


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
                "--layer",
                "background",
                "--source",
                "all",
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
                "--layer",
                "background",
                "--source",
                "all",
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
