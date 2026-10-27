"""Compare CLI contracts and generated-fixture results with a frozen source tree.

Run from the checkout whose project environment is already prepared:

    UV_CACHE_DIR=/private/tmp/goodnotes-architecture-uv \
      uv run --locked --no-sync python tests/architecture/contract_compare.py \
      --baseline /private/tmp/goodnotes-architecture-baseline \
      --candidate . --output /private/tmp/architecture-contracts

Each side runs in subprocesses with only that tree's ``src`` on PYTHONPATH. The
fixture is generated locally and contains no Goodnotes Library data. Before any
capture, the frozen baseline package is checked against the expected Git commit.
The process exit status reports capture completion; semantic differences are
written to ``diff.json`` for review and do not make capture itself fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pymupdf

BASELINE_REVISION = "0dc897713694326f9b00ef200cc77c2aeb601870"
PACKAGE_SOURCE = "src/goodnotes_cli"

TIMESTAMP_KEYS = {
    "captured_at",
    "checked_at",
    "started_at",
    "completed_at",
    "published_at",
    "created_at_utc",
}
SOURCE_FINGERPRINT_KEYS = {"source_sha256", "source_fingerprint", "source_hash"}
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d")
VERSION_SOURCE = re.compile(r"\(source [0-9a-f]{12}\)")
SNAPSHOT_PATH = re.compile(r"snapshots/\d{8}T\d{6}Z-[0-9a-f]+")


def _git_output(repo_root: Path, *arguments: str) -> bytes:
    try:
        result = subprocess.run(
            ["git", *arguments],
            cwd=repo_root,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise ValueError(f"Cannot run Git in candidate repository {repo_root}: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip()
        raise ValueError(f"Git command failed in {repo_root}: {' '.join(arguments)}: {detail}")
    return result.stdout


def _verify_baseline_package(
    baseline_root: Path,
    candidate_root: Path,
    expected_revision: str = BASELINE_REVISION,
) -> dict[str, Any]:
    """Require the baseline's Python package files to match the expected Git tree."""
    baseline_root = baseline_root.resolve()
    candidate_root = candidate_root.resolve()
    if not baseline_root.is_dir():
        raise ValueError(f"Frozen baseline directory does not exist: {baseline_root}")
    if not (candidate_root / ".git").exists():
        raise ValueError(f"Candidate is not a Git checkout: {candidate_root}")

    revision = (
        _git_output(candidate_root, "rev-parse", "--verify", f"{expected_revision}^{{commit}}")
        .decode("ascii", "strict")
        .strip()
    )
    if revision != expected_revision:
        raise ValueError(
            f"Expected baseline revision {expected_revision}, but Git resolved {revision}."
        )

    tree = _git_output(
        candidate_root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        revision,
        "--",
        PACKAGE_SOURCE,
    )
    expected_paths = sorted(
        entry.decode("utf-8", "strict") for entry in tree.split(b"\0") if entry.endswith(b".py")
    )
    if not expected_paths:
        raise ValueError(
            f"Git revision {revision} contains no Python files under {PACKAGE_SOURCE}."
        )

    package_root = baseline_root / PACKAGE_SOURCE
    if not package_root.is_dir():
        raise ValueError(f"Frozen baseline is missing package source directory: {package_root}")
    actual_files = sorted(
        path.relative_to(baseline_root).as_posix()
        for path in package_root.rglob("*.py")
        if "__pycache__" not in path.parts and path.suffix == ".py"
    )
    if actual_files != expected_paths:
        missing = sorted(set(expected_paths) - set(actual_files))
        unexpected = sorted(set(actual_files) - set(expected_paths))
        raise ValueError(
            "Frozen baseline Python package pathset does not match Git revision "
            f"{revision}; missing={missing}, unexpected={unexpected}."
        )

    digest = hashlib.sha256()
    for relative_path in expected_paths:
        baseline_file = baseline_root / relative_path
        if baseline_file.is_symlink() or not baseline_file.is_file():
            raise ValueError(f"Frozen baseline package file is not a regular file: {relative_path}")
        expected_bytes = _git_output(
            candidate_root, "cat-file", "blob", f"{revision}:{relative_path}"
        )
        actual_bytes = baseline_file.read_bytes()
        if actual_bytes != expected_bytes:
            raise ValueError(
                f"Frozen baseline package content differs from Git at {relative_path}."
            )
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(actual_bytes)
        digest.update(b"\0")

    return {
        "revision": revision,
        "package_path": PACKAGE_SOURCE,
        "package_python_file_count": len(expected_paths),
        "package_python_sha256": digest.hexdigest(),
    }


