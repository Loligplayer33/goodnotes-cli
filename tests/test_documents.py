from __future__ import annotations

import json
import multiprocessing
import os
import sqlite3
import struct
import zlib
from pathlib import Path
from typing import Any

import pymupdf as fitz
import pytest

from goodnotes_cli.documents import export_pdf, register_annotated_pdf, render_page
from goodnotes_cli.errors import FilesystemAccessError, GoodnotesError
from goodnotes_cli.models import Page
from goodnotes_cli.safety import Sandbox


class FakeSandbox:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.library_dir = root / "Library"
        self.attachments_dir = self.library_dir / "Attachments"
        self.projection_path = self.library_dir / "Databases" / "projection.sqlite"
        self.outputs_dir = root.parent / "outputs"
        self.root.mkdir(parents=True)
        self.attachments_dir.mkdir(parents=True)
        self.projection_path.parent.mkdir(parents=True)
        self.outputs_dir.mkdir()
        with sqlite3.connect(self.projection_path) as connection:
            connection.execute(
                "CREATE TABLE pages (id TEXT, document_id TEXT, deleted INTEGER DEFAULT 0)"
            )

    def add_notebook(self, document_id: str, page_count: int) -> None:
        with sqlite3.connect(self.projection_path) as connection:
            connection.executemany(
                "INSERT INTO pages(id, document_id, deleted) VALUES (?, ?, 0)",
                [(f"{document_id}-{number}", document_id) for number in range(1, page_count + 1)],
            )

    def ensure_output(self, path: Path) -> Path:
        candidate = path if path.is_absolute() else self.outputs_dir / path
        candidate = candidate.resolve()
        if candidate.is_relative_to(self.root.resolve()):
            raise GoodnotesError("Output paths must be outside the snapshot.")
        if candidate.exists():
            raise GoodnotesError(f"Output file already exists: {candidate}")
        candidate.parent.mkdir(parents=True, exist_ok=True)
        return candidate


def _make_pdf(path: Path, labels: list[str], *, width: float = 612, height: float = 792) -> None:
    document = fitz.open()
    try:
        for label in labels:
            page = document.new_page(width=width, height=height)
            page.insert_text((72, 100), label, fontsize=20)
        document.save(path)
    finally:
        document.close()


def _make_repeated_image_pdf(path: Path) -> None:
    width = 256
    height = 256
    state = 0x12345678
    rows = []
    for _ in range(height):
        row = bytearray()
        for _ in range(width * 3):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            row.append(state >> 15 & 0xFF)
        rows.append(b"\x00" + row)

    def png_chunk(kind: bytes, data: bytes) -> bytes:
        checksum = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    image = (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", header)
        + png_chunk(b"IDAT", zlib.compress(b"".join(rows)))
        + png_chunk(b"IEND", b"")
    )
    document = fitz.open()
    try:
        page = document.new_page(width=612, height=792)
        page.insert_image(fitz.Rect(72, 144, 540, 612), stream=image)
        page.insert_text((72, 100), "repeated image fixture", fontsize=18)
        document.save(path)
    finally:
        document.close()


def _page(
    page_id: str,
    number: int,
    attachment_name: str | None,
    *,
    document_id: str = "notebook-1",
    pdf_page: int | None = 1,
) -> Page:
    return Page(
        id=page_id,
        document_id=document_id,
        number=number,
        position=f"{number:02}",
        attachment_path=f"attachments/{attachment_name}" if attachment_name else None,
        pdf_page=pdf_page,
        width=612,
        height=792,
    )


def _make_real_snapshot(root: Path, notebooks: dict[str, int]) -> None:
    library = root / "Library"
    databases = library / "Databases"
    (library / "Attachments").mkdir(parents=True)
    databases.mkdir()
    (root / "goodnotes-snapshot.json").write_text('{"format_version": 1}\n')
    with sqlite3.connect(databases / "projection.sqlite") as connection:
        connection.execute(
            "CREATE TABLE pages (id TEXT, document_id TEXT, deleted INTEGER DEFAULT 0)"
        )
        for document_id, page_count in notebooks.items():
            connection.executemany(
                "INSERT INTO pages(id, document_id, deleted) VALUES (?, ?, 0)",
                [(f"{document_id}-{number}", document_id) for number in range(1, page_count + 1)],
            )


