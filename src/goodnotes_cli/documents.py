"""PDF export and page rendering for sandboxed Goodnotes data."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path, PurePath
from typing import Protocol

import pymupdf as fitz

from .errors import FilesystemAccessError, GoodnotesError
from .models import Page
from .safety import _require_not_production, open_database


class SandboxProtocol(Protocol):
    @property
    def root(self) -> Path: ...

    @property
    def library_dir(self) -> Path: ...

    @property
    def attachments_dir(self) -> Path: ...

    @property
    def projection_path(self) -> Path: ...

    def ensure_output(self, path: Path) -> Path: ...


_REGISTRY_VERSION = 1
_BACKGROUND_WARNING = (
    "This export contains page backgrounds only. Goodnotes handwriting and other annotations "
    "are not included."
)
_ANNOTATED_WARNING = (
    "The CLI cannot check whether the registered PDF contains Goodnotes annotations. Export it "
    "with Include Annotations and Include Page Background enabled. Page count checks do not "
    "verify page order, so make sure the PDF pages match the notebook's current order."
)
_MIN_DPI = 72
_MAX_DPI = 600
_MAX_PAGE_POINTS = 14_400.0
_MAX_RENDER_PIXELS = 50_000_000


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
    try:
        if layer == "background":
            for page in ordered:
                source_path = _resolve_attachment_path(sandbox, page)
                _append_background_page(result_doc, page, source_path)
            includes_handwriting = False
            annotation_status = "not-included"
            warnings = [_BACKGROUND_WARNING]
            source = "per-page-background-attachments"
        elif layer == "native":
            for page in ordered:
                native_annotations.append(_append_native_page(result_doc, sandbox, page))
            includes_handwriting = any(
                bool(entry.get("rendered_strokes")) for entry in native_annotations
            )
            annotation_status = "native-reconstructed-approximate"
            warnings = list(
                dict.fromkeys(
                    warning for entry in native_annotations for warning in _native_warnings(entry)
                )
            )
            source = "copied-native-records-and-backgrounds"
        else:
            source_doc, entry = _open_registered_pdf(sandbox, ordered[0].document_id)
            try:
                _check_registered_page_count(sandbox, ordered[0].document_id, entry)
                registered_pages = _registered_page_count(entry)
                for page in ordered:
                    source_page = page.number - 1
                    _check_annotated_page_number(page, registered_pages)
                    result_doc.insert_pdf(
                        source_doc,
                        from_page=source_page,
                        to_page=source_page,
                        annots=True,
                    )
            finally:
                source_doc.close()
            includes_handwriting = None
            annotation_status = "user-provided-unverified"
            warnings = [_ANNOTATED_WARNING]
            source = "registered-user-exported-pdf"

        if result_doc.page_count != len(ordered):
            raise GoodnotesError("The PDF export did not contain the requested pages.")

        _publish_new_file(
            output_path,
            lambda temporary: result_doc.save(temporary, garbage=4, deflate=True),
        )
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


def render_page(
    sandbox: SandboxProtocol,
    page: Page,
    destination: Path,
    *,
    layer: str = "background",
    dpi: int = 150,
) -> dict[str, object]:
    """Render one page to PNG at a bounded resolution."""
    _check_layer(layer)
    _check_dpi(dpi)
    output_path = sandbox.ensure_output(Path(destination))
    if output_path.suffix.casefold() != ".png":
        raise GoodnotesError("Page renders must use a .png destination.")

    native_annotations: dict[str, object] | None = None
    if layer in {"background", "native"}:
        render_doc = fitz.open()
        try:
            if layer == "native":
                native_annotations = _append_native_page(render_doc, sandbox, page)
            else:
                source_path = _resolve_attachment_path(sandbox, page)
                _append_background_page(render_doc, page, source_path)
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
            annotation_status = "native-reconstructed-approximate"
            source = "copied-native-records-and-background"
            warnings = _native_warnings(native_annotations)
        else:
            includes_handwriting = False
            annotation_status = "not-included"
            source = "per-page-background-attachment"
            warnings = [_BACKGROUND_WARNING]
        source_page_number = page.pdf_page or 1
    else:
        source_doc, entry = _open_registered_pdf(sandbox, page.document_id)
        try:
            _check_registered_page_count(sandbox, page.document_id, entry)
            _check_annotated_page_number(page, _registered_page_count(entry))
            source_page = source_doc.load_page(page.number - 1)
            _check_render_size(source_page.rect, dpi)
            _publish_new_file(
                output_path,
                lambda temporary: source_page.get_pixmap(dpi=dpi, alpha=False).save(temporary),
            )
        except GoodnotesError:
            raise
        except Exception as exc:
            raise GoodnotesError(f"Could not render annotated page {page.number}: {exc}") from exc
        finally:
            source_doc.close()
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


def register_annotated_pdf(
    sandbox: SandboxProtocol,
    document_id: str,
    pdf_path: Path,
    expected_pages: int,
) -> dict[str, object]:
    """Register a user-exported annotated PDF after exact page-count checks.

    A per-notebook sidecar stores the path, page count, and file hash. It does not copy the source
    PDF. Page count checks cannot prove that the PDF pages match the notebook's current page order.
    """
    _validate_document_id(document_id)
    if (
        isinstance(expected_pages, bool)
        or not isinstance(expected_pages, int)
        or expected_pages < 1
    ):
        raise GoodnotesError("The expected notebook page count must be a positive integer.")

    actual_pages = _current_page_count(sandbox, document_id)
    if actual_pages != expected_pages:
        raise GoodnotesError(
            f"The notebook currently has {actual_pages} pages, but {expected_pages} were expected. "
            "Refresh the sandbox or use the current page count, then try again."
        )

    try:
        source_path = Path(pdf_path).expanduser().resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(f"The annotated PDF does not exist: {pdf_path}") from exc
    _require_not_production(source_path, purpose="an annotated-PDF input")
    if not source_path.is_file():
        raise GoodnotesError("The annotated PDF path must point to a regular file.")

    try:
        source_doc = fitz.open(source_path)
    except Exception as exc:
        raise GoodnotesError(f"Could not open the annotated PDF: {exc}") from exc
    try:
        if not source_doc.is_pdf:
            raise GoodnotesError("The registered annotated source must be a PDF file.")
        if source_doc.needs_pass:
            raise GoodnotesError("Password-protected PDFs cannot be registered.")
        actual_pdf_pages = source_doc.page_count
    finally:
        source_doc.close()

    if actual_pdf_pages != expected_pages:
        raise GoodnotesError(
            f"The PDF has {actual_pdf_pages} pages, but notebook {document_id} has "
            f"{expected_pages}. The PDF was not registered."
        )

    try:
        digest, source_stat = _sha256_stable(source_path)
    except GoodnotesError as exc:
        raise GoodnotesError(f"Could not verify the annotated PDF: {exc}") from exc
    entry: dict[str, object] = {
        "version": _REGISTRY_VERSION,
        "document_id": document_id,
        "path": str(source_path),
        "page_count": actual_pdf_pages,
        "size_bytes": source_stat.st_size,
        "mtime_ns": source_stat.st_mtime_ns,
        "sha256": digest,
    }

    registry_path = _registry_path(sandbox, document_id, create=True)
    _write_registry(registry_path, entry)

    return {
        "document_id": document_id,
        "path": str(source_path),
        "pages": actual_pdf_pages,
        "registered": True,
        "warnings": [
            "Registration checks page count only. Confirm that the PDF has the notebook's current "
            "page order and that its export included annotations and page backgrounds."
        ],
    }


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


def _append_native_page(
    document: fitz.Document, sandbox: SandboxProtocol, page: Page
) -> dict[str, object]:
    """Compose a copied native overlay with its verified background mapping."""
    from .annotations import render_native_overlay

    source_path = _resolve_attachment_path(sandbox, page)
    index = _append_background_page(document, page, source_path)
    target = document[index]
    if target.rotation or target.cropbox != target.mediabox:
        raise GoodnotesError(
            "Native annotation rendering does not yet support rotated or cropped backgrounds."
        )
    overlay = render_native_overlay(
        sandbox,
        page,
        width_points=target.rect.width,
        height_points=target.rect.height,
    )
    if overlay.svg and overlay.metadata.get("rendered_strokes"):
        with fitz.open(stream=overlay.svg, filetype="svg") as vector:
            with fitz.open(stream=vector.convert_to_pdf(), filetype="pdf") as ink:
                target.show_pdf_page(target.rect, ink, 0, overlay=True)
    return {"page": page.number, "page_id": page.id, **overlay.metadata}


def _check_dpi(dpi: int) -> None:
    if isinstance(dpi, bool) or not isinstance(dpi, int) or not _MIN_DPI <= dpi <= _MAX_DPI:
        raise GoodnotesError(f"DPI must be an integer between {_MIN_DPI} and {_MAX_DPI}.")


def _resolve_attachment_path(sandbox: SandboxProtocol, page: Page) -> Path:
    raw_path = page.attachment_path
    if not raw_path:
        raise GoodnotesError(
            f"Page {page.number} has no available background attachment. "
            "No blank page was substituted."
        )

    attachment_root = Path(sandbox.attachments_dir).resolve()
    raw = Path(raw_path).expanduser()
    candidates: list[Path] = []
    if raw.is_absolute():
        candidates.append(raw)
    else:
        parts = PurePath(raw).parts
        if ".." in parts:
            raise GoodnotesError("The page attachment path escapes the sandbox attachments folder.")
        if parts and parts[0].casefold() == "attachments":
            parts = parts[1:]
        if not parts:
            raise GoodnotesError("The page attachment path is empty.")
        candidates.append(attachment_root.joinpath(*parts))
        candidates.append(Path(sandbox.library_dir).joinpath(*PurePath(raw).parts))

    for candidate in candidates:
        try:
            resolved = candidate.resolve(strict=True)
        except FileNotFoundError:
            continue
        if not resolved.is_relative_to(attachment_root):
            raise GoodnotesError(
                "The page attachment resolves outside the sandbox attachments folder."
            )
        if not resolved.is_file():
            continue
        return resolved
    raise GoodnotesError(
        f"The background attachment for page {page.number} is missing from the sandbox copy."
    )


def _page_rotation(page: Page) -> int:
    rotation = page.rotation
    if isinstance(rotation, bool) or not isinstance(rotation, int) or rotation % 90:
        raise GoodnotesError(f"Page {page.number} has an unsupported rotation value.")
    return rotation % 360


def _attachment_page_index(page: Page, page_count: int) -> int:
    if page.pdf_page is None:
        if page_count == 1:
            return 0
        raise GoodnotesError(
            f"Page {page.number} uses a {page_count}-page attachment without a source-page mapping."
        )
    if isinstance(page.pdf_page, bool) or not isinstance(page.pdf_page, int):
        raise GoodnotesError(f"Page {page.number} has an invalid source PDF page number.")
    if not 1 <= page.pdf_page <= page_count:
        raise GoodnotesError(
            f"Page {page.number} maps to attachment page {page.pdf_page}, but that attachment has "
            f"{page_count} pages."
        )
    return page.pdf_page - 1


def _append_background_page(document: fitz.Document, page: Page, source_path: Path) -> int:
    source_doc: fitz.Document | None = None
    try:
        source_doc = fitz.open(source_path)
        if source_doc.needs_pass:
            raise GoodnotesError(
                f"The background attachment for page {page.number} is password protected."
            )
        source_index = _attachment_page_index(page, source_doc.page_count)
        source_page = source_doc.load_page(source_index)
        _check_page_size(source_page.rect)

        if source_doc.is_pdf:
            previous_rotation = source_page.rotation
            document.insert_pdf(
                source_doc,
                from_page=source_index,
                to_page=source_index,
                annots=False,
            )
            output_page = document.load_page(document.page_count - 1)
            rotation = (previous_rotation + _page_rotation(page)) % 360
            output_page.set_rotation(rotation)
        else:
            width, height = _image_page_size(source_page.rect, page)
            output_page = document.new_page(width=width, height=height)
            output_page.insert_image(output_page.rect, filename=str(source_path))
            output_page.set_rotation(_page_rotation(page))
        return document.page_count - 1
    except GoodnotesError:
        raise
    except Exception as exc:
        raise GoodnotesError(
            f"Could not read the background for page {page.number}: {exc}"
        ) from exc
    finally:
        if source_doc is not None:
            source_doc.close()


def _image_page_size(rect: fitz.Rect, page: Page) -> tuple[float, float]:
    width = page.width if page.width is not None else rect.width
    height = page.height if page.height is not None else rect.height
    _check_dimensions(width, height)
    return float(width), float(height)


def _check_page_size(rect: fitz.Rect) -> None:
    _check_dimensions(rect.width, rect.height)


def _check_dimensions(width: float, height: float) -> None:
    if (
        not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
        or width > _MAX_PAGE_POINTS
        or height > _MAX_PAGE_POINTS
    ):
        raise GoodnotesError("The page has unsupported dimensions.")


def _check_render_size(rect: fitz.Rect, dpi: int) -> None:
    _check_page_size(rect)
    width = math.ceil(rect.width * dpi / 72)
    height = math.ceil(rect.height * dpi / 72)
    if width * height > _MAX_RENDER_PIXELS:
        raise GoodnotesError(
            f"The page would render to {width:,} by {height:,} pixels. "
            "Lower the DPI or choose a smaller page."
        )


def _check_annotated_page_number(page: Page, registered_pages: int) -> None:
    if page.number > registered_pages:
        raise GoodnotesError(
            f"Page {page.number} is outside the registered PDF's {registered_pages} pages."
        )


def _current_page_count(sandbox: SandboxProtocol, document_id: str) -> int:
    with open_database(sandbox.projection_path) as connection:
        row = connection.execute(
            "SELECT count(*) FROM pages WHERE document_id = ? AND COALESCE(deleted, 0) = 0",
            (document_id,),
        ).fetchone()
    if row is None:
        raise GoodnotesError("Could not read the notebook page count from the sandbox.")
    return int(row[0])


def _sidecar_dir(sandbox: SandboxProtocol, *, create: bool = False) -> Path:
    try:
        return _sidecar_dir_checked(sandbox, create=create)
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc,
            operation="create_annotated_pdf_sidecar" if create else "read_annotated_pdf_record",
            path=Path(sandbox.root).parent / ".goodnotes-cli",
            kind="output" if create else "source",
        ) from exc


def _sidecar_dir_checked(sandbox: SandboxProtocol, *, create: bool) -> Path:
    root = Path(sandbox.root).resolve(strict=True)
    sidecar_parent = root.parent / ".goodnotes-cli"
    snapshot_key = hashlib.sha256(os.fsencode(root)).hexdigest()[:24]
    sidecar_dir = sidecar_parent / snapshot_key
    if sidecar_parent.is_symlink() or sidecar_dir.is_symlink():
        raise GoodnotesError("The annotated-PDF sidecar must not be a symbolic link.")
    _require_not_production(sidecar_dir, purpose="an annotated-PDF sidecar")
    if create:
        sentinel = sidecar_dir / f".validate-{uuid.uuid4().hex}"
        sandbox.ensure_output(sentinel)
        try:
            sidecar_parent.mkdir(mode=0o700, exist_ok=True)
            sidecar_dir.mkdir(mode=0o700, exist_ok=True)
        except OSError as exc:
            raise FilesystemAccessError.from_exception(
                exc, operation="create_annotated_pdf_sidecar", path=sidecar_dir, kind="output"
            ) from exc
    if sidecar_parent.is_symlink() or sidecar_dir.is_symlink():
        raise GoodnotesError("The annotated-PDF sidecar must not be a symbolic link.")
    resolved_dir = sidecar_dir.resolve(strict=create)
    overlaps_snapshot = (
        resolved_dir == root
        or root.is_relative_to(resolved_dir)
        or resolved_dir.is_relative_to(root)
    )
    if overlaps_snapshot:
        raise GoodnotesError("The annotated-PDF sidecar must stay outside the snapshot.")
    return resolved_dir


def _registry_path(sandbox: SandboxProtocol, document_id: str, *, create: bool = False) -> Path:
    _validate_document_id(document_id)
    sidecar = _sidecar_dir(sandbox, create=create)
    document_key = hashlib.sha256(document_id.encode("utf-8")).hexdigest()
    registry = sidecar / f"{document_key}.json"
    try:
        if registry.is_symlink():
            raise GoodnotesError("The notebook's annotated-PDF record must not be a symbolic link.")
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc,
            operation="write_annotated_pdf_record" if create else "read_annotated_pdf_record",
            path=registry,
            kind="output" if create else "source",
        ) from exc
    return registry


def _validate_document_id(document_id: str) -> None:
    if (
        not isinstance(document_id, str)
        or not document_id
        or len(document_id) > 256
        or document_id in {".", ".."}
        or document_id != document_id.strip()
        or "/" in document_id
        or "\\" in document_id
        or not document_id.isprintable()
    ):
        raise GoodnotesError("A valid notebook ID without path characters is required.")


def _read_registry(path: Path, document_id: str) -> dict[str, object] | None:
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise GoodnotesError("The annotated-PDF record must be a regular file.")
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="read_annotated_pdf_record", path=path, kind="source"
        ) from exc
    except json.JSONDecodeError as exc:
        raise GoodnotesError(f"Could not read the annotated-PDF record: {exc}") from exc
    if (
        not isinstance(raw, dict)
        or raw.get("version") != _REGISTRY_VERSION
        or raw.get("document_id") != document_id
    ):
        raise GoodnotesError("The annotated-PDF record has an unsupported or invalid format.")
    return raw


def _write_registry(path: Path, record: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".json", dir=path.parent
        )
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="write_annotated_pdf_record", path=path, kind="output"
        ) from exc
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream, ensure_ascii=True, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="write_annotated_pdf_record", path=path, kind="output"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)


def _open_registered_pdf(
    sandbox: SandboxProtocol, document_id: str
) -> tuple[fitz.Document, dict[str, object]]:
    _validate_document_id(document_id)
    registry_path = _registry_path(sandbox, document_id)
    entry = _read_registry(registry_path, document_id)
    if entry is None:
        raise GoodnotesError(
            f"No annotated PDF is registered for notebook {document_id}. Register a Goodnotes PDF "
            "export before requesting the annotated layer."
        )

    source_path_text = entry.get("path")
    if not isinstance(source_path_text, str) or not Path(source_path_text).is_absolute():
        raise GoodnotesError(
            "The registered annotated PDF path is invalid. Register the PDF again."
        )
    try:
        source_path = Path(source_path_text).resolve(strict=True)
    except OSError as exc:
        raise GoodnotesError(
            "The registered annotated PDF is unavailable at its saved path. Register it again."
        ) from exc
    if str(source_path) != source_path_text:
        raise GoodnotesError("The registered annotated PDF path changed. Register it again.")
    _require_not_production(source_path, purpose="an annotated-PDF input")
    try:
        stat = source_path.stat()
    except OSError as exc:
        raise GoodnotesError(
            "The registered annotated PDF is unavailable at its saved path. Register it again."
        ) from exc
    if not source_path.is_file():
        raise GoodnotesError("The registered annotated PDF is no longer a regular file.")
    if stat.st_size != entry.get("size_bytes") or stat.st_mtime_ns != entry.get("mtime_ns"):
        raise GoodnotesError("The registered annotated PDF changed. Register it again before use.")

    expected_digest = entry.get("sha256")
    try:
        digest, stable_stat = _sha256_stable(source_path)
    except GoodnotesError as exc:
        raise GoodnotesError(f"Could not verify the registered annotated PDF: {exc}") from exc
    if stable_stat.st_size != entry.get("size_bytes") or stable_stat.st_mtime_ns != entry.get(
        "mtime_ns"
    ):
        raise GoodnotesError("The registered annotated PDF changed while it was being read.")
    if not isinstance(expected_digest, str) or digest != expected_digest:
        raise GoodnotesError(
            "The registered annotated PDF failed its integrity check. Register it again."
        )

    try:
        source_doc = fitz.open(source_path)
    except Exception as exc:
        raise GoodnotesError(f"Could not open the registered annotated PDF: {exc}") from exc
    if not source_doc.is_pdf or source_doc.needs_pass:
        source_doc.close()
        raise GoodnotesError("The registered annotated source is no longer a readable PDF.")
    if source_doc.page_count != entry.get("page_count"):
        source_doc.close()
        raise GoodnotesError("The registered annotated PDF page count changed. Register it again.")
    return source_doc, entry


def _check_registered_page_count(
    sandbox: SandboxProtocol, document_id: str, entry: dict[str, object]
) -> None:
    current = _current_page_count(sandbox, document_id)
    registered_pages = _registered_page_count(entry)
    if current != registered_pages:
        raise GoodnotesError(
            f"The notebook now has {current} pages, but its registered PDF has "
            f"{registered_pages} pages. Register a new export for the current notebook."
        )


def _registered_page_count(entry: dict[str, object]) -> int:
    value = entry.get("page_count")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise GoodnotesError("The annotated-PDF registry has an invalid page count.")
    return value


def _sha256_stable(path: Path) -> tuple[str, os.stat_result]:
    """Hash a regular file and reject changes during the read."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise GoodnotesError(f"Could not open {path} for an integrity check.") from exc

    digest = hashlib.sha256()
    try:
        source = os.fdopen(descriptor, "rb")
        descriptor = -1
        with source:
            before_fd = os.fstat(source.fileno())
            before_path = path.lstat()
            if (
                not stat.S_ISREG(before_fd.st_mode)
                or not stat.S_ISREG(before_path.st_mode)
                or _file_signature(before_fd) != _file_signature(before_path)
            ):
                raise GoodnotesError("The annotated PDF changed before its integrity check.")
            while chunk := source.read(1024 * 1024):
                digest.update(chunk)
            after_fd = os.fstat(source.fileno())
            after_path = path.lstat()
            if _file_signature(before_fd) != _file_signature(after_fd) or _file_signature(
                before_fd
            ) != _file_signature(after_path):
                raise GoodnotesError("The annotated PDF changed during its integrity check.")
    except OSError as exc:
        raise GoodnotesError("Could not read the annotated PDF for its integrity check.") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return digest.hexdigest(), after_fd


def _file_signature(info: os.stat_result) -> tuple[int, int, int, int, int, int]:
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


def _publish_new_file(
    destination: Path,
    writer: Callable[[str], object],
) -> None:
    """Write to a sibling temporary file, then create the destination without overwriting."""
    temporary = destination.with_name(
        f".{destination.stem}.tmp-{os.getpid()}-{uuid.uuid4().hex}{destination.suffix}"
    )
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        writer(str(temporary))
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise GoodnotesError("The document renderer did not create an output file.")
        try:
            os.link(temporary, destination)
        except FileExistsError as exc:
            raise GoodnotesError(f"The output file already exists: {destination}") from exc
    except GoodnotesError:
        raise
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="write_document_output", path=destination, kind="output"
        ) from exc
    finally:
        temporary.unlink(missing_ok=True)