def _create_fixture(root: Path) -> tuple[Path, Path, Path]:
    source = root / "fixture" / "Library"
    databases = source / "Databases"
    attachments = source / "Attachments"
    databases.mkdir(parents=True)
    attachments.mkdir()

    projection = databases / "projection.sqlite"
    with sqlite3.connect(projection) as db:
        db.executescript(
            """
            CREATE TABLE folders (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL,
                deleted INTEGER DEFAULT 0, position TEXT
            );
            CREATE TABLE documents (
                id TEXT PRIMARY KEY, name TEXT NOT NULL, updated_at REAL,
                deleted INTEGER DEFAULT 0, position TEXT
            );
            CREATE TABLE folder_to_folder_items (
                id TEXT PRIMARY KEY, item_type INTEGER, item_id TEXT,
                parent_folder_id TEXT, root_folder_id TEXT, item_position TEXT,
                deleted INTEGER DEFAULT 0
            );
            CREATE TABLE pages (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, template_id TEXT,
                position TEXT NOT NULL, group_id TEXT, deleted INTEGER DEFAULT 0
            );
            CREATE TABLE templates (
                id TEXT PRIMARY KEY, document_id TEXT NOT NULL, attachment_id TEXT,
                page_number INTEGER, size_width REAL, size_height REAL
            );
            CREATE TABLE attachments (
                id TEXT PRIMARY KEY, path TEXT, document_id TEXT, available INTEGER
            );
            CREATE TABLE notes_items (
                id TEXT PRIMARY KEY, group_id TEXT NOT NULL, deleted INTEGER DEFAULT 0
            );
            """
        )
        db.executemany(
            "INSERT INTO folders VALUES (?, ?, ?, ?, ?)",
            [("folder-a", "Generated Course", 1, 0, "a"), ("folder-b", "Other Course", 2, 0, "b")],
        )
        db.executemany(
            "INSERT INTO documents VALUES (?, ?, ?, ?, ?)",
            [
                ("doc-main", "Generated Notes", 3, 0, "a"),
                ("doc-alt", "Generated Notes", 4, 0, "b"),
                ("doc-root", "Root Notes", 5, 0, "c"),
            ],
        )
        db.executemany(
            "INSERT INTO folder_to_folder_items VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                ("link-a", 0, "folder-a", "", "generated-root", "a", 0),
                ("link-b", 0, "folder-b", "", "generated-root", "b", 0),
                ("link-main", 1, "doc-main", "folder-a", "generated-root", "a", 0),
                ("link-alt", 1, "doc-alt", "folder-b", "generated-root", "a", 0),
                ("link-root", 1, "doc-root", "", "generated-root", "c", 0),
            ],
        )
        page_rows = [
            ("page-02", "doc-main", "template-02", "02", "group-02", 0),
            ("page-10", "doc-main", "template-10", "10", "group-10", 0),
            ("page-30", "doc-main", "template-30", "30", None, 0),
        ]
        db.executemany("INSERT INTO pages VALUES (?, ?, ?, ?, ?, ?)", page_rows)
        db.executemany(
            "INSERT INTO templates VALUES (?, ?, ?, ?, ?, ?)",
            [
                (f"template-{position}", "doc-main", "attachment-generated", page, 240, 180)
                for position, page in (("02", 1), ("10", 2), ("30", 3))
            ],
        )
        db.execute(
            "INSERT INTO attachments VALUES (?, ?, ?, ?)",
            ("attachment-generated", "generated.pdf", "doc-main", 1),
        )
        db.execute("INSERT INTO notes_items VALUES ('note-02', 'group-02', 0)")

    fts = databases / "fts.sqlite"
    with sqlite3.connect(fts) as db:
        db.executescript(
            """
            CREATE TABLE annotation_text_content (
                text_id INTEGER PRIMARY KEY, page_id TEXT, text TEXT,
                engine_type TEXT DEFAULT 'myscript', priority TEXT DEFAULT 'primary'
            );
            CREATE TABLE textbox_text_content (
                text_id INTEGER PRIMARY KEY, page_id TEXT, text TEXT
            );
            CREATE TABLE pdf_text_content (
                text_id INTEGER PRIMARY KEY, text TEXT, attachment_id TEXT,
                pdf_page_no INTEGER, document_id TEXT
            );
            CREATE TABLE page_meta (
                page_id TEXT PRIMARY KEY, document_id TEXT, page_position TEXT,
                pdf_text_id INTEGER, template_attachment_id_and_page_no TEXT,
                is_doc_deleted INTEGER, is_page_deleted INTEGER
            );
            CREATE TABLE template_meta (
                template_id TEXT PRIMARY KEY, document_id TEXT,
                attachment_id TEXT, pdf_page_no INTEGER
            );
            """
        )
        db.executemany(
            "INSERT INTO page_meta VALUES (?, ?, ?, ?, ?, 0, 0)",
            [
                (page_id, "doc-main", position, text_id, f"attachment-generated_{pdf_page}")
                for page_id, position, text_id, pdf_page in (
                    ("page-02", "02", 101, 1),
                    ("page-10", "10", 102, 2),
                    ("page-30", "30", 103, 3),
                )
            ],
        )
        db.executemany(
            "INSERT INTO pdf_text_content VALUES (?, ?, ?, ?, ?)",
            [
                (101, "generated indexed phrase", "attachment-generated", 1, "doc-main"),
                (102, "second indexed phrase", "attachment-generated", 2, "doc-main"),
                (103, "third indexed phrase", "attachment-generated", 3, "doc-main"),
            ],
        )
        db.execute(
            "INSERT INTO annotation_text_content VALUES "
            "(1, 'page-02', 'generated handwriting', 'myscript', 'primary')"
        )
        db.execute(
            "INSERT INTO textbox_text_content VALUES (1, 'page-02', 'generated typed phrase')"
        )

    pdf_path = attachments / "generated.pdf"
    with pymupdf.open() as pdf:
        for text in (
            "Generated indexed phrase",
            "Generated scanned page phrase",
            "Generated third page phrase",
        ):
            page = pdf.new_page(width=240, height=180)
            page.insert_text((18, 32), text)
        pdf.save(pdf_path)

    sandbox = root / "sandbox"
    (sandbox / "Library").parent.mkdir(parents=True)
    shutil.copytree(source, sandbox / "Library")
    (sandbox / "goodnotes-snapshot.json").write_text(
        json.dumps({"format_version": 1}), encoding="utf-8"
    )
    return source, sandbox, pdf_path


