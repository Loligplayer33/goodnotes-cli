"""All-or-nothing PDF export."""

from __future__ import annotations

from pathlib import Path

import pymupdf as fitz

from ..errors import GoodnotesError
from ..models import Page
from .backgrounds import (
    _append_background_page,
    _pdf_final_use_flags,
    _PdfDocumentCache,
    _resolve_attachment_path,
)
from .pages import (
    _ANNOTATED_WARNING,
    _BACKGROUND_WARNING,
    _append_native_page,
    _check_layer,
    _native_record_reader,
    _native_warnings,
    _selected_pages,
)
from .publication import _publish_new_file
from .registered import (
    _check_annotated_page_number,
    _check_registered_page_count,
    _open_registered_pdf,
    _registered_page_count,
    _RegisteredPdf,
)
from .types import SandboxProtocol


def export_pdf(
    sandbox: SandboxProtocol,
    pages: list[Page],
    destination: Path,
    *,
    layer: str = "background",
    page_numbers: list[int] | None = None,
) -> dict[str, object]:
    """Export notebook pages to a new PDF.

    Background exports use the per-page source attachment. Annotated exports require a PDF
    registered for the same document. When ``page_numbers`` is provided, selected pages are
    exported in that sequence. Neither path overwrites an existing output file.
    """
    ordered = _selected_pages(pages, page_numbers)
    _check_layer(layer)
    output_path = sandbox.ensure_output(Path(destination))
    if output_path.suffix.casefold() != ".pdf":
        raise GoodnotesError("PDF exports must use a .pdf destination.")

    result_doc = fitz.open()
    native_annotations: list[dict[str, object]] = []
    registered_pdf: _RegisteredPdf | None = None
    try:
        if layer == "background":
            source_paths = [_resolve_attachment_path(sandbox, page) for page in ordered]
            final_uses = _pdf_final_use_flags(source_paths)
            with _PdfDocumentCache() as pdf_cache:
                for page, source_path, final in zip(ordered, source_paths, final_uses, strict=True):
                    source_doc = pdf_cache.open(source_path)
                    _append_background_page(
                        result_doc,
                        page,
                        source_path,
                        source_doc=source_doc,
                        final=final,
                    )
            includes_handwriting = False
            annotation_status = "not-included"
            warnings = [_BACKGROUND_WARNING]
            source = "per-page-background-attachments"
        elif layer == "native":
            source_paths = [_resolve_attachment_path(sandbox, page) for page in ordered]
            final_uses = _pdf_final_use_flags(source_paths)
            with _native_record_reader(sandbox) as record_reader:
                with _PdfDocumentCache() as pdf_cache:
                    for page, source_path, final in zip(
                        ordered, source_paths, final_uses, strict=True
                    ):
                        native_annotations.append(
                            _append_native_page(
                                result_doc,
                                sandbox,
                                page,
                                pdf_cache=pdf_cache,
                                record_reader=record_reader,
                                source_path=source_path,
                                final=final,
                            )
                        )
            includes_handwriting = any(
                bool(entry.get("rendered_strokes")) for entry in native_annotations
            )
            annotation_status = (
                "native-no-records-observed"
                if all(
                    entry.get("annotation_status") == "native-no-records-observed"
                    for entry in native_annotations
                )
                else "native-reconstructed-approximate"
            )
            warnings = list(
                dict.fromkeys(
                    warning for entry in native_annotations for warning in _native_warnings(entry)
                )
            )
            source = "copied-native-records-and-backgrounds"
        else:
            registered_pdf = _open_registered_pdf(sandbox, ordered[0].document_id)
            try:
                _check_registered_page_count(sandbox, ordered[0].document_id, registered_pdf.entry)
                registered_pages = _registered_page_count(registered_pdf.entry)
                for index, page in enumerate(ordered):
                    registered_pdf.ensure_unchanged()
                    source_page = page.number - 1
                    _check_annotated_page_number(page, registered_pages)
                    result_doc.insert_pdf(
                        registered_pdf.document,
                        from_page=source_page,
                        to_page=source_page,
                        annots=True,
                        final=index == len(ordered) - 1,
                    )
                    registered_pdf.ensure_unchanged()
            finally:
                registered_pdf.close()
            includes_handwriting = None
            annotation_status = "user-provided-unverified"
            warnings = [_ANNOTATED_WARNING]
            source = "registered-user-exported-pdf"

        if result_doc.page_count != len(ordered):
            raise GoodnotesError("The PDF export did not contain the requested pages.")

        if registered_pdf is not None:
            registered_pdf.ensure_unchanged()
        _publish_new_file(
            output_path,
            lambda temporary: result_doc.save(temporary, garbage=4, deflate=True),
        )
        if registered_pdf is not None:
            registered_pdf.ensure_unchanged()
    except GoodnotesError:
        raise
    except Exception as exc:
        raise GoodnotesError(f"Could not export the PDF: {exc}") from exc
    finally:
        result_doc.close()

    result: dict[str, object] = {
        "path": str(output_path),
        "document_id": ordered[0].document_id,
        "pages": len(ordered),
        "selected_pages": [page.number for page in ordered],
        "layer": layer,
        "source": source,
        "includes_handwriting": includes_handwriting,
        "annotation_status": annotation_status,
        "warnings": warnings,
        "captured_at": getattr(sandbox, "captured_at", None),
    }
    if layer == "native":
        result["native_annotations"] = native_annotations
    return result
