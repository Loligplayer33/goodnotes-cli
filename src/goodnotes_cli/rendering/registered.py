"""Register and verify annotated PDF sources for sandboxed notebooks."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

import pymupdf as fitz

from ..errors import FilesystemAccessError, GoodnotesError
from ..models import Page
from ..storage.metadata import CACHE_METADATA_LIMIT, read_json
from ..storage.paths import _require_not_production
from ..storage.paths import stat_key as _file_signature
from ..storage.sandbox import open_database
from .types import SandboxProtocol

_REGISTRY_VERSION = 1


@dataclass
class _RegisteredPdf:
    document: fitz.Document
    entry: dict[str, object]
    path: Path
    signature: tuple[int, int, int, int, int, int]

    def ensure_unchanged(self) -> None:
        try:
            current = _file_signature(self.path.lstat())
        except OSError as exc:
            raise GoodnotesError(
                "The registered annotated PDF became unavailable during this operation."
            ) from exc
        if current != self.signature:
            raise GoodnotesError(
                "The registered annotated PDF changed during this operation. "
                "Register it again before use."
            )

    def close(self) -> None:
        self.document.close()


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
        digest, source_stat = _sha256_stable(source_path)
    except GoodnotesError as exc:
        raise GoodnotesError(f"Could not verify the annotated PDF: {exc}") from exc

    try:
        source_doc = fitz.open(source_path)
    except Exception as exc:
        raise GoodnotesError(f"Could not open the annotated PDF: {exc}") from exc
    try:
        if _file_signature(source_stat) != _file_signature(source_path.lstat()):
            raise GoodnotesError("The annotated PDF changed before its page count was checked.")
        if not source_doc.is_pdf:
            raise GoodnotesError("The registered annotated source must be a PDF file.")
        if source_doc.needs_pass:
            raise GoodnotesError("Password-protected PDFs cannot be registered.")
        actual_pdf_pages = source_doc.page_count
        if _file_signature(source_stat) != _file_signature(source_path.lstat()):
            raise GoodnotesError("The annotated PDF changed while its page count was checked.")
    except OSError as exc:
        raise GoodnotesError("Could not verify the annotated PDF path after opening it.") from exc
    finally:
        source_doc.close()

    if actual_pdf_pages != expected_pages:
        raise GoodnotesError(
            f"The PDF has {actual_pdf_pages} pages, but notebook {document_id} has "
            f"{expected_pages}. The PDF was not registered."
        )

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
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode):
            raise GoodnotesError("The annotated-PDF record must be a regular file.")
        raw = read_json(path, limit=CACHE_METADATA_LIMIT, encoding="utf-8")
        if _file_signature(info) != _file_signature(path.lstat()):
            raise ValueError("The annotated-PDF record changed while reading.")
    except OSError as exc:
        raise FilesystemAccessError.from_exception(
            exc, operation="read_annotated_pdf_record", path=path, kind="source"
        ) from exc
    except ValueError as exc:
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


def _open_registered_pdf(sandbox: SandboxProtocol, document_id: str) -> _RegisteredPdf:
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
        initial_stat = source_path.stat()
    except OSError as exc:
        raise GoodnotesError(
            "The registered annotated PDF is unavailable at its saved path. Register it again."
        ) from exc
    if not source_path.is_file():
        raise GoodnotesError("The registered annotated PDF is no longer a regular file.")
    if initial_stat.st_size != entry.get("size_bytes") or initial_stat.st_mtime_ns != entry.get(
        "mtime_ns"
    ):
        raise GoodnotesError("The registered annotated PDF changed. Register it again before use.")

    expected_digest = entry.get("sha256")
    try:
        digest, stable_stat = _sha256_stable(source_path)
    except GoodnotesError as exc:
        raise GoodnotesError(f"Could not verify the registered annotated PDF: {exc}") from exc
    if _file_signature(initial_stat) != _file_signature(stable_stat):
        raise GoodnotesError("The registered annotated PDF changed while it was being read.")
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
    try:
        if _file_signature(source_path.lstat()) != _file_signature(stable_stat):
            raise GoodnotesError("The registered annotated PDF changed after its integrity check.")
        if not source_doc.is_pdf or source_doc.needs_pass:
            raise GoodnotesError("The registered annotated source is no longer a readable PDF.")
        if source_doc.page_count != entry.get("page_count"):
            raise GoodnotesError(
                "The registered annotated PDF page count changed. Register it again."
            )
        if _file_signature(source_path.lstat()) != _file_signature(stable_stat):
            raise GoodnotesError("The registered annotated PDF changed while it was opened.")
    except OSError as exc:
        source_doc.close()
        raise GoodnotesError(
            "Could not verify the registered annotated PDF path after opening it."
        ) from exc
    except BaseException:
        source_doc.close()
        raise
    return _RegisteredPdf(source_doc, entry, source_path, _file_signature(stable_stat))


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
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
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