def _register_pdf_in_process(
    snapshot_root: str, document_id: str, pdf_path: str, results: Any
) -> None:
    try:
        register_annotated_pdf(
            Sandbox.load(snapshot_root), document_id, Path(pdf_path), expected_pages=1
        )
    except Exception as exc:
        results.put((document_id, False, str(exc)))
    else:
        results.put((document_id, True, ""))


def test_export_orders_pages_and_uses_each_pages_attachment(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 2)
    _make_pdf(sandbox.attachments_dir / "first.pdf", ["first background"])
    _make_pdf(sandbox.attachments_dir / "second.pdf", ["second background"])

    result = export_pdf(
        sandbox,
        [
            _page("page-2", 2, "second.pdf"),
            _page("page-1", 1, "first.pdf"),
        ],
        Path("ordered.pdf"),
    )

    with fitz.open(result["path"]) as exported:
        assert exported.page_count == 2
        assert "first background" in exported[0].get_text()
        assert "second background" in exported[1].get_text()
    assert result["pages"] == 2
    assert result["selected_pages"] == [1, 2]
    assert result["includes_handwriting"] is False
    assert result["layer"] == "background"
    assert result["warnings"]


def test_missing_annotated_registration_does_not_create_sidecar(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "snapshot")
    sandbox.add_notebook("generated-notebook", 1)
    _make_pdf(sandbox.attachments_dir / "background.pdf", ["Generated background"])
    page = _page("generated-page", 1, "background.pdf", document_id="generated-notebook")
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    with pytest.raises(GoodnotesError, match="No annotated PDF is registered"):
        render_page(sandbox, page, Path("missing.png"), layer="annotated")
    assert {path.relative_to(tmp_path) for path in tmp_path.rglob("*")} == before


def test_registry_stat_permission_failure_has_details(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from goodnotes_cli.documents import _read_registry

    path = tmp_path / "denied.json"
    original_lstat = Path.lstat

    def denied(target: Path) -> os.stat_result:
        if target == path:
            raise PermissionError(13, "denied by generated fixture", str(target))
        return original_lstat(target)

    monkeypatch.setattr(Path, "lstat", denied)
    with pytest.raises(FilesystemAccessError) as captured:
        _read_registry(path, "generated-notebook")
    assert captured.value.code == "source_permission_denied"
    assert captured.value.details["operation"] == "read_annotated_pdf_record"
    assert captured.value.details["path"] == str(path)


def test_registry_parent_permission_failure_has_details(tmp_path: Path) -> None:
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        pytest.skip("A root process can traverse despite directory mode bits")
    sandbox = FakeSandbox(tmp_path / "snapshot")
    sandbox.add_notebook("generated-notebook", 1)
    parent = tmp_path / ".goodnotes-cli"
    parent.mkdir(mode=0o700)
    parent.chmod(0)
    try:
        with pytest.raises(FilesystemAccessError) as captured:
            render_page(
                sandbox,
                _page("generated-page", 1, None, document_id="generated-notebook"),
                Path("denied.png"),
                layer="annotated",
            )
    finally:
        parent.chmod(0o700)
    assert captured.value.code == "source_permission_denied"
    assert captured.value.details["operation"] == "read_annotated_pdf_record"
    assert Path(captured.value.details["path"]).is_relative_to(parent)
    assert list(parent.iterdir()) == []


def test_export_selects_reversed_noncontiguous_pages_from_mixed_attachments(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 5)
    _make_pdf(sandbox.attachments_dir / "second-page.pdf", ["notebook page two"])
    _make_pdf(sandbox.attachments_dir / "fifth-page.pdf", ["notebook page five"])
    pages = [
        _page(f"page-{number}", number, attachment)
        for number, attachment in (
            (1, None),
            (2, "second-page.pdf"),
            (3, None),
            (4, None),
            (5, "fifth-page.pdf"),
        )
    ]

    result = export_pdf(
        sandbox,
        pages,
        Path("selected.pdf"),
        page_numbers=[5, 2],
    )

    with fitz.open(result["path"]) as exported:
        assert exported.page_count == 2
        assert "notebook page five" in exported[0].get_text()
        assert "notebook page two" in exported[1].get_text()
    assert result["pages"] == 2
    assert result["selected_pages"] == [5, 2]


def test_export_selection_has_no_twenty_page_cap(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 21)
    _make_pdf(sandbox.attachments_dir / "background.pdf", ["shared background"])
    pages = [_page(f"page-{number}", number, "background.pdf") for number in range(1, 22)]

    result = export_pdf(
        sandbox,
        pages,
        Path("over-twenty.pdf"),
        page_numbers=list(range(1, 22)),
    )

    with fitz.open(result["path"]) as exported:
        assert exported.page_count == 21
    assert result["pages"] == 21
    assert result["selected_pages"] == list(range(1, 22))


@pytest.mark.parametrize(
    ("page_numbers", "message"),
    [
        ([], "at least one notebook page"),
        ([1, 1], "must not contain duplicates"),
        ([True], "must be integers"),
        ([1.5], "must be integers"),
        (["1"], "must be integers"),
        ([0], "must be positive"),
        ([-1], "must be positive"),
        ([3], "not present in the notebook"),
    ],
    ids=["empty", "duplicate", "bool", "float", "string", "zero", "negative", "unknown"],
)
def test_export_rejects_invalid_selection_without_creating_output(
    tmp_path: Path,
    page_numbers: list[Any],
    message: str,
) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 2)
    output = sandbox.outputs_dir / "invalid.pdf"

    with pytest.raises(GoodnotesError, match=message):
        export_pdf(
            sandbox,
            [_page("page-1", 1, None), _page("page-2", 2, None)],
            output,
            page_numbers=page_numbers,
        )

    assert not output.exists()
    assert list(sandbox.outputs_dir.iterdir()) == []


