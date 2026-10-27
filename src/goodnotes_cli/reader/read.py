"""Read bounded text from a page of the selected snapshot."""

from __future__ import annotations

from ..errors import GoodnotesError
from ..models import Item, PageRef, PageText, RecognitionStatus, TextPart
from . import bounded, index
from . import text as text_queries
from .catalog import Catalog
from .pages import PageReader
from .pdf import PdfTextReader
from .session import ReaderSession


class PageTextReader:
    def __init__(
        self, session: ReaderSession, catalog: Catalog, pages: PageReader, pdf: PdfTextReader
    ) -> None:
        self._session = session
        self._catalog = catalog
        self._pages = pages
        self._pdf = pdf

    def read_page(
        self,
        notebook: str | Item,
        page: int,
        *,
        source: str = "all",
        max_chars: int | None = None,
    ) -> PageText:
        """Read a page, optionally restricting sources and materialized text length."""
        self._session.require_open()
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise GoodnotesError("Page numbers are one-based positive integers.")
        source = source.casefold() if isinstance(source, str) else ""
        try:
            selected_sources = index.selected_text_sources(source)
        except GoodnotesError as exc:
            raise GoodnotesError(
                "Read source must be all, text, handwriting, pdf, or textbox."
            ) from exc
        if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0
        ):
            raise GoodnotesError("max_chars must be a non-negative integer or omitted.")
        item = self._catalog.resolve(notebook, kind="notebook")
        pages = self._pages.pages(item.id)
        if page > len(pages):
            raise GoodnotesError(f"Page {page} is outside this notebook's active page range.")
        selected = pages[page - 1]
        parts: list[TextPart] = []
        warnings = self._read_warnings(source)
        remaining = max_chars
        truncated = False

        if "handwriting" in selected_sources:
            rows, remaining, omitted, found = self._read_text_table(
                "handwriting", selected.id, remaining
            )
            parts.extend(rows)
            truncated = truncated or omitted
            if found:
                warnings.append(
                    "Stored handwriting text is machine-recognized and unverified. Candidate "
                    "alternatives and delimiters are preserved as stored. Label any cleaned "
                    "reading as an interpretation, not a verbatim quote or visual transcription."
                )
            recognition = self._recognition_status_for_page_ids([selected.id]).get(selected.id)
            if recognition is not None and recognition.index_state == "pending":
                warnings.append(
                    "Handwriting indexing is pending for this page; stored recognition text "
                    "may be missing or outdated."
                )

        if not truncated and "textbox" in selected_sources:
            rows, remaining, omitted, _ = self._read_text_table("textbox", selected.id, remaining)
            parts.extend(rows)
            truncated = truncated or omitted

        if not truncated and "pdf" in selected_sources:
            page_refs = self._pages.page_refs_for_notebooks({item.id}, page_ids={selected.id})
            page_ref = (
                page_refs[0] if page_refs else PageRef(document_id=item.id, page_id=selected.id)
            )
            pdf_parts, remaining, omitted, indexed, mapping_warning = self._pdf.read_indexed_page(
                page_ref, remaining
            )
            parts.extend(pdf_parts)
            truncated = truncated or omitted
            if mapping_warning:
                warnings.append(mapping_warning)
            if indexed:
                warnings.append(
                    "PDF text comes from the indexed PDF layer and may omit annotations."
                )
            elif (
                remaining == 0
                and selected.attachment_path
                and selected.pdf_page is not None
                and self._pages.attachment_state_by_page.get(
                    (selected.document_id, selected.id), ("unmapped", None)
                )[0]
                == "available"
            ):
                # There is no indexed text to inspect, so avoid extracting a whole PDF page
                # merely to return an empty string at a zero-character budget.
                truncated = True
                warnings.append(
                    "PDF background text was not read because the character budget was exhausted."
                )
            else:
                with self._pdf.pdf_session():
                    extracted_text, extraction_warning, extraction_truncated = (
                        self._pdf.extract_fallback(selected, max_chars=remaining)
                    )
                if extracted_text:
                    parts.append(
                        TextPart(
                            source="pdf",
                            text=extracted_text,
                            truncated=extraction_truncated,
                        )
                    )
                    if remaining is not None:
                        remaining = max(0, remaining - len(extracted_text))
                truncated = truncated or extraction_truncated
                if extraction_warning:
                    warnings.append(extraction_warning)

        return PageText(selected.id, item.id, page, parts, warnings, truncated)

    def _read_warnings(self, source: str) -> list[str]:
        """Return only source-coverage warnings relevant to this read request."""
        self._session.ensure_fts()
        selected_sources = index.selected_text_sources(source)
        if self._session.fts is None:
            labels = ", ".join(index.TEXT_LABELS[item].lower() for item in selected_sources)
            return [
                "The sandbox has no search index; indexed "
                f"{labels} text is unavailable for this page."
            ]
        warnings: list[str] = []
        for selected in selected_sources:
            label = index.TEXT_LABELS[selected]
            table = index.TEXT_TABLES[selected]
            if not self._session.table_exists(self._session.fts, table):
                warnings.append(
                    f"{label} text is unavailable because `{table}` is missing from the "
                    "search index."
                )
        if "pdf" in selected_sources and not index.page_meta_supports_pdf_mapping(
            self._session.fts, schema=self._session.fts_schema
        ):
            warnings.append(
                "Indexed PDF text cannot be mapped because usable `page_meta` mapping is "
                "unavailable in the search index."
            )
        if (
            "pdf" in selected_sources
            and self._session.table_exists(self._session.fts, "pdf_text_content")
            and not index.pdf_identity_schema_available(
                self._session.fts, schema=self._session.fts_schema
            )
        ):
            warnings.append(
                "Indexed PDF text cannot be verified because full PDF page identity columns are "
                "unavailable in the search index."
            )
        return warnings

    def _read_text_table(
        self, source: str, page_id: str, budget: int | None
    ) -> tuple[list[TextPart], int | None, bool, bool]:
        """Read one ordinary content table while loading at most its remaining budget."""
        if self._session.fts is None:
            return [], budget, False, False
        table = index.TEXT_TABLES[source]
        if not self._session.table_exists(self._session.fts, table):
            return [], budget, False, False

        if source == "handwriting":
            ordering = (
                "CASE priority WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 ELSE 2 END, text_id"
            )
            result_ordering = (
                "CASE candidates.priority WHEN 'primary' THEN 0 WHEN 'secondary' THEN 1 "
                "ELSE 2 END, candidates.text_id"
            )
            metadata_fields = ", engine_type, priority"
            candidate_fields = ", engine_type, priority"
        else:
            ordering = "text_id"
            result_ordering = "candidates.text_id"
            metadata_fields = ""
            candidate_fields = ""

        if budget == 0:
            index.sql_batch_size(self._session.fts)
            row = self._session.fts.execute(
                f"""
                SELECT text_id, typeof(text) AS text_type, length(text) AS text_length
                       {metadata_fields}
                FROM {table}
                WHERE page_id = ? AND COALESCE(text, '') <> ''
                ORDER BY {ordering}
                LIMIT 1
                """,
                (page_id,),
            ).fetchone()
            if row is None:
                return [], budget, False, False
            bounded.validate_metadata(row, table, handwriting=source == "handwriting")
            return [], budget, True, True

        parts: list[TextPart] = []
        found = False
        index.sql_batch_size(self._session.fts, fixed=6)
        rows = self._session.fts.execute(
            f"""
            WITH candidates AS (
                SELECT text_id, typeof(text) AS text_type, length(text) AS text_length
                       {candidate_fields},
                       row_number() OVER (ORDER BY {ordering}) AS row_number,
                       count(*) OVER () AS row_count,
                       COALESCE(sum(length(text)) OVER (
                           ORDER BY {ordering}
                           ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                       ), 0) AS prior_length
                FROM {table}
                WHERE page_id = ? AND COALESCE(text, '') <> ''
            )
            SELECT candidates.*,
                   CASE WHEN ? IS NULL THEN content.text
                        ELSE substr(content.text, 1,
                            min(candidates.text_length, ? - candidates.prior_length + 1))
                   END AS bounded_text,
                   CASE WHEN ? IS NULL THEN 0
                        ELSE instr(content.text, char(0))
                   END AS contains_nul
            FROM candidates
            JOIN {table} AS content
              ON content.text_id = candidates.text_id AND content.page_id = ?
            WHERE ? IS NULL OR candidates.prior_length < ?
            ORDER BY {result_ordering}
            """,
            (page_id, budget, budget, budget, page_id, budget, budget),
        )
        for row in rows:
            found = True
            part, budget, omitted = bounded.materialize(row, table, source, budget)
            if part is not None:
                parts.append(part)
            if omitted:
                return parts, budget, True, found
        return parts, budget, False, found

    def _recognition_status_for_page_ids(self, page_ids: list[str]) -> dict[str, RecognitionStatus]:
        """Read recognition row counts and optional pending flags without loading text."""
        assert self._session.projection is not None
        self._session.ensure_fts()
        raw_statuses = text_queries.recognition_status(
            self._session.projection,
            self._session.fts,
            page_ids,
            projection_schema=self._session.projection_schema,
            fts_schema=self._session.fts_schema,
        )
        return {page_id: RecognitionStatus(**status) for page_id, status in raw_statuses.items()}
