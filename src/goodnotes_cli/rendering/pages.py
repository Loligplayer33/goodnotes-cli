"""Page render orchestration and shared operation resources."""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pymupdf as fitz

from ..errors import GoodnotesError
from ..models import Page
from .backgrounds import (
    _append_background_page,
    _check_dpi,
    _check_render_size,
    _PdfDocumentCache,
    _resolve_attachment_path,
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

if TYPE_CHECKING:
    from ..native import NativeError
_BACKGROUND_WARNING = (
    "This export contains page backgrounds only. Goodnotes handwriting and other annotations "
    "are not included."
)
_ANNOTATED_WARNING = (
    "The CLI cannot check whether the registered PDF contains Goodnotes annotations. Export it "
    "with Include Annotations and Include Page Background enabled. Page count checks do not "
    "verify page order, so make sure the PDF pages match the notebook's current order."
)


def render_page(
    sandbox: SandboxProtocol,
    page: Page,
    destination: Path,
    *,
    layer: str = "background",
    dpi: int = 150,
) -> dict[str, object]:
    """Render one page to PNG at a bounded resolution."""
    return render_pages(sandbox, [page], [destination], layer=layer, dpi=dpi)[0]


def render_pages(
    sandbox: SandboxProtocol,
    pages: list[Page],
    destinations: list[Path],
    *,
    layer: str = "background",
    dpi: int = 150,
    allow_partial_native_errors: bool = False,
) -> list[dict[str, object]]:
    """Render pages in one operation, sharing bounded source and native resources.

    Results preserve the input order and use the same shape as :func:`render_page`. The
    registered PDF is verified once per notebook, and one disposable native-store session is
    used for every page when ``layer="native"``. With
    ``allow_partial_native_errors=True``, classified native page failures are returned as
    ``render_error`` rows when at least one page succeeds; all-page failure raises the first error.
    """
    _check_layer(layer)
    _check_dpi(dpi)
    if not pages or len(pages) != len(destinations):
        raise GoodnotesError("Provide one PNG destination for every page to render.")

    output_paths = [sandbox.ensure_output(Path(destination)) for destination in destinations]
    if any(path.suffix.casefold() != ".png" for path in output_paths):
        raise GoodnotesError("Page renders must use a .png destination.")
    if len({path.resolve(strict=False) for path in output_paths}) != len(output_paths):
        raise GoodnotesError("Page render destinations must be distinct.")

    if layer == "native":
        with _native_record_reader(sandbox) as record_reader:
            return _render_pages_with_resources(
                sandbox,
                pages,
                output_paths,
                layer=layer,
                dpi=dpi,
                record_reader=record_reader,
                allow_partial_native_errors=allow_partial_native_errors,
            )
    return _render_pages_with_resources(
        sandbox,
        pages,
        output_paths,
        layer=layer,
        dpi=dpi,
        record_reader=None,
        allow_partial_native_errors=allow_partial_native_errors,
    )


def _render_pages_with_resources(
    sandbox: SandboxProtocol,
    pages: list[Page],
    output_paths: list[Path],
    *,
    layer: str,
    dpi: int,
    record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None,
    allow_partial_native_errors: bool = False,
) -> list[dict[str, object]]:
    from ..native import NativeError

    rendered: list[dict[str, object]] = []
    registered: dict[str, _RegisteredPdf] = {}
    first_page_error: NativeError | None = None
    with _PdfDocumentCache() as pdf_cache:
        try:
            for page, output_path in zip(pages, output_paths, strict=True):
                try:
                    result = _render_page_with_resources(
                        sandbox,
                        page,
                        output_path,
                        layer=layer,
                        dpi=dpi,
                        pdf_cache=pdf_cache,
                        registered=registered,
                        record_reader=record_reader,
                    )
                except NativeError as exc:
                    if not allow_partial_native_errors or not _is_page_local_native_error(exc):
                        raise
                    first_page_error = first_page_error or exc
                    rendered.append(_native_render_error(page, exc))
                    continue
                rendered.append(result)
            for registered_pdf in registered.values():
                registered_pdf.ensure_unchanged()
            if first_page_error is not None and not any(
                "render_error" not in result for result in rendered
            ):
                raise first_page_error
        finally:
            _close_registered_pdfs(registered)
    return rendered


def _is_page_local_native_error(exc: NativeError) -> bool:
    # read_native_page is used for missing records; engine and copy-integrity failures use other
    # operations and must abort the whole bundle.
    return exc.code == "native_data_unavailable" and exc.details.get("operation") in {
        "decode_native_page",
        "render_native_page",
        "map_native_page",
        "read_native_page",
    }


def _native_render_error(page: Page, exc: NativeError) -> dict[str, object]:
    return {
        "page": page.number,
        "render_error": {
            "code": exc.code,
            "message": str(exc),
            "details": dict(exc.details),
        },
    }


def _render_page_with_resources(
    sandbox: SandboxProtocol,
    page: Page,
    output_path: Path,
    *,
    layer: str,
    dpi: int,
    pdf_cache: _PdfDocumentCache,
    registered: dict[str, _RegisteredPdf],
    record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None,
) -> dict[str, object]:
    native_annotations: dict[str, object] | None = None
    if layer in {"background", "native"}:
        render_doc = fitz.open()
        try:
            if layer == "native":
                native_annotations = _append_native_page(
                    render_doc,
                    sandbox,
                    page,
                    pdf_cache=pdf_cache,
                    record_reader=record_reader,
                )
            else:
                source_path = _resolve_attachment_path(sandbox, page)
                _append_background_page(
                    render_doc,
                    page,
                    source_path,
                    source_doc=pdf_cache.open(source_path),
                )
            source_page = render_doc.load_page(0)
            _check_render_size(source_page.rect, dpi)
            _publish_new_file(
                output_path,
                lambda temporary: source_page.get_pixmap(dpi=dpi, alpha=False).save(temporary),
            )
        except GoodnotesError:
            raise
        except Exception as exc:
            raise GoodnotesError(f"Could not render page {page.number}: {exc}") from exc
        finally:
            render_doc.close()
        if native_annotations is not None:
            includes_handwriting = bool(native_annotations.get("rendered_strokes"))
            annotation_status = str(
                native_annotations.get("annotation_status", "native-reconstructed-approximate")
            )
            source = "copied-native-records-and-background"
            warnings = _native_warnings(native_annotations)
        else:
            includes_handwriting = False
            annotation_status = "not-included"
            source = "per-page-background-attachment"
            warnings = [_BACKGROUND_WARNING]
        source_page_number = page.pdf_page or 1
    else:
        registered_pdf = registered.get(page.document_id)
        if registered_pdf is None:
            registered_pdf = _open_registered_pdf(sandbox, page.document_id)
            try:
                _check_registered_page_count(sandbox, page.document_id, registered_pdf.entry)
            except BaseException:
                registered_pdf.close()
                raise
            registered[page.document_id] = registered_pdf
        try:
            registered_pdf.ensure_unchanged()
            _check_annotated_page_number(page, _registered_page_count(registered_pdf.entry))
            source_page = registered_pdf.document.load_page(page.number - 1)
            _check_render_size(source_page.rect, dpi)
            _publish_new_file(
                output_path,
                lambda temporary: source_page.get_pixmap(dpi=dpi, alpha=False).save(temporary),
            )
            registered_pdf.ensure_unchanged()
        except GoodnotesError:
            raise
        except Exception as exc:
            raise GoodnotesError(f"Could not render annotated page {page.number}: {exc}") from exc
        includes_handwriting = None
        annotation_status = "user-provided-unverified"
        source = "registered-user-exported-pdf"
        warnings = [_ANNOTATED_WARNING]
        source_page_number = page.number

    result: dict[str, object] = {
        "path": str(output_path),
        "document_id": page.document_id,
        "page": page.number,
        "source_page": source_page_number,
        "dpi": dpi,
        "layer": layer,
        "source": source,
        "includes_handwriting": includes_handwriting,
        "annotation_status": annotation_status,
        "warnings": warnings,
        "captured_at": getattr(sandbox, "captured_at", None),
    }
    if native_annotations is not None:
        result["native_annotations"] = native_annotations
    return result


def _ordered_pages(pages: list[Page]) -> list[Page]:
    if not pages:
        raise GoodnotesError("Select at least one notebook page to export.")
    document_id = pages[0].document_id
    if any(page.document_id != document_id for page in pages):
        raise GoodnotesError("A PDF export can include pages from only one notebook.")
    if any(isinstance(page.number, bool) or not isinstance(page.number, int) for page in pages):
        raise GoodnotesError("Every page must have an integer one-based page number.")
    if any(page.number < 1 for page in pages):
        raise GoodnotesError("Every page must have a positive one-based page number.")
    if len({page.number for page in pages}) != len(pages):
        raise GoodnotesError("The selected pages contain duplicate page numbers.")
    if len({page.id for page in pages}) != len(pages):
        raise GoodnotesError("The selected pages contain duplicate page IDs.")
    return sorted(pages, key=lambda page: page.number)


def _selected_pages(pages: list[Page], page_numbers: list[int] | None) -> list[Page]:
    ordered = _ordered_pages(pages)
    if page_numbers is None:
        return ordered
    if not isinstance(page_numbers, list):
        raise GoodnotesError("Selected page numbers must be provided as a list.")
    if not page_numbers:
        raise GoodnotesError("Select at least one notebook page to export.")
    if any(isinstance(number, bool) or not isinstance(number, int) for number in page_numbers):
        raise GoodnotesError("Selected page numbers must be integers.")
    if any(number < 1 for number in page_numbers):
        raise GoodnotesError("Selected page numbers must be positive one-based numbers.")
    if len(set(page_numbers)) != len(page_numbers):
        raise GoodnotesError("Selected page numbers must not contain duplicates.")

    pages_by_number = {page.number: page for page in ordered}
    unknown = [number for number in page_numbers if number not in pages_by_number]
    if unknown:
        raise GoodnotesError(
            f"Selected page number {unknown[0]} is not present in the notebook's current pages."
        )
    return [pages_by_number[number] for number in page_numbers]


def _check_layer(layer: str) -> None:
    if layer not in {"background", "annotated", "native"}:
        raise GoodnotesError("The PDF layer must be 'background', 'native', or 'annotated'.")


def _native_warnings(metadata: dict[str, object]) -> list[str]:
    warnings = metadata.get("warnings")
    return [str(value) for value in warnings] if isinstance(warnings, list) else []


@contextmanager
def _native_record_reader(
    sandbox: SandboxProtocol,
) -> Generator[Callable[[str], list[tuple[bytes, bytes]]], None, None]:
    from ..native import NativeStoreSession

    with NativeStoreSession(sandbox) as session:
        yield session.read_page_records


def _close_registered_pdfs(
    registered: dict[str, _RegisteredPdf],
) -> None:
    documents = list(registered.values())
    registered.clear()
    first_error: BaseException | None = None
    for registered_pdf in documents:
        try:
            registered_pdf.close()
        except BaseException as exc:
            first_error = first_error or exc
    if first_error is not None:
        raise first_error


def _append_native_page(
    document: fitz.Document,
    sandbox: SandboxProtocol,
    page: Page,
    *,
    pdf_cache: _PdfDocumentCache,
    record_reader: Callable[[str], list[tuple[bytes, bytes]]] | None,
    source_path: Path | None = None,
    final: bool = True,
) -> dict[str, object]:
    """Compose a copied native overlay with its verified background mapping."""
    from .overlay import render_native_overlay

    source_path = source_path or _resolve_attachment_path(sandbox, page)
    index = _append_background_page(
        document, page, source_path, source_doc=pdf_cache.open(source_path), final=final
    )
    target = document[index]
    if target.rotation or target.cropbox != target.mediabox:
        from ..native import NativeError

        raise NativeError(
            "native_data_unavailable",
            "Native annotation rendering does not yet support rotated or cropped backgrounds.",
            operation="map_native_page",
            context={"page": page.number},
        )
    from ..native import NativeError

    try:
        overlay = render_native_overlay(
            sandbox,
            page,
            width_points=target.rect.width,
            height_points=target.rect.height,
            record_reader=record_reader,
        )
    except NativeError as exc:
        exc.details["page"] = page.number
        raise
    if overlay.svg and (
        overlay.metadata.get("rendered_objects") or overlay.metadata.get("rendered_strokes")
    ):
        with fitz.open(stream=overlay.svg, filetype="svg") as vector:
            with fitz.open(stream=vector.convert_to_pdf(), filetype="pdf") as ink:
                target.show_pdf_page(target.rect, ink, 0, overlay=True)
    return {"page": page.number, "page_id": page.id, **overlay.metadata}