def _normalise(value: Any, root: Path, output: Path) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if key in SOURCE_FINGERPRINT_KEYS and isinstance(child, str):
                result[key] = "<SOURCE_FINGERPRINT>"
            elif key in TIMESTAMP_KEYS and isinstance(child, str) and TIMESTAMP.match(child):
                result[key] = "<GENERATED_TIME>"
            elif key in {"stat_key", "cache_stat_key"} and isinstance(child, list):
                result[key] = "<GENERATED_FILE_STAT>"
            elif key == "capture_seconds" and isinstance(child, (float, int)):
                result[key] = "<GENERATED_DURATION>"
            elif key in {"cache_signature", "source_signature"} and isinstance(child, str):
                result[key] = "<GENERATED_SOURCE_SIGNATURE>"
            elif key in {"snapshot_id", "generation_id"} and isinstance(child, str):
                result[key] = "<GENERATED_SNAPSHOT_ID>"
            else:
                result[key] = _normalise(child, root, output)
        return result
    if isinstance(value, list):
        return [_normalise(child, root, output) for child in value]
    if isinstance(value, str):
        text = VERSION_SOURCE.sub("(source <SOURCE_FINGERPRINT>)", value)
        text = SNAPSHOT_PATH.sub("snapshots/<GENERATED_SNAPSHOT_ID>", text)
        for path in sorted((root, output), key=lambda item: len(str(item)), reverse=True):
            text = text.replace(str(path), "<RUN_ROOT>")
        return text
    return value