def test_export_deduplicates_repeated_image_streams_without_changing_pages(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    page_count = 12
    sandbox.add_notebook("notebook-1", page_count)
    source = sandbox.attachments_dir / "repeated-image.pdf"
    _make_repeated_image_pdf(source)
    pages = [
        _page(f"page-{number}", number, "repeated-image.pdf") for number in range(1, page_count + 1)
    ]

    result = export_pdf(sandbox, pages, Path("deduplicated.pdf"))
    exported_path = Path(str(result["path"]))

    baseline = fitz.open()
    try:
        for _ in pages:
            source_doc = fitz.open(source)
            try:
                baseline.insert_pdf(source_doc, from_page=0, to_page=0, annots=False)
            finally:
                source_doc.close()
        baseline_path = tmp_path / "baseline-garbage-3.pdf"
        baseline.save(baseline_path, garbage=3, deflate=True)
    finally:
        baseline.close()

    with fitz.open(exported_path) as exported, fitz.open(baseline_path) as unoptimized:
        assert exported.page_count == unoptimized.page_count == page_count
        assert exported[0].get_text() == unoptimized[0].get_text()
        assert (
            exported[0].get_pixmap(alpha=False).samples
            == unoptimized[0].get_pixmap(alpha=False).samples
        )
    assert exported_path.stat().st_size < baseline_path.stat().st_size * 0.6


def test_export_uses_one_based_mapping_for_multi_page_attachment(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 2)
    _make_pdf(sandbox.attachments_dir / "source.pdf", ["source one", "source two"])

    result = export_pdf(
        sandbox,
        [
            _page("page-2", 2, "source.pdf", pdf_page=2),
            _page("page-1", 1, "source.pdf", pdf_page=1),
        ],
        Path("mapped.pdf"),
    )

    with fitz.open(result["path"]) as exported:
        assert "source one" in exported[0].get_text()
        assert "source two" in exported[1].get_text()


def test_annotated_layer_never_falls_back_to_background(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)
    _make_pdf(sandbox.attachments_dir / "background.pdf", ["background text"])

    with pytest.raises(GoodnotesError, match="No annotated PDF is registered"):
        export_pdf(
            sandbox,
            [_page("page-1", 1, "background.pdf")],
            Path("annotated.pdf"),
            layer="annotated",
        )
    assert not (sandbox.outputs_dir / "annotated.pdf").exists()


def test_annotated_export_selects_current_page_numbers_in_requested_order(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 4)
    source = tmp_path / "annotated.pdf"
    _make_pdf(source, ["annotated one", "annotated two", "annotated three", "annotated four"])
    register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=4)
    pages = [_page(f"page-{number}", number, None) for number in range(4, 0, -1)]

    result = export_pdf(
        sandbox,
        pages,
        Path("annotated-selected.pdf"),
        layer="annotated",
        page_numbers=[4, 2],
    )

    with fitz.open(result["path"]) as exported:
        assert exported.page_count == 2
        assert "annotated four" in exported[0].get_text()
        assert "annotated two" in exported[1].get_text()
    assert result["pages"] == 2
    assert result["selected_pages"] == [4, 2]
    assert result["annotation_status"] == "user-provided-unverified"


