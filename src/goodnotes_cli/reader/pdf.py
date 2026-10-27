"""Bounded extraction and scanning of PDF text in a read-only snapshot."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import contextmanager
from typing import Any

from ..errors import GoodnotesError
from ..models import Item, Page, PageRef, SearchHit, TextPart
from . import bounded, index
from . import text as text_index
from .attachments import AttachmentResolver
from .pages import PageReader
from .session import ReaderSession, _pymupdf

_MAX_PDF_FALLBACK_BYTES = 128 * 1024 * 1024
_MAX_PDF_FALLBACK_TEXT = 100_000


class PdfTextReader:
    """Read indexed text and confined PDF attachments for a reader session."""

    def __init__(
        self, session: ReaderSession, pages: PageReader, attachments: AttachmentResolver
    ) -> None:
        self._session = session
        self._pages = pages
        self._attachments = attachments
        self._bindings_cache: dict[tuple[PageRef, ...], text_index._PDFPageBindings] = {}

    def clear(self) -> None:
        self._bindings_cache.clear()

    def bindings_for(self, page_refs: list[PageRef]) -> text_index._PDFPageBindings:
        key = tuple(page_refs)
        cached = self._bindings_cache.get(key)
        if cached is None:
            cached = text_index.pdf_page_bindings(
                self._session.fts, page_refs, schema=self._session.fts_schema
            )
            self._bindings_cache[key] = cached
        return cached

    @contextmanager
    def pdf_session(self) -> Generator[None, None, None]:
        self._session.start_pdf_cache()
        self._attachments.begin_read_cache()
        yield

    @staticmethod
    def validate_scan(query: str, *, limit: int, page_limit: int, page_offset: int) -> None:
        if not isinstance(query, str) or not query:
            raise GoodnotesError("Search query must not be empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GoodnotesError("Search limit must be a positive integer.")
        if (
            isinstance(page_limit, bool)
            or not isinstance(page_limit, int)
            or page_limit < 1
            or page_limit > 250
        ):
            raise GoodnotesError("PDF scan page_limit must be between 1 and 250.")
        if isinstance(page_offset, bool) or not isinstance(page_offset, int) or page_offset < 0:
            raise GoodnotesError("PDF scan page_offset must be a non-negative integer.")

    def scan_pdfs(
        self,
        query: str,
        scope: dict[str, Any],
        notebooks: list[Item],
        *,
        limit: int = 20,
        page_limit: int = 200,
        page_offset: int = 0,
    ) -> dict[str, Any]:
        """Search copied PDF backgrounds in a bounded window of notebook pages."""
        self.validate_scan(query, limit=limit, page_limit=page_limit, page_offset=page_offset)

        total_pages = sum(item.page_count for item in notebooks)
        window_pages = min(page_limit, max(0, total_pages - page_offset))

        hits: list[SearchHit] = []
        warnings: list[str] = [
            "PDF scan searches copied background PDF text only; it does not scan handwriting "
            "or the stored search index."
        ]
        pages_scanned = 0
        missing_backgrounds = 0
        unmapped_backgrounds = 0
        unreadable_backgrounds = 0
        truncated_pages = 0
        extraction_issues: list[dict[str, Any]] = []
        folded_query = query.casefold()

        stopped_for_hits = False
        for notebook_item, page in self._pages.iter_page_window(
            notebooks, offset=page_offset, count=window_pages
        ):
            pages_scanned += 1
            text, extraction_warning, extraction_truncated = self.extract_fallback(page)
            issue: str | None = None
            attachment_state, _ = self._pages.attachment_state_by_page.get(
                (page.document_id, page.id), ("unmapped", None)
            )
            if attachment_state != "available":
                missing_backgrounds += 1
                issue = {
                    "missing": "missing-attachment",
                    "unsafe": "unsafe-attachment-path",
                    "ambiguous": "ambiguous-attachment",
                    "unavailable": "unavailable-attachment",
                }.get(attachment_state, "missing-background")
            elif page.pdf_page is None:
                unmapped_backgrounds += 1
                issue = "unmapped-background"
            elif text is None:
                unreadable_backgrounds += 1
                issue = "unreadable-background"
            if extraction_truncated:
                truncated_pages += 1
                issue = "truncated-text"
            if issue is not None:
                extraction_issues.append(
                    {
                        "page_id": page.id,
                        "document_id": notebook_item.id,
                        "page": page.number,
                        "issue": issue,
                        "detail": extraction_warning,
                    }
                )
            if text is not None and folded_query in text.casefold():
                hits.append(
                    SearchHit(
                        document_id=notebook_item.id,
                        path=notebook_item.path,
                        page=page.number,
                        page_id=page.id,
                        source="pdf",
                        text=text,
                    )
                )
                if len(hits) >= limit and page_offset + pages_scanned < total_pages:
                    stopped_for_hits = True
                    break

        scan_complete = page_offset + pages_scanned >= total_pages
        if scan_complete:
            stop_reason = "complete"
        elif stopped_for_hits:
            stop_reason = "hit-limit"
        else:
            stop_reason = "page-limit"
        if missing_backgrounds:
            warnings.append(
                f"{missing_backgrounds} scanned pages have no confined copied background."
            )
        if unmapped_backgrounds:
            warnings.append(
                f"{unmapped_backgrounds} copied backgrounds have no verified PDF page mapping."
            )
        if unreadable_backgrounds:
            warnings.append(
                f"{unreadable_backgrounds} mapped copied backgrounds yielded no PDF text."
            )
        if truncated_pages:
            warnings.append(
                f"{truncated_pages} PDF page extractions were truncated at the safe text limit."
            )
        if extraction_issues:
            warnings.append(
                "PDF text coverage is incomplete for one or more scanned pages; see "
                "`extraction_issues`."
            )

        return {
            "hits": hits,
            "pages_scanned": pages_scanned,
            "next_page_offset": None if scan_complete else page_offset + pages_scanned,
            "scope": scope,
            "total_pages": total_pages,
            "scan_complete": scan_complete,
            "coverage_complete": page_offset == 0 and scan_complete and not extraction_issues,
            "stop_reason": stop_reason,
            "extraction_issues": extraction_issues,
            "warnings": warnings,
        }

    def extract_fallback(
        self, page: Page, *, max_chars: int | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract one mapped PDF page from a safe copied attachment, with size bounds."""
        if not page.attachment_path:
            status, _ = self._pages.attachment_state_by_page.get(
                (page.document_id, page.id), ("unmapped", None)
            )
            messages = {
                "missing": "The mapped copied attachment is missing from the sandbox.",
                "unsafe": "The stored attachment path is outside the sandbox copy.",
                "ambiguous": (
                    "The copied attachment cannot be resolved because its basename is ambiguous."
                ),
                "unavailable": "The copied attachment could not be inspected safely.",
            }
            return (
                None,
                messages.get(
                    status,
                    "No confined copied attachment is available for PDF text extraction.",
                ),
                False,
            )
        if page.pdf_page is None or page.pdf_page < 1:
            return (
                None,
                "PDF text is not indexed and the page has no verified PDF page mapping.",
                False,
            )
        safe_path, status = self._attachments.resolve(page.attachment_path, for_read=True)
        if status == "unsafe":
            return None, "The stored attachment path is outside the sandbox copy.", False
        if safe_path is None:
            if status == "missing":
                return None, "The mapped copied attachment is missing from the sandbox.", False
            return None, "The copied PDF attachment is unavailable.", False
        try:
            if safe_path.stat().st_size > _MAX_PDF_FALLBACK_BYTES:
                return (
                    None,
                    "The copied PDF attachment exceeds the safe extraction size limit.",
                    False,
                )
            with self._session.pdf_document(safe_path) as pdf:
                if page.pdf_page > pdf.page_count:
                    return None, "The mapped PDF page is outside the copied attachment.", False
                text = pdf.load_page(page.pdf_page - 1).get_text("text").strip()
        except (OSError, RuntimeError, ValueError, _pymupdf().FileDataError):
            return None, "The copied attachment could not be read as a PDF.", False
        if not text:
            return None, "The mapped PDF page has no extractable text.", False
        truncated = False
        warning = "PDF text was extracted from the confined copied attachment."
        if len(text) > _MAX_PDF_FALLBACK_TEXT:
            text = text[:_MAX_PDF_FALLBACK_TEXT]
            truncated = True
            warning = (
                "PDF text was extracted from the confined copied attachment and truncated "
                f"at {_MAX_PDF_FALLBACK_TEXT:,} characters."
            )
        if max_chars is not None and len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
            warning = (
                "PDF text was extracted from the confined copied attachment and truncated "
                "to the requested character budget."
            )
        return text, warning, truncated

    def read_indexed_page(
        self, page_ref: PageRef, budget: int | None
    ) -> tuple[list[TextPart], int | None, bool, bool, str | None]:
        """Read PDF index rows that match the projection-derived page identity."""
        self._session.ensure_fts()
        if self._session.fts is None or not index.table_exists(
            self._session.fts, "pdf_text_content", schema=self._session.fts_schema
        ):
            return [], budget, False, False, None
        binding_result = self.bindings_for([page_ref])
        if not binding_result["available"]:
            return (
                [],
                budget,
                False,
                False,
                "Indexed PDF text identity could not be verified; only confined copied "
                "background text can be used.",
            )
        binding = binding_result["pages"][page_ref.page_id]
        if binding["state"] == "unknown":
            return (
                [],
                budget,
                False,
                False,
                "Indexed PDF text mapping could not be verified for this page; only confined "
                "copied background text can be used.",
            )
        mapping_warning = None
        if binding["mismatched_direct_pointer"]:
            mapping_warning = (
                "A stored PDF text pointer did not match the current page identity and was ignored."
            )
        text_ids = binding["text_ids"]
        if not text_ids:
            return [], budget, False, False, mapping_warning

        assert self._session.fts is not None
        parts: list[TextPart] = []
        found = False

        offset = 0
        while offset < len(text_ids):
            batch_size = index.sql_batch_size(self._session.fts, fixed=3 if budget == 0 else 11)
            batch = text_ids[offset : offset + batch_size]
            offset += len(batch)
            placeholders = ", ".join("?" for _ in batch)
            if budget == 0:
                row = self._session.fts.execute(
                    f"""
                    SELECT text_id, typeof(text_id) AS text_id_type,
                           typeof(text) AS text_type, length(text) AS text_length
                    FROM pdf_text_content
                    WHERE text_id IN ({placeholders})
                      AND document_id = ? AND attachment_id = ? AND pdf_page_no = ?
                      AND COALESCE(text, '') <> ''
                    ORDER BY text_id
                    LIMIT 1
                    """,
                    (*batch, page_ref.document_id, page_ref.attachment_id, page_ref.pdf_page_no),
                ).fetchone()
                if row is not None:
                    bounded.validate_metadata(row, "pdf_text_content", pdf=True)
                    return parts, budget, True, True, mapping_warning
                continue

            rows = self._session.fts.execute(
                f"""
                WITH candidates AS (
                    SELECT text_id, typeof(text_id) AS text_id_type,
                           typeof(text) AS text_type, length(text) AS text_length,
                           row_number() OVER (ORDER BY text_id) AS row_number,
                           count(*) OVER () AS row_count,
                           COALESCE(sum(length(text)) OVER (
                               ORDER BY text_id
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ), 0) AS prior_length
                    FROM pdf_text_content
                    WHERE text_id IN ({placeholders})
                      AND document_id = ? AND attachment_id = ? AND pdf_page_no = ?
                      AND COALESCE(text, '') <> ''
                )
                SELECT candidates.*,
                       CASE WHEN ? IS NULL THEN content.text
                            ELSE substr(content.text, 1,
                                min(candidates.text_length,
                                    ? - candidates.prior_length + 1))
                       END AS bounded_text,
                       CASE WHEN ? IS NULL THEN 0
                            ELSE instr(content.text, char(0))
                       END AS contains_nul
                FROM candidates
                JOIN pdf_text_content AS content
                  ON content.text_id = candidates.text_id
                 AND content.document_id = ? AND content.attachment_id = ?
                 AND content.pdf_page_no = ?
                WHERE ? IS NULL OR candidates.prior_length < ?
                ORDER BY candidates.text_id
                """,
                (
                    *batch,
                    page_ref.document_id,
                    page_ref.attachment_id,
                    page_ref.pdf_page_no,
                    budget,
                    budget,
                    budget,
                    page_ref.document_id,
                    page_ref.attachment_id,
                    page_ref.pdf_page_no,
                    budget,
                    budget,
                ),
            )
            for row in rows:
                found = True
                part, budget, omitted = bounded.materialize(row, "pdf_text_content", "pdf", budget)
                if part is not None:
                    parts.append(part)
                if omitted:
                    return parts, budget, True, found, mapping_warning
        return parts, budget, False, found, mapping_warning