def _parse_output(text: str) -> Any:
    lines = text.splitlines()
    if not lines:
        return ""
    try:
        values = [json.loads(line) for line in lines]
    except json.JSONDecodeError:
        return text
    return values[0] if len(values) == 1 else values


def _file_manifest(paths: list[Path], root: Path, output: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for path in paths:
        targets = [path]
        if path.is_dir():
            targets = [path, *sorted(path.rglob("*"))]
        for target in targets:
            if not target.exists():
                relative = target.relative_to(root) if target.is_relative_to(root) else target
                entries.append(
                    {
                        "path": _normalise(str(relative), root, output),
                        "exists": False,
                    }
                )
                continue
            relative = target.relative_to(root) if target.is_relative_to(root) else target
            record: dict[str, Any] = {
                "path": _normalise(str(relative), root, output),
                "kind": "directory" if target.is_dir() else "file",
            }
            if target.is_file():
                data = target.read_bytes()
                if target.suffix == ".json":
                    try:
                        record["json"] = _normalise(json.loads(data), root, output)
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        record["sha256"] = hashlib.sha256(data).hexdigest()
                    else:
                        pass
                elif target.suffix.lower() == ".pdf":
                    with pymupdf.open(target) as pdf:
                        record["pdf"] = {
                            "page_count": pdf.page_count,
                            "pages": [
                                {
                                    "text": page.get_text(),
                                    "rect": [round(value, 4) for value in page.rect],
                                }
                                for page in pdf
                            ],
                        }
                else:
                    record.update({"size": len(data), "sha256": hashlib.sha256(data).hexdigest()})
            entries.append(record)
    return entries


def _run_case(
    *,
    root: Path,
    output: Path,
    run_root: Path,
    sandbox: Path,
    source: Path,
    name: str,
    arguments: list[str],
    verbose: bool = False,
    fields: str | None = None,
    text_mode: bool = False,
    json_mode: bool = False,
    expect_snapshot: str | None = None,
    data_group: str | None = None,
    files: list[Path] | None = None,
) -> dict[str, Any]:
    data_dir = run_root / "state" / (data_group or name.replace("/", "-"))
    data_dir.parent.mkdir(parents=True, exist_ok=True)
    environment = dict(os.environ)
    environment.pop("GOODNOTES_SANDBOX", None)
    environment["GOODNOTES_DATA_DIR"] = str(data_dir)
    environment["GOODNOTES_SCRATCH_DIR"] = str(run_root / "scratch")
    environment["PYTHONPATH"] = str(root / "src")
    environment["PYTHONNOUSERSITE"] = "1"
    (run_root / "scratch").mkdir(parents=True, exist_ok=True)

    global_options: list[str] = []
    if "sync" not in arguments and "watch" not in arguments and "snapshot" not in arguments:
        global_options.extend(["--sandbox", str(sandbox)])
    if verbose:
        global_options.append("--verbose")
    if text_mode:
        global_options.append("--text")
    if json_mode:
        global_options.append("--json")
    if expect_snapshot is not None:
        global_options.extend(["--expect-snapshot", expect_snapshot])
    if fields is not None:
        global_options.extend(["--fields", fields])
    command = [sys.executable, "-m", "goodnotes_cli", *global_options, *arguments]
    source_before = _hash_tree(source)
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        stdout = _parse_output(completed.stdout)
        stderr = _parse_output(completed.stderr)
        source_unchanged = source_before == _hash_tree(source)
        return {
            "command": _normalise(arguments, run_root, output),
            "verbose": verbose,
            "fields": fields,
            "exit_code": completed.returncode,
            "stdout": _normalise(stdout, run_root, output),
            "stderr": _normalise(stderr, run_root, output),
            "data_dir": _file_manifest([data_dir], run_root, output),
            "files": _file_manifest(files or [], run_root, output),
            "source_unchanged": source_unchanged,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": _normalise(arguments, run_root, output),
            "verbose": verbose,
            "fields": fields,
            "timeout": True,
            "stdout": _normalise(exc.stdout or "", run_root, output),
            "stderr": _normalise(exc.stderr or "", run_root, output),
        }


def _hash_tree(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(root)).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _field_selection(schema: dict[str, Any]) -> str | None:
    contract = schema.get("output")
    if not isinstance(contract, dict):
        return None
    fields = contract.get("fields")
    if not isinstance(fields, list) or len(fields) < 1:
        defaults = contract.get("default_output")
        fields = defaults.get("fields") if isinstance(defaults, dict) else None
    if not isinstance(fields, list) or not fields:
        return None
    return ",".join(str(field) for field in fields[:3])


def _run_side(
    root: Path,
    label: str,
    output: Path,
    fixture_root: Path,
    capture_id: str,
    source: Path,
    base_sandbox: Path,
    source_pdf: Path,
    baseline_provenance: dict[str, Any],
) -> dict[str, Any]:
    root = root.resolve()
    run_root = output / "runs" / capture_id / label
    run_root.mkdir(parents=True)
    side_sandbox = run_root / "sandbox"
    shutil.copytree(base_sandbox, side_sandbox)
    sandbox = side_sandbox.parent / "sandbox"

    records: dict[str, Any] = {}

    def record(name: str, arguments: list[str], **kwargs: Any) -> None:
        records[name] = _run_case(
            root=root,
            output=output,
            run_root=run_root,
            sandbox=sandbox,
            source=source,
            name=name,
            arguments=arguments,
            **kwargs,
        )

    # Public parser, help, schema and version surface, including hidden commands.
    record("help", ["--help"])
    record("version", ["--version"])
    record("schema-index", ["schema"])
    index_result = records["schema-index"]["stdout"]
    command_names: list[str] = []
    if isinstance(index_result, dict):
        command_names = list(index_result.get("commands", {})) + list(
            index_result.get("advanced_commands", {})
        )
    schemas: dict[str, dict[str, Any]] = {}
    for command_name in command_names:
        record(f"schema/{command_name}", ["schema", command_name])
        record(f"help/{command_name}", [command_name, "--help"])
        record(f"invalid-flag/{command_name}", [command_name, "--architecture-invalid-flag"])
        schema_value = records[f"schema/{command_name}"]["stdout"]
        if isinstance(schema_value, dict):
            schemas[command_name] = schema_value

    # Successful generated-snapshot journeys, each in compact and verbose modes.
    operations: list[tuple[str, list[str], list[Path] | None, str | None]] = [
        ("ls", ["ls"], None, "/"),
        ("tree", ["tree"], None, "/"),
        ("info", ["info", "Generated Course/Generated Notes"], None, None),
        ("pages", ["pages", "doc-main"], None, None),
        ("read", ["read", "doc-main", "--pages", "1-3", "--source", "all"], None, None),
        ("search", ["search", "generated indexed phrase", "--source", "all"], None, None),
        ("scan-pdfs", ["scan-pdfs", "scanned page phrase", "--notebook", "doc-main"], None, None),
        ("doctor", ["doctor"], None, None),
    ]
    for command_name, arguments, _files, _unused in operations:
        record(f"success/{command_name}/compact", arguments)
        record(f"success/{command_name}/verbose", arguments, verbose=True)
        field_selection = _field_selection(schemas.get(command_name, {}))
        if field_selection:
            record(f"success/{command_name}/fields", arguments, fields=field_selection)

    record("success/ls/offset", ["ls", "--limit", "1", "--offset", "1"])
    record("success/pages/offset", ["pages", "doc-main", "--limit", "1", "--offset", "1"])
    record(
        "success/read/truncated",
        ["read", "doc-main", "--pages", "1-3", "--source", "all", "--max-chars", "18"],
    )
    record(
        "success/search/offset",
        [
            "search",
            "phrase",
            "--notebook",
            "doc-main",
            "--source",
            "all",
            "--limit",
            "1",
            "--offset",
            "1",
        ],
    )
    record(
        "success/scan-pdfs/offset",
        [
            "scan-pdfs",
            "phrase",
            "--notebook",
            "doc-main",
            "--page-limit",
            "1",
            "--page-offset",
            "1",
        ],
    )

    record("success/ls/text", ["ls"], text_mode=True)
    record("success/ls/json", ["ls"], json_mode=True)
    record("success/ls/expected-snapshot", ["ls"], expect_snapshot=sandbox.name)

    access_args = ["doctor", "--access-only"]
    record("success/doctor-access/compact", access_args)
    record("success/doctor-access/verbose", access_args, verbose=True)
    record(
        "success/doctor-access/fields",
        access_args,
        fields="scope,status,warnings",
    )
    record(
        "error/doctor-conflicting-mode",
        ["doctor", "--access-only", "--notebook", "doc-main"],
    )
    record(
        "error/doctor-conflicting-mode-with-fields",
        ["doctor", "--access-only", "--notebook", "doc-main"],
        fields="status,scope",
    )
    record(
        "error/doctor-conflicting-mode-with-normal-field",
        ["doctor", "--access-only", "--notebook", "doc-main"],
        fields="active_pages",
    )

    # The checked-in synthetic PDF exercises file and manifest publication paths.
    io_root = run_root / "outputs"
    io_root.mkdir()
    for label_suffix, is_verbose, field_mode in (
        ("compact", False, False),
        ("verbose", True, False),
        ("fields", False, True),
    ):
        render_fields = _field_selection(schemas.get("render", {})) if field_mode else None
        png = io_root / f"render-{label_suffix}.png"
        record(
            f"success/render/{label_suffix}",
            ["render", "doc-main", "--page", "2", "--layer", "background", "--output", str(png)],
            verbose=is_verbose,
            fields=render_fields,
            files=[png],
        )
        export_fields = _field_selection(schemas.get("export", {})) if field_mode else None
        pdf = io_root / f"export-{label_suffix}.pdf"
        record(
            f"success/export/{label_suffix}",
            ["export", "doc-main", "--pages", "3,1", "--layer", "background", "--output", str(pdf)],
            verbose=is_verbose,
            fields=export_fields,
            files=[pdf],
        )
        bundle_fields = _field_selection(schemas.get("bundle", {})) if field_mode else None
        bundle = io_root / f"bundle-{label_suffix}"
        record(
            f"success/bundle/{label_suffix}",
            [
                "bundle",
                "doc-main",
                "--pages",
                "1,3",
                "--layer",
                "background",
                "--output",
                str(bundle),
            ],
            verbose=is_verbose,
            fields=bundle_fields,
            files=[bundle],
        )

    bundle_document_id_path = io_root / "bundle-document-id"
    record(
        "accepted-change/F13-bundle-fields-document-id",
        [
            "bundle",
            "doc-main",
            "--pages",
            "1",
            "--layer",
            "background",
            "--output",
            str(bundle_document_id_path),
        ],
        fields="document_id",
        files=[bundle_document_id_path],
    )
    truncated_bundle = io_root / "bundle-truncated"
    record(
        "success/bundle/truncated",
        [
            "bundle",
            "doc-main",
            "--pages",
            "1,2,3",
            "--layer",
            "background",
            "--max-chars",
            "18",
            "--output",
            str(truncated_bundle),
        ],
        files=[truncated_bundle],
    )

    record("success/sync/compact", ["sync", "--library", str(source)], data_group="sync")
    record(
        "success/sync/verbose",
        ["sync", "--library", str(source)],
        verbose=True,
        data_group="sync",
    )
    record(
        "success/sync/fields",
        ["sync", "--library", str(source)],
        fields=_field_selection(schemas.get("sync", {})),
        data_group="sync",
    )
    record(
        "success/watch/compact",
        [
            "watch",
            "--library",
            str(source),
            "--interval",
            "0.05",
            "--debounce",
            "0",
            "--iterations",
            "1",
        ],
        data_group="watch",
    )
    record(
        "success/watch/verbose",
        [
            "watch",
            "--library",
            str(source),
            "--interval",
            "0.05",
            "--debounce",
            "0",
            "--iterations",
            "1",
        ],
        verbose=True,
        data_group="watch",
    )
    record(
        "success/watch/fields",
        [
            "watch",
            "--library",
            str(source),
            "--interval",
            "0.05",
            "--debounce",
            "0",
            "--iterations",
            "1",
        ],
        fields=_field_selection(schemas.get("watch", {})),
        data_group="watch",
    )
    for mode, is_verbose, field_mode in (
        ("compact", False, False),
        ("verbose", True, False),
        ("fields", False, True),
    ):
        snapshot_output = io_root / f"snapshot-copy-{mode}"
        record(
            f"success/snapshot/{mode}",
            ["snapshot", str(source), str(snapshot_output)],
            verbose=is_verbose,
            fields=_field_selection(schemas.get("snapshot", {})) if field_mode else None,
            files=[snapshot_output],
        )
    annotated_pdf = io_root / "registered.pdf"
    shutil.copyfile(source_pdf, annotated_pdf)
    for mode, is_verbose, field_mode in (
        ("compact", False, False),
        ("verbose", True, False),
        ("fields", False, True),
    ):
        record(
            f"success/attach-pdf/{mode}",
            ["attach-pdf", "doc-main", str(annotated_pdf)],
            verbose=is_verbose,
            fields=_field_selection(schemas.get("attach-pdf", {})) if field_mode else None,
            files=[sandbox],
        )

    # Expected failures retain exit status, structured error details and stderr shape.
    errors = [
        ("unknown-command", ["not-a-goodnotes-command"]),
        ("invalid-kind", ["ls", "--kind", "invalid"]),
        ("ambiguous-selector", ["info", "Generated Notes"]),
        ("bad-page", ["read", "doc-main", "--page", "0"]),
        ("conflicting-page-options", ["read", "doc-main", "--page", "1", "--pages", "1"]),
        ("bad-source", ["read", "doc-main", "--source", "invalid"]),
        (
            "conflicting-search-scope",
            ["search", "phrase", "--folder", "/", "--notebook", "doc-main"],
        ),
        ("out-of-range-render", ["render", "doc-main", "--page", "99", "--layer", "background"]),
        (
            "bad-export-layer",
            [
                "export",
                "doc-main",
                "--layer",
                "invalid",
                "--output",
                str(io_root / "bad-layer.pdf"),
            ],
        ),
        ("out-of-range-bundle", ["bundle", "doc-main", "--pages", "99", "--layer", "background"]),
        ("doctor-library-without-access-only", ["doctor", "--library", str(source)]),
        ("sync-with-sandbox", ["--sandbox", str(sandbox), "sync", "--library", str(source)]),
    ]
    for name, arguments in errors:
        if arguments[0] == "--sandbox":
            # The general invoker adds sandbox before all non-mutating commands;
            # keep this dedicated conflict invocation free of duplicate options.
            env = dict(os.environ)
            env.pop("GOODNOTES_SANDBOX", None)
            env["GOODNOTES_DATA_DIR"] = str(run_root / "state" / name)
            env["PYTHONPATH"] = str(root / "src")
            completed = subprocess.run(
                [sys.executable, "-m", "goodnotes_cli", *arguments],
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=45,
                check=False,
            )
            records[f"error/{name}"] = {
                "command": _normalise(arguments, run_root, output),
                "exit_code": completed.returncode,
                "stdout": _normalise(_parse_output(completed.stdout), run_root, output),
                "stderr": _normalise(_parse_output(completed.stderr), run_root, output),
            }
        else:
            record(f"error/{name}", arguments)

    return {
        "root_label": label,
        "verified_baseline": baseline_provenance,
        "commands": command_names,
        "records": records,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--capture",
        choices=("baseline", "candidate", "both"),
        default="both",
        help=(
            "Capture one side now, or both sides and compare them. Exit status reports capture "
            "completion; semantic differences are written for review."
        ),
    )
    args = parser.parse_args()
    try:
        baseline_provenance = _verify_baseline_package(args.baseline, args.candidate)
    except ValueError as exc:
        parser.error(f"baseline provenance verification failed: {exc}")

    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    capture_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    fixture_root = output / "fixtures" / capture_id
    fixture_root.mkdir(parents=True)
    source, base_sandbox, source_pdf = _create_fixture(fixture_root)

    results: dict[str, dict[str, Any]] = {}
    labels = ("baseline", "candidate") if args.capture == "both" else (args.capture,)
    roots = {"baseline": args.baseline, "candidate": args.candidate}
    for label in labels:
        results[label] = _run_side(
            roots[label],
            label,
            output,
            fixture_root,
            capture_id,
            source,
            base_sandbox,
            source_pdf,
            baseline_provenance,
        )
        receipt_path = (
            output / f"{label}.json"
            if args.capture == "both"
            else output / f"{label}-{capture_id}.json"
        )
        receipt_path.write_text(
            json.dumps(results[label], ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.capture != "both":
        summary = {
            "capture_complete": True,
            "captured": list(labels),
            "comparison_state": "not_compared",
            "exit_status_policy": "capture-completion-only",
            "verified_baseline": baseline_provenance,
            "receipt": str(receipt_path),
        }
        summary_path = output / f"capture-{capture_id}.json"
        summary_path.write_text(
            json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(summary, sort_keys=True))
        return 0
    baseline_records = results["baseline"]["records"]
    candidate_records = results["candidate"]["records"]
    keys = sorted(set(baseline_records) | set(candidate_records))
    differences = [
        {
            "case": key,
            "baseline_present": key in baseline_records,
            "candidate_present": key in candidate_records,
            "baseline": baseline_records.get(key),
            "candidate": candidate_records.get(key),
        }
        for key in keys
        if baseline_records.get(key) != candidate_records.get(key)
    ]
    summary = {
        "capture_complete": True,
        "comparison_state": "equivalent" if not differences else "differences_require_review",
        "exit_status_policy": "capture-completion-only",
        "verified_baseline": baseline_provenance,
        "baseline_command_count": len(results["baseline"]["commands"]),
        "candidate_command_count": len(results["candidate"]["commands"]),
        "baseline_case_count": len(baseline_records),
        "candidate_case_count": len(candidate_records),
        "difference_count": len(differences),
        "differences": differences,
    }
    (output / "diff.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {key: value for key, value in summary.items() if key != "differences"}, sort_keys=True
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