def test_missing_background_attachment_does_not_create_blank_page(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)

    with pytest.raises(GoodnotesError, match="no available background attachment"):
        export_pdf(
            sandbox,
            [_page("page-1", 1, None)],
            Path("blank.pdf"),
        )
    assert not (sandbox.outputs_dir / "blank.pdf").exists()


def test_register_rejects_pdf_page_count_mismatch(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 2)
    source = tmp_path / "one-page.pdf"
    _make_pdf(source, ["only page"])

    with pytest.raises(GoodnotesError, match="notebook-1 has 2"):
        register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=2)
    assert not (sandbox.root.parent / ".goodnotes-cli").exists()


@pytest.mark.parametrize("document_id", ["../notebook", "/notebook", "folder\\notebook", "bad\nid"])
def test_register_rejects_path_like_notebook_ids(tmp_path: Path, document_id: str) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")

    with pytest.raises(GoodnotesError, match="without path characters"):
        register_annotated_pdf(sandbox, document_id, Path("unused.pdf"), expected_pages=1)

    assert not (sandbox.root.parent / ".goodnotes-cli").exists()


def test_annotated_export_maps_current_page_numbers_after_reordering(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 2)
    source = tmp_path / "annotated.pdf"
    _make_pdf(source, ["annotated first", "annotated second"])
    register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=2)

    result = export_pdf(
        sandbox,
        [_page("page-2", 2, None), _page("page-1", 1, None)],
        Path("annotated-order.pdf"),
        layer="annotated",
    )

    with fitz.open(result["path"]) as exported:
        assert exported.page_count == 2
        assert "annotated first" in exported[0].get_text()
        assert "annotated second" in exported[1].get_text()
    assert result["includes_handwriting"] is None
    assert result["annotation_status"] == "user-provided-unverified"

    rendered = render_page(
        sandbox,
        _page("page-1", 1, None),
        Path("annotated.png"),
        layer="annotated",
        dpi=72,
    )
    assert rendered["includes_handwriting"] is None
    assert rendered["annotation_status"] == "user-provided-unverified"


def test_render_page_checks_dpi_and_produces_png(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)
    _make_pdf(sandbox.attachments_dir / "background.pdf", ["background text"])

    result = render_page(
        sandbox,
        _page("page-1", 1, "background.pdf"),
        Path("page.png"),
        dpi=72,
    )
    with fitz.open(result["path"]) as rendered:
        assert rendered.page_count == 1
        assert rendered[0].rect.width == 612
        assert rendered[0].rect.height == 792
    assert result["includes_handwriting"] is False
    assert result["annotation_status"] == "not-included"

    with pytest.raises(GoodnotesError, match="between 72 and 600"):
        render_page(
            sandbox,
            _page("page-1", 1, "background.pdf"),
            Path("too-high.png"),
            dpi=601,
        )


def test_output_file_is_never_overwritten(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)
    _make_pdf(sandbox.attachments_dir / "background.pdf", ["background text"])
    existing = sandbox.outputs_dir / "existing.pdf"
    existing.write_bytes(b"keep this file")

    with pytest.raises(GoodnotesError, match="already exists"):
        export_pdf(sandbox, [_page("page-1", 1, "background.pdf")], Path("existing.pdf"))
    assert existing.read_bytes() == b"keep this file"


