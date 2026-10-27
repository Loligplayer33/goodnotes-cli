"""Background resolution, mapping and page-size checks."""

from __future__ import annotations

import math
import stat
from collections import OrderedDict
from pathlib import Path, PurePath
from typing import Literal

import pymupdf as fitz

from ..errors import GoodnotesError
from ..models import Page
from ..storage.paths import resolve_confined_file, stat_key
from .types import SandboxProtocol

_MIN_DPI = 72
_MAX_DPI = 600
_MAX_PAGE_POINTS = 14_400.0
_MAX_RENDER_PIXELS = 50_000_000
_MAX_OPEN_PDF_SOURCES = 8


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
    if raw.is_absolute():
        resolved = resolve_confined_file(attachment_root, raw)
        if resolved is not None:
            return resolved
    else:
        original_parts = PurePath(raw).parts
        parts = original_parts
        if ".." in parts:
            raise GoodnotesError("The page attachment path escapes the sandbox attachments folder.")
        if parts and parts[0].casefold() == "attachments":
            parts = parts[1:]
        if not parts:
            raise GoodnotesError("The page attachment path is empty.")
        for root, relative_parts in (
            (attachment_root, parts),
            (Path(sandbox.library_dir), original_parts),
        ):
            resolved = resolve_confined_file(root, Path(*relative_parts))
            if resolved is not None:
                if not resolved.is_relative_to(attachment_root):
                    raise GoodnotesError(
                        "The page attachment resolves outside the sandbox attachments folder."
                    )
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


def _append_background_page(
    document: fitz.Document,
    page: Page,
    source_path: Path,
    *,
    source_doc: fitz.Document | None = None,
    final: bool = True,
) -> int:
    owns_source_doc = source_doc is None
    try:
        if source_doc is None:
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
                final=final,
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
        if owns_source_doc and source_doc is not None:
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


class _PdfDocumentCache:
    """Keep a bounded set of stable source documents open for one document operation."""

    def __init__(self, capacity: int = _MAX_OPEN_PDF_SOURCES) -> None:
        self._capacity = capacity
        self._documents: OrderedDict[
            tuple[int, int], tuple[Path, fitz.Document, tuple[int, int, int, int, int, int]]
        ] = OrderedDict()
        self._path_signatures: dict[Path, tuple[int, int, int, int, int, int]] = {}

    def __enter__(self) -> _PdfDocumentCache:
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> Literal[False]:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise
        return False

    def open(self, path: Path) -> fitz.Document:
        source_path = Path(path)
        signature = _pdf_source_signature(source_path)
        previous_signature = self._path_signatures.get(source_path)
        if previous_signature is not None and previous_signature != signature:
            raise GoodnotesError("A page background changed during the rendering operation.")
        self._path_signatures[source_path] = signature
        identity = signature[0], signature[1]
        cached = self._documents.get(identity)
        if cached is not None:
            if cached[2] != signature:
                raise GoodnotesError("A page background changed during the rendering operation.")
            self._documents.move_to_end(identity)
            return cached[1]

        source_doc = fitz.open(source_path)
        try:
            if _pdf_source_signature(source_path) != signature:
                raise GoodnotesError("A page background changed while it was being opened.")
            self._documents[identity] = source_path, source_doc, signature
            while len(self._documents) > self._capacity:
                _, (_, evicted, _) = self._documents.popitem(last=False)
                evicted.close()
        except BaseException:
            source_doc.close()
            raise
        return source_doc

    def close(self) -> None:
        documents = [entry[1] for entry in self._documents.values()]
        self._documents.clear()
        first_error: BaseException | None = None
        for document in documents:
            try:
                document.close()
            except BaseException as exc:
                first_error = first_error or exc
        if first_error is not None:
            raise first_error


def _pdf_source_signature(path: Path) -> tuple[int, int, int, int, int, int]:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise GoodnotesError("A page background is no longer a regular file.")
    return stat_key(info)


def _pdf_final_use_flags(paths: list[Path], capacity: int = _MAX_OPEN_PDF_SOURCES) -> list[bool]:
    """Mark each cached PDF handle's last insertion before reuse ends or eviction."""
    cache: OrderedDict[tuple[int, int], int] = OrderedDict()
    handles: list[int] = []
    last_use: dict[int, int] = {}
    signatures: dict[tuple[int, int], tuple[int, int, int, int, int, int]] = {}
    next_handle = 0
    for index, path in enumerate(paths):
        signature = _pdf_source_signature(path)
        identity = signature[0], signature[1]
        previous = signatures.get(identity)
        if previous is not None and previous != signature:
            raise GoodnotesError("A page background changed during the rendering operation.")
        signatures[identity] = signature
        handle = cache.get(identity)
        if handle is None:
            if len(cache) >= capacity:
                cache.popitem(last=False)
            handle = next_handle
            next_handle += 1
            cache[identity] = handle
        else:
            cache.move_to_end(identity)
        handles.append(handle)
        last_use[handle] = index
    return [last_use[handle] == index for index, handle in enumerate(handles)]
