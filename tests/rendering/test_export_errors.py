"""Selected PDF pages retain context across source planning and rendering failures."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from goodnotes_cli.documents import export_pdf
from goodnotes_cli.errors import GoodnotesError
from goodnotes_cli.models import Page
from goodnotes_cli.rendering.types import SandboxProtocol
from tests.rendering.test_documents import FakeSandbox, _make_pdf, _page


@pytest.mark.parametrize("selection", [[1, 2], [5, 2]])
def test_corrupt_attachment_reports_selected_public_page_and_publishes_nothing(
    tmp_path: Path, selection: list[int]
) -> None:
    sandbox = FakeSandbox(tmp_path / "snapshot")
    _make_pdf(sandbox.attachments_dir / "valid.pdf", ["generated valid page"])
    (sandbox.attachments_dir / "corrupt.pdf").write_bytes(b"generated invalid PDF")
    pages = [replace(_page(f"page-{n}", n, "valid.pdf"), pdf_page=1) for n in range(1, 6)]
    pages[1] = replace(pages[1], attachment_path="attachments/corrupt.pdf")

    with pytest.raises(GoodnotesError, match="page 2") as captured:
        export_pdf(sandbox, pages, Path("failed.pdf"), page_numbers=selection)

    assert captured.value.__cause__ is not None
    assert not list(sandbox.outputs_dir.iterdir())


@pytest.mark.parametrize("boundary", ["resolve", "signature", "open", "insert"])
def test_background_planning_and_render_failures_keep_page_and_explicit_cause(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import goodnotes_cli.rendering.export as exporter
    from goodnotes_cli.rendering import backgrounds

    sandbox = FakeSandbox(tmp_path / "snapshot")
    for name in ("first.pdf", "second.pdf"):
        _make_pdf(sandbox.attachments_dir / name, ["generated page"])
    pages = [_page("page-5", 5, "first.pdf"), _page("page-2", 2, "second.pdf")]
    expected = ValueError("generated boundary failure")
    closed = []
    original_close = backgrounds._PdfDocumentCache.close

    def close(cache: backgrounds._PdfDocumentCache) -> None:
        closed.append(True)
        original_close(cache)

    monkeypatch.setattr(backgrounds._PdfDocumentCache, "close", close)
    if boundary == "resolve":
        original = exporter._resolve_attachment_path

        def resolve(sandbox: SandboxProtocol, page: Page) -> Path:
            if page.number == 2:
                raise expected
            return original(sandbox, page)

        monkeypatch.setattr(exporter, "_resolve_attachment_path", resolve)
    elif boundary == "signature":
        original_signature = exporter._pdf_source_signature

        def signature(path: Path) -> tuple[int, int, int, int, int, int]:
            if path.name == "second.pdf":
                raise expected
            return original_signature(path)

        monkeypatch.setattr(exporter, "_pdf_source_signature", signature)
    elif boundary == "open":
        original_open = backgrounds._PdfDocumentCache.open

        def open_pdf(cache: backgrounds._PdfDocumentCache, path: Path) -> object:
            if path.name == "second.pdf":
                raise expected
            return original_open(cache, path)

        monkeypatch.setattr(backgrounds._PdfDocumentCache, "open", open_pdf)
    else:
        original_insert = exporter._append_background_page

        def insert(*args: Any, **kwargs: Any) -> int:
            if args[1].number == 2:
                raise expected
            return original_insert(*args, **kwargs)

        monkeypatch.setattr(exporter, "_append_background_page", insert)
    with pytest.raises(GoodnotesError, match="page 2") as captured:
        export_pdf(sandbox, pages, Path("failed.pdf"), page_numbers=[5, 2])
    assert captured.value.__cause__ is expected
    assert bool(closed) is (boundary in {"open", "insert"})
    assert not list(sandbox.outputs_dir.iterdir())


@pytest.mark.parametrize("kind", ["native", "interrupt"])
def test_export_keeps_native_error_instance_cause_and_control_interrupts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    import goodnotes_cli.rendering.export as exporter
    from goodnotes_cli.native import NativeError

    sandbox = FakeSandbox(tmp_path / "snapshot")
    _make_pdf(sandbox.attachments_dir / "valid.pdf", ["generated page"])
    page = _page("page-5", 5, "valid.pdf")
    cause = OSError("generated original cause")
    expected = NativeError("native_data_unavailable", "generated", operation="decode_native_page")
    expected.__cause__ = cause
    original_details = dict(expected.details)
    failure: BaseException = expected if kind == "native" else KeyboardInterrupt("generated")

    def fail(*args: object, **kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(exporter._PdfDocumentCache, "open", fail)
    with pytest.raises(type(failure)) as captured:
        export_pdf(sandbox, [page], Path("failed.pdf"))
    assert captured.value is failure
    if kind == "native":
        assert expected.__cause__ is cause
        assert expected.details == {**original_details, "page": 5}
    assert not list(sandbox.outputs_dir.iterdir())


@pytest.mark.parametrize("boundary", ["insert", "revalidate"])
def test_annotated_page_failure_is_attributed_and_closes_registered_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, boundary: str
) -> None:
    import pymupdf

    from goodnotes_cli.documents import register_annotated_pdf
    from goodnotes_cli.rendering.registered import _RegisteredPdf

    sandbox = FakeSandbox(tmp_path / "snapshot")
    sandbox.add_notebook("notebook-1", 5)
    pdf = tmp_path / "annotated.pdf"
    _make_pdf(pdf, [f"generated {n}" for n in range(5)])
    register_annotated_pdf(sandbox, "notebook-1", pdf, expected_pages=5)
    pages = [_page("page-5", 5, None), _page("page-2", 2, None)]
    expected = ValueError("generated annotated failure")
    closes = []
    original_close = _RegisteredPdf.close

    def close(source: _RegisteredPdf) -> None:
        closes.append(True)
        original_close(source)

    monkeypatch.setattr(_RegisteredPdf, "close", close)
    if boundary == "insert":
        original_insert = pymupdf.Document.insert_pdf

        def insert(document: pymupdf.Document, *args: Any, **kwargs: Any) -> object:
            if kwargs["from_page"] == 1:
                raise expected
            return original_insert(document, *args, **kwargs)

        monkeypatch.setattr(pymupdf.Document, "insert_pdf", insert)
    else:
        original_check = _RegisteredPdf.ensure_unchanged
        checks = 0

        def check(source: _RegisteredPdf) -> None:
            nonlocal checks
            checks += 1
            if checks == 3:
                raise expected
            original_check(source)

        monkeypatch.setattr(_RegisteredPdf, "ensure_unchanged", check)
    with pytest.raises(GoodnotesError, match="page 2") as captured:
        export_pdf(sandbox, pages, Path("failed.pdf"), layer="annotated", page_numbers=[5, 2])
    assert captured.value.__cause__ is expected
    assert closes == [True]
    assert not list(sandbox.outputs_dir.iterdir())


def test_native_background_open_failure_has_page_and_closes_session_and_pdf_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections.abc import Callable, Generator
    from contextlib import contextmanager
    from uuid import uuid4

    import goodnotes_cli.rendering.export as exporter
    from goodnotes_cli.rendering import backgrounds

    sandbox = FakeSandbox(tmp_path / "snapshot")
    _make_pdf(sandbox.attachments_dir / "first.pdf", ["generated first"], width=144, height=144)
    (sandbox.attachments_dir / "corrupt.pdf").write_bytes(b"generated corrupt PDF")
    pages = [
        replace(_page(str(uuid4()), number, name), width=264, height=264)
        for number, name in ((5, "first.pdf"), (2, "corrupt.pdf"))
    ]
    events = []
    original_close = backgrounds._PdfDocumentCache.close

    @contextmanager
    def reader(_sandbox: SandboxProtocol) -> Generator[Callable[..., list], None, None]:
        events.append("session-open")
        try:
            yield lambda _root: []
        finally:
            events.append("session-close")

    def close(cache: backgrounds._PdfDocumentCache) -> None:
        events.append("cache-close")
        original_close(cache)

    monkeypatch.setattr(exporter, "_native_record_reader", reader)
    monkeypatch.setattr(backgrounds._PdfDocumentCache, "close", close)
    with pytest.raises(GoodnotesError, match="page 2") as captured:
        export_pdf(sandbox, pages, Path("failed.pdf"), layer="native", page_numbers=[5, 2])
    assert captured.value.__cause__ is not None
    assert events == ["session-open", "cache-close", "session-close"]
    assert not list(sandbox.outputs_dir.iterdir())