def test_registered_pdf_must_match_current_notebook_page_count(tmp_path: Path) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)
    source = tmp_path / "annotated.pdf"
    _make_pdf(source, ["only page"])
    register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=1)
    sandbox.add_notebook("notebook-1", 1)

    with pytest.raises(GoodnotesError, match="now has 2 pages"):
        export_pdf(
            sandbox,
            [_page("page-1", 1, None)],
            Path("stale.pdf"),
            layer="annotated",
        )
    assert not (sandbox.outputs_dir / "stale.pdf").exists()


def test_real_sandbox_registers_sidecar_outside_read_only_snapshot(tmp_path: Path) -> None:
    root = tmp_path / "snapshot"
    _make_real_snapshot(root, {"notebook-1": 1})
    source = tmp_path / "annotated.pdf"
    _make_pdf(source, ["annotated page"])
    root.chmod(0o555)
    try:
        sandbox = Sandbox.load(root)
        register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=1)
        output = tmp_path / "outside" / "annotated.pdf"
        result = export_pdf(
            sandbox,
            [_page("page-1", 1, None)],
            output,
            layer="annotated",
        )
    finally:
        root.chmod(0o755)

    registry_files = list((root.parent / ".goodnotes-cli").rglob("*.json"))
    assert len(registry_files) == 1
    assert registry_files[0].is_relative_to(root.parent)
    assert not registry_files[0].is_relative_to(root)
    assert result["path"] == str(output.resolve())
    assert output.is_file()


def test_concurrent_registrations_for_different_notebooks_both_survive(
    tmp_path: Path,
) -> None:
    root = tmp_path / "snapshot"
    _make_real_snapshot(root, {"notebook-a": 1, "notebook-b": 1})
    pdf_a = tmp_path / "annotated-a.pdf"
    pdf_b = tmp_path / "annotated-b.pdf"
    _make_pdf(pdf_a, ["registered for A"])
    _make_pdf(pdf_b, ["registered for B"])
    context = multiprocessing.get_context("spawn")
    results = context.Queue()
    processes = [
        context.Process(
            target=_register_pdf_in_process,
            args=(str(root), document_id, str(pdf_path), results),
        )
        for document_id, pdf_path in (("notebook-a", pdf_a), ("notebook-b", pdf_b))
    ]
    root.chmod(0o555)
    try:
        for process in processes:
            process.start()
        outcomes = [results.get(timeout=30) for _ in processes]
        for process in processes:
            process.join(timeout=30)
        assert all(process.exitcode == 0 for process in processes)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            process.join(timeout=10)
        results.close()
        root.chmod(0o755)

    assert all(success for _, success, _ in outcomes), outcomes
    records = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (root.parent / ".goodnotes-cli").rglob("*.json")
    ]
    assert {record["document_id"] for record in records} == {"notebook-a", "notebook-b"}

    sandbox = Sandbox.load(root)
    registered = (("notebook-a", "registered for A"), ("notebook-b", "registered for B"))
    for document_id, marker in registered:
        output = tmp_path / f"{document_id}.pdf"
        page = Page(
            id=f"{document_id}-page",
            document_id=document_id,
            number=1,
            position="01",
        )
        export_pdf(sandbox, [page], output, layer="annotated")
        with fitz.open(output) as exported:
            assert marker in exported[0].get_text()


def test_registered_pdf_integrity_detects_same_size_edit_with_restored_mtime(
    tmp_path: Path,
) -> None:
    sandbox = FakeSandbox(tmp_path / "sandbox")
    sandbox.add_notebook("notebook-1", 1)
    source = tmp_path / "annotated.pdf"
    _make_pdf(source, ["registered content"])
    register_annotated_pdf(sandbox, "notebook-1", source, expected_pages=1)
    original_stat = source.stat()
    with source.open("r+b") as stream:
        stream.seek(128)
        original = stream.read(1)
        stream.seek(128)
        stream.write(bytes([original[0] ^ 1]))
    os.utime(source, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    edited_stat = source.stat()
    assert edited_stat.st_size == original_stat.st_size
    assert edited_stat.st_mtime_ns == original_stat.st_mtime_ns

    with pytest.raises(GoodnotesError, match="integrity check"):
        export_pdf(
            sandbox,
            [_page("page-1", 1, None)],
            Path("changed.pdf"),
            layer="annotated",
        )
    assert not (sandbox.outputs_dir / "changed.pdf").exists()
