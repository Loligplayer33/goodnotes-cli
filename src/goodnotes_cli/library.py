"""Read-only access to a marked Goodnotes library snapshot."""

from __future__ import annotations

import hashlib
import heapq
import math
import shlex
import sqlite3
from collections import OrderedDict
from collections.abc import Generator, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import text_index
from .errors import GoodnotesError, TextIndexUnavailableError
from .models import Item, Page, PageRef, PageText, RecognitionStatus, SearchHit, TextPart
from .safety import open_database, resolve_confined_file

if TYPE_CHECKING:
    from .safety import Sandbox


_FOLDER = 0
_NOTEBOOK = 1
_MAX_PDF_FALLBACK_BYTES = 128 * 1024 * 1024
_MAX_PDF_FALLBACK_TEXT = 100_000
_MAX_OPEN_PDFS = 4


def _pymupdf() -> Any:
    """Import PyMuPDF only for operations that actually read a PDF."""
    import pymupdf

    return pymupdf


class _PDFDocumentCache:
    """A small LRU of read-only PDF documents scoped to one Library session."""

    def __init__(self, maximum: int = _MAX_OPEN_PDFS) -> None:
        self.maximum = maximum
        self.documents: OrderedDict[Path, Any] = OrderedDict()

    def get(self, path: Path) -> Any:
        document = self.documents.pop(path, None)
        if document is None:
            document = _pymupdf().open(path)
        self.documents[path] = document
        while len(self.documents) > self.maximum:
            _, evicted = self.documents.popitem(last=False)
            evicted.close()
        return document

    def close(self) -> None:
        for document in reversed(self.documents.values()):
            document.close()
        self.documents.clear()


@dataclass(frozen=True)
class _Entry:
    kind: str
    id: str
    name: str
    parent_id: str | None
    is_root: bool
    position: str
    updated_at: float
    page_count: int = 0


@dataclass(frozen=True)
class _SearchCandidate:
    """A heap entry whose smallest heap element is the worst retained hit."""

    sort_key: tuple[str, int, int, int]
    sequence: int
    duplicate_key: tuple[str, str, int, bytes]
    hit: SearchHit

    def __lt__(self, other: _SearchCandidate) -> bool:
        return (self.sort_key, self.sequence) > (other.sort_key, other.sequence)


class Library:
    """Read a sandboxed Goodnotes snapshot without changing either database."""

    def __init__(self, sandbox: Sandbox) -> None:
        self.sandbox = sandbox
        self._stack: ExitStack | None = None
        self._projection: sqlite3.Connection | None = None
        self._fts: sqlite3.Connection | None = None
        self._fts_attempted = False
        self._projection_schema: text_index.SchemaCache | None = None
        self._fts_schema: text_index.SchemaCache | None = None
        self._active_items: dict[tuple[str, str], str] = {}
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._links: dict[tuple[str, str], list[sqlite3.Row]] = {}
        self._children: dict[tuple[str, str] | None, list[tuple[str, str]]] = {}
        self._paths: dict[tuple[str, str], str | None] = {}
        self._duplicate_parent_count = 0
        self._cycle_count = 0
        self._orphan_count = 0
        self._broken_link_count = 0
        self._links_to_deleted_count = 0
        self._active_folder_count = 0
        self._active_notebook_count = 0
        self._page_numbers: dict[tuple[str, str], int] | None = None
        self._page_cache: dict[tuple[str, str], Page] = {}
        self._pages_by_notebook: dict[str, list[Page]] = {}
        self._attachment_state_by_page: dict[tuple[str, str], tuple[str, str | None]] = {}
        self._attachment_resolution_cache: dict[str, tuple[Path | None, str]] = {}
        self._pdf_attachment_resolution_cache: dict[str, tuple[Path | None, str]] | None = None
        self._attachment_basename_cache: dict[str, list[Path]] | None = None
        self._text_warnings_cache: list[str] | None = None
        self._page_refs_cache: dict[
            tuple[tuple[str, ...], tuple[str, ...] | None], list[PageRef]
        ] = {}
        self._pdf_bindings_cache: dict[tuple[PageRef, ...], text_index._PDFPageBindings] = {}
        self._scope_cache: dict[
            tuple[tuple[str, str] | str, tuple[str, str] | str | None],
            tuple[dict[str, Any], list[Item], set[str]],
        ] = {}
        self._pdf_documents: _PDFDocumentCache | None = None

    def __enter__(self) -> Library:
        if self._stack is not None:
            raise GoodnotesError("Library is already open.")

        stack = ExitStack()
        try:
            self._projection = stack.enter_context(open_database(self.sandbox.projection_path))
            self._projection_schema = text_index.SchemaCache(self._projection)
            self._fts = None
            self._fts_schema = None
            self._fts_attempted = False
            self._pdf_documents = None
            self._pdf_attachment_resolution_cache = None
            self._page_cache.clear()
            self._pages_by_notebook.clear()
            self._attachment_state_by_page.clear()
            self._attachment_resolution_cache.clear()
            self._pdf_attachment_resolution_cache = None
            self._attachment_basename_cache = None
            self._page_refs_cache.clear()
            self._pdf_bindings_cache.clear()
            self._scope_cache.clear()
            self._page_numbers = None
            self._stack = stack
            self._load_catalog()
            self._text_warnings_cache = None
        except Exception:
            stack.close()
            self._projection = None
            self._fts = None
            self._projection_schema = None
            self._fts_schema = None
            self._stack = None
            raise
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if self._pdf_documents is not None:
                self._pdf_documents.close()
        finally:
            self._pdf_documents = None
            self._pdf_attachment_resolution_cache = None
            try:
                if self._stack is not None:
                    self._stack.close()
            finally:
                self._stack = None
                self._projection = None
                self._fts = None
                self._projection_schema = None
                self._fts_schema = None
                self._fts_attempted = False
                self._page_cache.clear()
                self._pages_by_notebook.clear()
                self._attachment_state_by_page.clear()
                self._attachment_resolution_cache.clear()
                self._attachment_basename_cache = None
                self._page_refs_cache.clear()
                self._pdf_bindings_cache.clear()
                self._scope_cache.clear()
                self._page_numbers = None
                self._text_warnings_cache = None

    def list_items(self, folder: str | Item = "/", kind: str | None = None) -> list[Item]:
        """List direct children of a folder, or direct children of the library root."""
        self._require_open()
        self._ensure_consistent_graph()
        normalized_kind = self._validate_kind(kind)
        parent_key = self._folder_key(folder, root_allowed=True)
        keys = self._children.get(parent_key, [])
        return [
            self._item_for_key(key)
            for key in keys
            if normalized_kind is None or key[0] == normalized_kind
        ]

    def tree(
        self,
        folder: str | Item = "/",
        max_depth: int | None = None,
    ) -> list[Item]:
        """Return descendant items in stored sibling order, in pre-order."""
        self._require_open()
        self._ensure_consistent_graph()
        if max_depth is not None and (
            isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0
        ):
            raise GoodnotesError("max_depth must be a non-negative integer or omitted.")
        parent_key = self._folder_key(folder, root_allowed=True)
        result: list[Item] = []

        def visit(key: tuple[str, str] | None, depth: int) -> None:
            if max_depth is not None and depth > max_depth:
                return
            for child_key in self._children.get(key, []):
                result.append(self._item_for_key(child_key))
                if child_key[0] == "folder":
                    visit(child_key, depth + 1)

        visit(parent_key, 1)
        return result

    def resolve(self, selector: str | Item, kind: str | None = None) -> Item:
        """Resolve an exact id, exact full path, or unique basename without guessing."""
        self._require_open()
        self._ensure_consistent_graph()
        normalized_kind = self._validate_kind(kind)
        if isinstance(selector, Item):
            selector = selector.id
        if not isinstance(selector, str) or selector == "":
            raise GoodnotesError("An item selector is required.")

        id_matches = [key for key in self._active_items if key[1].casefold() == selector.casefold()]
        if id_matches:
            if len(id_matches) > 1:
                raise GoodnotesError("That UUID is ambiguous in the active library data.")
            if normalized_kind is not None and id_matches[0][0] != normalized_kind:
                raise GoodnotesError("That selector identifies an item of a different kind.")
            return self._resolved_item(id_matches)

        path_selector = selector.startswith("/") or "/" in selector
        if path_selector:
            path = self._normalize_path_selector(selector)
            if not path:
                raise GoodnotesError("The library root has no item UUID to resolve.")
            matches = [
                key
                for key, candidate in self._paths.items()
                if candidate == path and (normalized_kind is None or key[0] == normalized_kind)
            ]
        else:
            matches = [
                key
                for key, name in self._active_items.items()
                if name == selector and (normalized_kind is None or key[0] == normalized_kind)
            ]
        if not matches:
            raise GoodnotesError("No active, reachable item matches that selector.")
        return self._resolved_item(matches)

    def pages(self, notebook: str | Item) -> list[Page]:
        """Return active notebook pages in stable position/id order, numbered from one."""
        self._require_open()
        self._ensure_consistent_graph()
        item = self.resolve(notebook, kind="notebook")
        cached = self._pages_by_notebook.get(item.id)
        if cached is not None:
            return list(cached)
        assert self._projection is not None
        rows = self._projection.execute(
            """
            SELECT id, document_id, template_id, position, group_id
            FROM pages
            WHERE document_id = ? AND COALESCE(deleted, 0) = 0
            ORDER BY position, id
            """,
            (item.id,),
        ).fetchall()
        pages = self._hydrate_page_rows(rows)
        self._pages_by_notebook[item.id] = pages
        return list(pages)

    def read_page(
        self,
        notebook: str | Item,
        page: int,
        *,
        source: str = "all",
        max_chars: int | None = None,
    ) -> PageText:
        """Read a page, optionally restricting sources and materialized text length."""
        self._require_open()
        if isinstance(page, bool) or not isinstance(page, int) or page < 1:
            raise GoodnotesError("Page numbers are one-based positive integers.")
        source = source.casefold() if isinstance(source, str) else ""
        if source not in {"all", *text_index.TEXT_TABLES}:
            raise GoodnotesError("Read source must be all, handwriting, pdf, or textbox.")
        if max_chars is not None and (
            isinstance(max_chars, bool) or not isinstance(max_chars, int) or max_chars < 0
        ):
            raise GoodnotesError("max_chars must be a non-negative integer or omitted.")
        item = self.resolve(notebook, kind="notebook")
        pages = self.pages(item)
        if page > len(pages):
            raise GoodnotesError(f"Page {page} is outside this notebook's active page range.")
        selected = pages[page - 1]
        parts: list[TextPart] = []
        warnings = self._read_warnings(source)
        selected_sources = list(text_index.TEXT_TABLES) if source == "all" else [source]
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
            page_refs = self._page_refs_for_notebooks({item.id}, page_ids={selected.id})
            page_ref = (
                page_refs[0] if page_refs else PageRef(document_id=item.id, page_id=selected.id)
            )
            pdf_parts, remaining, omitted, indexed, mapping_warning = self._read_pdf_text_for_page(
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
                and Path(selected.attachment_path).is_file()
            ):
                # There is no indexed text to inspect, so avoid extracting a whole PDF page
                # merely to return an empty string at a zero-character budget.
                truncated = True
                warnings.append(
                    "PDF background text was not read because the character budget was exhausted."
                )
            else:
                with self._pdf_session():
                    extracted_text, extraction_warning, extraction_truncated = (
                        self._extract_pdf_fallback(selected, max_chars=remaining)
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
        self._ensure_fts()
        if source == "all":
            selected_sources = list(text_index.TEXT_TABLES)
        else:
            selected_sources = [source]
        if self._fts is None:
            labels = ", ".join(text_index.TEXT_LABELS[item].lower() for item in selected_sources)
            return [
                "The sandbox has no search index; indexed "
                f"{labels} text is unavailable for this page."
            ]
        warnings: list[str] = []
        for selected in selected_sources:
            label = text_index.TEXT_LABELS[selected]
            table = text_index.TEXT_TABLES[selected]
            if not self._table_exists(self._fts, table):
                warnings.append(
                    f"{label} text is unavailable because `{table}` is missing from the "
                    "search index."
                )
        if "pdf" in selected_sources and not text_index.page_meta_supports_pdf_mapping(
            self._fts, schema=self._fts_schema
        ):
            warnings.append(
                "Indexed PDF text cannot be mapped because usable `page_meta` mapping is "
                "unavailable in the search index."
            )
        if (
            "pdf" in selected_sources
            and self._table_exists(self._fts, "pdf_text_content")
            and not text_index.pdf_identity_schema_available(self._fts, schema=self._fts_schema)
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
        if self._fts is None:
            return [], budget, False, False
        table = text_index.TEXT_TABLES[source]
        if not self._table_exists(self._fts, table):
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

        def validate_metadata(
            row: sqlite3.Row,
        ) -> tuple[int, int, str | None, str | None]:
            text_id = row["text_id"]
            if not isinstance(text_id, int) or isinstance(text_id, bool):
                raise GoodnotesError(
                    f"Search index has an invalid `{table}.text_id`; expected an integer."
                )
            if row["text_type"] != "text":
                raise GoodnotesError(
                    f"Search index has an invalid `{table}.text` value; expected text."
                )
            text_length = row["text_length"]
            if not isinstance(text_length, int) or isinstance(text_length, bool) or text_length < 0:
                raise GoodnotesError(
                    f"Search index has an invalid `{table}.text` length; "
                    "expected a non-negative integer."
                )
            engine: str | None = None
            priority: str | None = None
            if source == "handwriting":
                engine_value = row["engine_type"]
                priority_value = row["priority"]
                if engine_value is not None and not isinstance(engine_value, str):
                    raise GoodnotesError(
                        f"Search index has an invalid `{table}.engine_type`; expected text or NULL."
                    )
                if priority_value is not None and not isinstance(priority_value, str):
                    raise GoodnotesError(
                        f"Search index has an invalid `{table}.priority`; expected text or NULL."
                    )
                engine = engine_value
                priority = priority_value
            return text_id, text_length, engine, priority

        if budget == 0:
            row = self._fts.execute(
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
            validate_metadata(row)
            return [], budget, True, True

        parts: list[TextPart] = []
        found = False
        rows = self._fts.execute(
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
            _, text_length, engine, priority = validate_metadata(row)
            if budget is not None and row["contains_nul"]:
                raise GoodnotesError(
                    f"Search index has embedded NUL in `{table}.text`; "
                    "a bounded read cannot preserve this value."
                )
            text = row["bounded_text"]
            if not isinstance(text, str):
                raise GoodnotesError(
                    f"Search index has an invalid `{table}.text` value; expected text."
                )
            available = budget
            part_truncated = False
            if available is not None:
                part_truncated = text_length > available or len(text) > available
                if part_truncated:
                    text = text[:available]
            if text:
                parts.append(
                    TextPart(
                        source=source,
                        text=text,
                        engine=engine,
                        priority=priority,
                        truncated=part_truncated,
                    )
                )
            if budget is not None:
                budget = max(0, budget - len(text))
            if part_truncated:
                return parts, budget, True, found
            if budget == 0 and row["row_number"] < row["row_count"]:
                return parts, budget, True, found
        return parts, budget, False, found

    def recognition_status(self, notebook: str | Item) -> dict[str, RecognitionStatus]:
        """Return stored handwriting-recognition status for every active notebook page."""
        self._require_open()
        self._ensure_consistent_graph()
        item = self._resolve_notebook_selector(notebook, option="--notebook")
        assert self._projection is not None
        page_ids = [
            row[0]
            for row in self._projection.execute(
                "SELECT id FROM pages WHERE document_id = ? AND COALESCE(deleted, 0) = 0 "
                "ORDER BY position, id",
                (item.id,),
            )
        ]
        return self._recognition_status_for_page_ids(page_ids)

    def _recognition_status_for_page_ids(self, page_ids: list[str]) -> dict[str, RecognitionStatus]:
        """Read recognition row counts and optional pending flags without loading text."""
        assert self._projection is not None
        self._ensure_fts()
        raw_statuses = text_index.recognition_status(
            self._projection,
            self._fts,
            page_ids,
            projection_schema=self._projection_schema,
            fts_schema=self._fts_schema,
        )
        return {page_id: RecognitionStatus(**status) for page_id, status in raw_statuses.items()}

    def text_warnings(self) -> list[str]:
        """Report library-wide text-source and page-mapping limitations for diagnostics."""
        self._require_open()
        self._ensure_fts()
        if self._text_warnings_cache is not None:
            return list(self._text_warnings_cache)
        if self._fts is None:
            self._text_warnings_cache = [
                "Library-wide: the sandbox has no search index; indexed handwriting, textbox, "
                "and PDF text sources are unavailable."
            ]
            return list(self._text_warnings_cache)

        warnings = [
            f"{text_index.TEXT_LABELS[source]} text is unavailable because `{table}` is missing "
            "from the search index."
            for source, table in text_index.TEXT_TABLES.items()
            if not self._table_exists(self._fts, table)
        ]
        if not text_index.page_meta_supports_search(self._fts, schema=self._fts_schema):
            warnings.append(
                "Stored-index search cannot map text rows to notebook pages because usable "
                "`page_meta` columns are missing."
            )
        else:
            index_page_ids = {
                row[0] for row in self._fts.execute("SELECT page_id FROM page_meta").fetchall()
            }
            missing_page_count = len(set(self._page_numbers_by_id()) - index_page_ids)
            if missing_page_count:
                warnings.append(
                    "Stored-index search cannot cover every active page; "
                    f"{missing_page_count} pages have no `page_meta` row."
                )
        self._text_warnings_cache = [f"Library-wide: {warning}" for warning in warnings]
        return list(self._text_warnings_cache)

    def search_coverage(
        self,
        folder: str | Item = "/",
        source: str = "all",
        *,
        notebook: str | Item | None = None,
    ) -> dict[str, Any]:
        """Summarize stored text and mapping metadata for one resolved library scope."""
        self._require_open()
        self._ensure_consistent_graph()
        self._ensure_fts()
        source = source.casefold() if isinstance(source, str) else ""
        if source not in {"all", *text_index.TEXT_TABLES}:
            raise GoodnotesError("Coverage source must be all, handwriting, pdf, or textbox.")
        scope, _, notebook_ids = self._resolve_text_scope(folder, notebook)
        page_refs = self._page_refs_for_notebooks(notebook_ids)
        requested_sources = list(text_index.TEXT_TABLES) if source == "all" else [source]
        pdf_bindings = (
            self._pdf_bindings_for(page_refs)
            if "pdf" in requested_sources and self._fts is not None
            else None
        )
        details = text_index.coverage(
            self._fts,
            page_refs,
            requested_sources,
            schema=self._fts_schema,
            pdf_bindings=pdf_bindings,
        )
        mapping = details["page_meta"]
        source_details = details["sources"]
        warnings = list(details["warnings"])
        next_steps: list[str] = []

        if source in {"all", "pdf"}:
            if scope["type"] == "notebook":
                scope_option = "--notebook"
                scope_value = scope["path"]
            elif scope["folder_id"] is None:
                scope_option = "--folder"
                scope_value = "/"
            else:
                scope_option = "--folder"
                scope_value = scope["path"]
            scan_command = (
                f"goodnotes scan-pdfs 'QUERY' {scope_option} {shlex.quote(scope_value)} "
                "--page-limit 200"
            )
            next_steps.append(
                "To check copied PDF backgrounds in this scope, run `"
                f"{scan_command}`; replace `QUERY` with a phrase and continue with "
                "`--page-offset` if returned."
            )
        if not mapping["available"] or mapping["active_pages_without_metadata"]:
            next_steps.append("Refresh the read-only snapshot with `goodnotes sync`.")
        if mapping.get("stale_or_mismatched_pages", 0):
            next_steps.append("Refresh the read-only snapshot with `goodnotes sync`.")
        for source_details_row in source_details.values():
            if source_details_row["availability"] == "unavailable":
                next_steps.append("Refresh the read-only snapshot with `goodnotes sync`.")

        if source in {"all", "handwriting"}:
            recognition = self._recognition_status_for_page_ids(
                [page_ref.page_id for page_ref in page_refs]
            )
            pending_pages = sum(
                1 for status in recognition.values() if status.index_state == "pending"
            )
            if pending_pages:
                warnings.append(
                    f"Handwriting indexing is pending for {pending_pages} active pages in this "
                    "scope; handwriting text counts may be incomplete or outdated."
                )
                next_steps.append(
                    "After Goodnotes finishes indexing, refresh the read-only snapshot with "
                    "`goodnotes sync`."
                )

        warnings.append(
            "Stored-text counts describe this snapshot; source-index completeness cannot be "
            "confirmed from the available metadata."
        )
        if not next_steps:
            next_steps.append(
                "Use `goodnotes read` on selected pages to inspect stored text and its source."
            )
        return {
            "scope": scope,
            "selected_source": source,
            "active_pages": len(page_refs),
            "page_meta": mapping,
            "sources": source_details,
            "completeness": "unknown",
            "warnings": list(dict.fromkeys(warnings)),
            "next_steps": list(dict.fromkeys(next_steps))[:5],
        }

    def _page_refs_for_notebooks(
        self, notebook_ids: set[str], *, page_ids: set[str] | None = None
    ) -> list[PageRef]:
        """Read active page identities and current PDF mappings from the projection."""
        if not notebook_ids:
            return []
        cache_key = (
            tuple(sorted(notebook_ids)),
            tuple(sorted(page_ids)) if page_ids is not None else None,
        )
        cached = self._page_refs_cache.get(cache_key)
        if cached is not None:
            return list(cached)
        assert self._projection is not None
        page_columns = self._table_columns(self._projection, "pages")
        template_columns = (
            self._table_columns(self._projection, "templates")
            if self._table_exists(self._projection, "templates")
            else set()
        )
        has_projection_mapping = {"template_id", "id", "document_id"} <= page_columns and {
            "id",
            "document_id",
            "attachment_id",
            "page_number",
        } <= template_columns
        if has_projection_mapping:
            identity = "t.attachment_id AS attachment_id, t.page_number AS page_number"
            template_join = (
                "LEFT JOIN templates AS t ON t.id = p.template_id AND t.document_id = p.document_id"
            )
        else:
            identity = "NULL AS attachment_id, NULL AS page_number"
            template_join = ""
        order = (
            "p.document_id, p.position, p.id"
            if "position" in page_columns
            else "p.document_id, p.id"
        )
        requested_pages = sorted(page_ids) if page_ids is not None else []
        page_batches = (
            list(text_index.batches(requested_pages, 300)) if page_ids is not None else [None]
        )
        result: list[PageRef] = []
        for notebook_batch in text_index.id_batches(sorted(notebook_ids)):
            document_placeholders = ", ".join("?" for _ in notebook_batch)
            for page_batch in page_batches:
                page_filter = ""
                parameters: tuple[Any, ...] = tuple(notebook_batch)
                if page_batch is not None:
                    page_placeholders = ", ".join("?" for _ in page_batch)
                    page_filter = f"AND p.id IN ({page_placeholders})"
                    parameters += tuple(page_batch)
                rows = self._projection.execute(
                    f"""
                    SELECT p.id AS page_id, p.document_id, {identity}
                    FROM pages AS p
                    {template_join}
                    WHERE p.document_id IN ({document_placeholders})
                      AND COALESCE(p.deleted, 0) = 0 {page_filter}
                    ORDER BY {order}
                    """,
                    parameters,
                )
                for row in rows:
                    attachment_id = row["attachment_id"]
                    if not isinstance(attachment_id, str) or not attachment_id:
                        attachment_id = None
                    page_number = row["page_number"]
                    pdf_page_no = (
                        page_number
                        if isinstance(page_number, int)
                        and not isinstance(page_number, bool)
                        and page_number > 0
                        else None
                    )
                    result.append(
                        PageRef(
                            document_id=self._required_text(
                                row["document_id"], "pages.document_id"
                            ),
                            page_id=self._required_text(row["page_id"], "pages.id"),
                            attachment_id=attachment_id,
                            pdf_page_no=pdf_page_no,
                        )
                    )
        self._page_refs_cache[cache_key] = result
        return list(result)

    def _pdf_bindings_for(self, page_refs: list[PageRef]) -> text_index._PDFPageBindings:
        assert self._fts is not None
        key = tuple(page_refs)
        cached = self._pdf_bindings_cache.get(key)
        if cached is None:
            cached = text_index.pdf_page_bindings(self._fts, page_refs, schema=self._fts_schema)
            self._pdf_bindings_cache[key] = cached
        return cached

    @staticmethod
    def _selector_cache_key(selector: str | Item | None) -> tuple[str, str] | str | None:
        if isinstance(selector, Item):
            return (selector.kind, selector.id)
        return selector

    def _resolve_text_scope(
        self, folder: str | Item, notebook: str | Item | None
    ) -> tuple[dict[str, Any], list[Item], set[str]]:
        folder_key = self._selector_cache_key(folder)
        assert folder_key is not None
        cache_key = (folder_key, self._selector_cache_key(notebook))
        cached = self._scope_cache.get(cache_key)
        if cached is not None:
            scope, notebooks, notebook_ids = cached
            return dict(scope), list(notebooks), set(notebook_ids)
        if notebook is not None:
            try:
                parent_key = self._folder_key(folder, root_allowed=True)
            except GoodnotesError as exc:
                if "different kind" in str(exc):
                    raise GoodnotesError(
                        "Use --notebook for a notebook selector and --folder for a folder "
                        "selector; do not pass both scopes."
                    ) from None
                raise
            if parent_key is not None:
                raise GoodnotesError("Use either --notebook or a non-root --folder, not both.")
            item = self._resolve_notebook_selector(notebook, option="--notebook")
            result = (
                {
                    "type": "notebook",
                    "notebook_id": item.id,
                    "path": item.path,
                },
                [item],
                {item.id},
            )
            self._scope_cache[cache_key] = result
            return dict(result[0]), list(result[1]), set(result[2])

        try:
            parent_key = self._folder_key(folder, root_allowed=True)
        except GoodnotesError as exc:
            is_notebook = False
            try:
                self.resolve(folder, kind="notebook")
                is_notebook = True
            except GoodnotesError:
                pass
            if is_notebook or "different kind" in str(exc):
                raise GoodnotesError(
                    "The selector identifies a notebook; use --notebook for a notebook or "
                    "--folder for a folder."
                ) from None
            raise
        notebooks = [item for item in self.tree(folder) if item.kind == "notebook"]
        if parent_key is None:
            scope = {"type": "folder", "folder_id": None, "path": "/"}
        else:
            item = self._item_for_key(parent_key)
            scope = {"type": "folder", "folder_id": item.id, "path": item.path}
        result = (scope, notebooks, {item.id for item in notebooks})
        self._scope_cache[cache_key] = result
        return dict(scope), list(notebooks), set(result[2])

    def _resolve_notebook_selector(self, selector: str | Item, *, option: str) -> Item:
        try:
            return self.resolve(selector, kind="notebook")
        except GoodnotesError as exc:
            is_folder = False
            try:
                self.resolve(selector, kind="folder")
                is_folder = True
            except GoodnotesError:
                pass
            if is_folder or "different kind" in str(exc):
                raise GoodnotesError(
                    f"{option} identifies a folder; use --folder for a folder scope and "
                    "--notebook for a notebook."
                ) from None
            raise

    def search(
        self,
        query: str,
        folder: str | Item = "/",
        source: str = "all",
        limit: int = 20,
        *,
        notebook: str | Item | None = None,
    ) -> list[SearchHit]:
        """Find literal, case-folded substrings in ordinary SQLite content tables."""
        self._require_open()
        self._ensure_consistent_graph()
        if not isinstance(query, str) or not query:
            raise GoodnotesError("Search query must not be empty.")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise GoodnotesError("Search limit must be a positive integer.")
        source = source.casefold() if isinstance(source, str) else ""
        valid_sources = {"all", *text_index.TEXT_TABLES}
        if source not in valid_sources:
            raise GoodnotesError("Search source must be all, handwriting, pdf, or textbox.")
        _, _, notebook_ids = self._resolve_text_scope(folder, notebook)
        self._ensure_fts()
        if self._fts is None:
            raise TextIndexUnavailableError("The sandbox has no search index.")

        selected_sources = list(text_index.TEXT_TABLES) if source == "all" else [source]
        missing_sources = [
            item_source
            for item_source in selected_sources
            if not text_index.search_source_available(
                self._fts, item_source, schema=self._fts_schema
            )
        ]
        if source != "all" and missing_sources:
            label = text_index.TEXT_LABELS[source]
            raise TextIndexUnavailableError(f"{label} text is unavailable in this search index.")
        if source == "all" and len(missing_sources) == len(selected_sources):
            raise TextIndexUnavailableError(
                "The search index has no available text content tables."
            )
        if not text_index.page_meta_supports_search(self._fts, schema=self._fts_schema):
            raise TextIndexUnavailableError(
                "Search results cannot be mapped because usable page metadata is missing."
            )
        if not notebook_ids:
            return []

        available = [
            item_source for item_source in selected_sources if item_source not in missing_sources
        ]
        if not available:
            raise TextIndexUnavailableError(
                "The requested search index content table is unavailable."
            )
        pdf_bindings: text_index._PDFPageBindings | None = None
        if "pdf" in available:
            pdf_bindings = self._pdf_bindings_for(self._page_refs_for_notebooks(notebook_ids))
            if not pdf_bindings["available"]:
                if source == "pdf":
                    raise TextIndexUnavailableError(
                        "PDF text cannot be searched because full page identity mapping is "
                        "unavailable in this search index."
                    )
                available.remove("pdf")
            elif (
                source == "pdf"
                and pdf_bindings["pages"]
                and not any(
                    binding["state"] != "unknown" for binding in pdf_bindings["pages"].values()
                )
            ):
                raise TextIndexUnavailableError(
                    "PDF text cannot be searched because active pages have no verifiable "
                    "identity mapping in this search index."
                )
        if not available:
            raise TextIndexUnavailableError("The requested text index mappings are unavailable.")

        source_order = {"handwriting": 0, "pdf": 1, "textbox": 2}
        folded_query = query.casefold()
        candidates: list[_SearchCandidate] = []
        retained_keys: set[tuple[str, str, int, bytes]] = set()
        sequence = 0
        notebook_batches = list(text_index.id_batches(sorted(notebook_ids)))
        # Each source cursor orders by text_id, and a page/source fixes the other
        # sort fields. Thus a later exact duplicate can never outrank its first row;
        # keys for candidates outside the retained top-K set need not be kept.
        for item_source in available:
            if item_source == "pdf":
                assert pdf_bindings is not None
                rows = (
                    row
                    for notebook_batch in notebook_batches
                    for row in text_index.search_pdf_rows(
                        self._fts, pdf_bindings, folded_query, notebook_batch
                    )
                )
            else:
                rows = (
                    row
                    for notebook_batch in notebook_batches
                    for row in self._search_rows(item_source, folded_query, notebook_batch)
                )
            for row in rows:
                document_id = row["document_id"]
                page_id = row["page_id"]
                page_number = self._page_number_for_id(document_id, page_id)
                if page_number is None:
                    continue
                entry = self._entries.get(("notebook", document_id))
                path = self._paths.get(("notebook", document_id))
                if entry is None or path is None:
                    continue
                text = row["text"]
                digest = hashlib.blake2b(
                    text.encode("utf-8", errors="surrogatepass"), digest_size=16
                ).digest()
                duplicate_key = (page_id, item_source, len(text), digest)
                if duplicate_key in retained_keys:
                    continue
                sort_key = (
                    path.casefold(),
                    page_number,
                    source_order[item_source],
                    int(row["sort_id"] or 0),
                )
                candidate = _SearchCandidate(
                    sort_key=sort_key,
                    sequence=sequence,
                    duplicate_key=duplicate_key,
                    hit=SearchHit(
                        document_id=document_id,
                        path=path,
                        page=page_number,
                        page_id=page_id,
                        source=item_source,
                        text=text,
                    ),
                )
                sequence += 1
                if len(candidates) < limit:
                    heapq.heappush(candidates, candidate)
                    retained_keys.add(duplicate_key)
                elif (candidate.sort_key, candidate.sequence) < (
                    candidates[0].sort_key,
                    candidates[0].sequence,
                ):
                    evicted = heapq.heapreplace(candidates, candidate)
                    retained_keys.remove(evicted.duplicate_key)
                    retained_keys.add(duplicate_key)
        return [
            candidate.hit
            for candidate in sorted(candidates, key=lambda x: (x.sort_key, x.sequence))
        ]

    def scan_pdfs(
        self,
        query: str,
        folder: str | Item = "/",
        *,
        limit: int = 20,
        page_limit: int = 200,
        page_offset: int = 0,
        notebook: str | Item | None = None,
    ) -> dict[str, Any]:
        """Search copied PDF backgrounds with an operation-scoped document cache."""
        with self._pdf_session():
            return self._scan_pdfs(
                query,
                folder,
                limit=limit,
                page_limit=page_limit,
                page_offset=page_offset,
                notebook=notebook,
            )

    def _scan_pdfs(
        self,
        query: str,
        folder: str | Item = "/",
        *,
        limit: int = 20,
        page_limit: int = 200,
        page_offset: int = 0,
        notebook: str | Item | None = None,
    ) -> dict[str, Any]:
        """Search copied PDF backgrounds in a bounded window of notebook pages."""
        self._require_open()
        self._ensure_consistent_graph()
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

        scope, notebooks, _ = self._resolve_text_scope(folder, notebook)
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
        for notebook_item, page in self._iter_page_window(
            notebooks, offset=page_offset, count=window_pages
        ):
            pages_scanned += 1
            text, extraction_warning, extraction_truncated = self._extract_pdf_fallback(page)
            issue: str | None = None
            attachment_state, _ = self._attachment_state_by_page.get(
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
            if extraction_truncated or (
                extraction_warning is not None and "truncated at" in extraction_warning
            ):
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

    def _iter_page_window(
        self, notebooks: list[Item], *, offset: int, count: int
    ) -> Iterator[tuple[Item, Page]]:
        """Hydrate only pages in a bounded scan window, skipping by catalog counts."""
        if count <= 0:
            return
        assert self._projection is not None
        skipped = offset
        remaining = count
        for notebook in notebooks:
            if remaining <= 0:
                break
            if skipped >= notebook.page_count:
                skipped -= notebook.page_count
                continue
            local_offset = skipped
            skipped = 0
            fetch_count = min(remaining, notebook.page_count - local_offset)
            rows = self._projection.execute(
                """
                SELECT id, document_id, template_id, position, group_id
                FROM pages
                WHERE document_id = ? AND COALESCE(deleted, 0) = 0
                ORDER BY position, id
                LIMIT ? OFFSET ?
                """,
                (notebook.id, fetch_count, local_offset),
            ).fetchall()
            pages = self._hydrate_page_rows(
                rows,
                numbers=list(range(local_offset + 1, local_offset + len(rows) + 1)),
            )
            for page in pages:
                yield notebook, page
            remaining -= len(rows)

    def stats(self) -> dict[str, Any]:
        """Return counts and capability warnings suitable for a doctor/info command."""
        self._require_open()
        self._ensure_fts()
        assert self._projection is not None
        active_folders = self._active_folder_count
        active_notebooks = self._active_notebook_count
        visible_folders = sum(
            1 for key, path in self._paths.items() if key[0] == "folder" and path is not None
        )
        visible_notebooks = sum(
            1 for key, path in self._paths.items() if key[0] == "notebook" and path is not None
        )
        page_count = self._count(self._projection, "pages", "COALESCE(deleted, 0) = 0")
        deleted_folders = self._count(self._projection, "folders", "COALESCE(deleted, 0) <> 0")
        deleted_notebooks = self._count(self._projection, "documents", "COALESCE(deleted, 0) <> 0")
        deleted_link_rows = self._count(
            self._projection,
            "folder_to_folder_items",
            "COALESCE(deleted, 0) <> 0",
        )
        fts_tables = []
        if self._fts is not None:
            fts_tables = [
                name
                for name in (
                    "annotation_text_content",
                    "textbox_text_content",
                    "pdf_text_content",
                )
                if self._table_exists(self._fts, name)
            ]
        warnings: list[str] = []
        if self._fts is None:
            warnings.append("Search index file is missing from the sandbox.")
        elif len(fts_tables) != 3:
            warnings.append("One or more ordinary text content tables are missing from the index.")
        if self._orphan_count:
            warnings.append(f"{self._orphan_count} active items have no resolvable root path.")
        if self._broken_link_count:
            warnings.append(
                f"{self._broken_link_count} active catalog links point to missing "
                "targets or parents."
            )
        if self._duplicate_parent_count:
            warnings.append(
                f"{self._duplicate_parent_count} active items have multiple parent links."
            )
        if self._cycle_count:
            warnings.append(f"{self._cycle_count} active folders are part of a parent cycle.")
        if self._links_to_deleted_count:
            warnings.append(
                f"{self._links_to_deleted_count} active links to deleted items were ignored."
            )
        page_meta_available = text_index.page_meta_supports_search(
            self._fts, schema=self._fts_schema
        )
        page_meta_ids: set[str] = set()
        if self._fts is not None and page_meta_available:
            page_meta_ids = {
                row[0] for row in self._fts.execute("SELECT page_id FROM page_meta").fetchall()
            }
        if page_meta_available:
            active_page_ids = set(self._page_numbers_by_id())
            pages_without_metadata: int | None = len(active_page_ids - page_meta_ids)
            metadata_without_pages: int | None = len(page_meta_ids - active_page_ids)
            if pages_without_metadata:
                warnings.append(
                    f"{pages_without_metadata} active pages have no index metadata row."
                )
            if metadata_without_pages:
                warnings.append(
                    f"{metadata_without_pages} index metadata rows have no active page."
                )
        else:
            pages_without_metadata = None
            metadata_without_pages = None
            warnings.append("Page-to-index metadata coverage is unavailable for this snapshot.")
        return {
            "active_folders": active_folders,
            "active_notebooks": active_notebooks,
            "active_pages": page_count,
            "deleted_folders": deleted_folders,
            "deleted_notebooks": deleted_notebooks,
            "visible_folders": visible_folders,
            "visible_notebooks": visible_notebooks,
            "orphaned_items": self._orphan_count,
            "broken_links": self._broken_link_count,
            "deleted_link_rows": deleted_link_rows,
            "links_to_deleted_items": self._links_to_deleted_count,
            "duplicate_parent_items": self._duplicate_parent_count,
            "cycle_folders": self._cycle_count,
            "pages_without_index_metadata": pages_without_metadata,
            "index_metadata_without_active_pages": metadata_without_pages,
            "search_index_available": bool(
                self._fts is not None and fts_tables and page_meta_available
            ),
            "search_content_tables": fts_tables,
            "warnings": warnings,
        }

    def _require_open(self) -> None:
        if self._stack is None or self._projection is None:
            raise GoodnotesError("Open Library in a `with` block before reading it.")

    def _ensure_fts(self) -> sqlite3.Connection | None:
        """Open the optional text index only when a text or diagnostic operation needs it."""
        if self._fts_attempted:
            return self._fts
        self._require_open()
        self._fts_attempted = True
        if self.sandbox.fts_path is None:
            return None
        fts_path = Path(self.sandbox.fts_path)
        if not fts_path.is_file():
            return None
        assert self._stack is not None
        try:
            self._fts = self._stack.enter_context(open_database(fts_path))
        except Exception:
            self._fts_attempted = False
            raise
        self._fts_schema = text_index.SchemaCache(self._fts)
        self._register_casefold()
        return self._fts

    def _register_casefold(self) -> None:
        if self._fts is not None:
            self._fts.create_function(
                "casefold",
                1,
                lambda value: value.casefold() if isinstance(value, str) else "",
                deterministic=True,
            )

    def _schema_for(self, connection: sqlite3.Connection) -> text_index.SchemaCache:
        if connection is self._projection and self._projection_schema is not None:
            return self._projection_schema
        if connection is self._fts and self._fts_schema is not None:
            return self._fts_schema
        return text_index.SchemaCache(connection)

    def _table_exists(self, connection: sqlite3.Connection | None, name: str) -> bool:
        if connection is None:
            return False
        return text_index.table_exists(connection, name, schema=self._schema_for(connection))

    def _table_columns(self, connection: sqlite3.Connection, name: str) -> set[str]:
        return text_index.table_columns(connection, name, schema=self._schema_for(connection))

    def _load_catalog(self) -> None:
        assert self._projection is not None
        projection = self._projection
        required = ("folders", "documents", "folder_to_folder_items", "pages")
        if any(not self._table_exists(projection, name) for name in required):
            raise GoodnotesError(
                "The projection database does not contain the expected library tables."
            )

        required_columns = {
            "folders": {"id", "name", "updated_at", "deleted", "position"},
            "documents": {"id", "name", "updated_at", "deleted", "position"},
            "folder_to_folder_items": {
                "id",
                "item_type",
                "item_id",
                "parent_folder_id",
                "root_folder_id",
                "item_position",
                "deleted",
            },
            "pages": {"id", "document_id", "template_id", "position", "group_id", "deleted"},
        }
        for table, expected in required_columns.items():
            missing = expected - self._table_columns(projection, table)
            if missing:
                raise GoodnotesError(
                    f"Projection database table `{table}` is missing required columns: "
                    f"{', '.join(sorted(missing))}."
                )

        folder_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM folders"
        ).fetchall()
        document_rows = projection.execute(
            "SELECT id, name, updated_at, deleted, position FROM documents"
        ).fetchall()
        folder_ids: set[str] = set()
        document_ids: set[str] = set()
        live_folders: dict[str, sqlite3.Row] = {}
        live_documents: dict[str, sqlite3.Row] = {}
        for kind, rows, identifiers, active in (
            ("folders", folder_rows, folder_ids, live_folders),
            ("documents", document_rows, document_ids, live_documents),
        ):
            for row in rows:
                item_id = self._required_text(row["id"], f"{kind}.id")
                identifiers.add(item_id)
                if self._deleted_flag(row["deleted"], f"{kind}.deleted"):
                    continue
                self._required_text(row["name"], f"{kind}.name")
                self._finite_number(row["updated_at"], f"{kind}.updated_at")
                self._required_text(row["position"], f"{kind}.position", allow_empty=True)
                active[item_id] = row
        self._active_items = {
            **{("folder", item_id): row["name"] for item_id, row in live_folders.items()},
            **{("notebook", item_id): row["name"] for item_id, row in live_documents.items()},
        }
        page_counts = {
            self._required_text(row["document_id"], "pages.document_id"): int(row["page_count"])
            for row in projection.execute(
                """
                SELECT document_id, count(*) AS page_count
                FROM pages
                WHERE COALESCE(deleted, 0) = 0
                GROUP BY document_id
                """
            )
            if row["document_id"] in live_documents
        }
        self._page_numbers = None

        active_rows = projection.execute(
            """
            SELECT item_type, item_id, parent_folder_id, root_folder_id, item_position
            FROM folder_to_folder_items
            WHERE COALESCE(deleted, 0) = 0
            ORDER BY item_position, id
            """
        ).fetchall()
        self._links = {}
        self._children = {}
        self._entries = {}
        self._duplicate_parent_count = 0
        self._broken_link_count = 0
        self._links_to_deleted_count = 0
        self._active_folder_count = len(live_folders)
        self._active_notebook_count = len(live_documents)
        for row in active_rows:
            if row["item_type"] == _FOLDER:
                kind, live_rows = "folder", live_folders
            elif row["item_type"] == _NOTEBOOK:
                kind, live_rows = "notebook", live_documents
            else:
                self._broken_link_count += 1
                continue
            item_id = row["item_id"]
            if not isinstance(item_id, str) or not item_id or item_id not in live_rows:
                identifiers = folder_ids if kind == "folder" else document_ids
                if item_id in identifiers:
                    self._links_to_deleted_count += 1
                else:
                    self._broken_link_count += 1
                continue
            key = (kind, item_id)
            self._links.setdefault(key, []).append(row)

        self._duplicate_parent_count = sum(1 for rows in self._links.values() if len(rows) > 1)
        self._entries = {}
        for key, rows in self._links.items():
            if len(rows) != 1:
                # Keep the item in the diagnostic catalog, but never select a parent by order.
                row = rows[0]
                parent_id, is_root = None, False
            else:
                row = rows[0]
                raw_parent = row["parent_folder_id"]
                root_id = row["root_folder_id"]
                if raw_parent is not None and not isinstance(raw_parent, str):
                    raise GoodnotesError(
                        "Projection database has an invalid folder_to_folder_items."
                        "parent_folder_id value; expected text or NULL."
                    )
                if root_id is not None and not isinstance(root_id, str):
                    raise GoodnotesError(
                        "Projection database has an invalid folder_to_folder_items."
                        "root_folder_id value; expected text or NULL."
                    )
                parent_is_root = (
                    raw_parent is None
                    or raw_parent == ""
                    or (raw_parent == root_id and raw_parent not in live_folders)
                )
                if parent_is_root:
                    parent_id, is_root = None, True
                elif raw_parent in live_folders:
                    parent_id, is_root = raw_parent, False
                else:
                    parent_id, is_root = None, False
                    self._broken_link_count += 1
            kind, item_id = key
            record = live_folders[item_id] if kind == "folder" else live_documents[item_id]
            link_position = row["item_position"]
            if link_position is not None and not isinstance(link_position, str):
                raise GoodnotesError(
                    "Projection database has an invalid folder_to_folder_items.item_position "
                    "value; expected text or NULL."
                )
            position = link_position or record["position"]
            self._entries[key] = _Entry(
                kind=kind,
                id=item_id,
                name=record["name"],
                parent_id=parent_id,
                is_root=is_root,
                position=position,
                updated_at=self._finite_number(record["updated_at"], f"{kind}.updated_at"),
                page_count=page_counts.get(item_id, 0) if kind == "notebook" else 0,
            )
            if len(rows) == 1 and (is_root or parent_id is not None):
                self._children.setdefault(parent_id_key(parent_id, is_root), []).append(key)

        for children in self._children.values():
            children.sort(key=lambda key: (self._entries[key].position, key[1]))

        self._paths = {}
        self._cycle_count = self._count_folder_cycles()
        self._orphan_count = len(live_folders) + len(live_documents) - len(self._entries)
        for key in self._entries:
            path = self._path_for_key(key, set())
            self._paths[key] = path
            if path is None:
                self._orphan_count += 1

    def _count_folder_cycles(self) -> int:
        state: dict[str, int] = {}
        cycle_members: set[str] = set()
        stack: list[str] = []

        def visit(folder_id: str) -> None:
            mark = state.get(folder_id, 0)
            if mark == 2:
                return
            if mark == 1:
                try:
                    cycle_start = stack.index(folder_id)
                    cycle_members.update(stack[cycle_start:])
                except ValueError:
                    cycle_members.add(folder_id)
                return
            state[folder_id] = 1
            stack.append(folder_id)
            key = ("folder", folder_id)
            entry = self._entries.get(key)
            if entry is not None and entry.parent_id is not None:
                visit(entry.parent_id)
            stack.pop()
            state[folder_id] = 2

        for kind, folder_id in self._entries:
            if kind == "folder":
                visit(folder_id)
        return len(cycle_members)

    def _path_for_key(
        self,
        key: tuple[str, str],
        visiting: set[tuple[str, str]],
    ) -> str | None:
        if key in self._paths:
            return self._paths[key]
        if key in visiting:
            return None
        entry = self._entries.get(key)
        links = self._links.get(key, [])
        if entry is None or len(links) != 1:
            return None
        visiting.add(key)
        path: str | None
        if entry.is_root:
            path = entry.name
        elif entry.parent_id is not None:
            parent_path = self._path_for_key(("folder", entry.parent_id), visiting)
            path = None if parent_path is None else _join_path(parent_path, entry.name)
        else:
            path = None
        visiting.remove(key)
        return path

    def _ensure_consistent_graph(self) -> None:
        if self._duplicate_parent_count:
            raise GoodnotesError(
                "The library has items with multiple parent links; use `stats()` to inspect it."
            )
        if self._cycle_count:
            raise GoodnotesError("The library has a folder cycle; use `stats()` to inspect it.")

    def _validate_kind(self, kind: str | None) -> str | None:
        if kind is None:
            return None
        if not isinstance(kind, str):
            raise GoodnotesError("Item kind must be `folder` or `notebook`.")
        normalized = kind.casefold()
        if normalized not in {"folder", "notebook"}:
            raise GoodnotesError("Item kind must be `folder` or `notebook`.")
        return normalized

    def _resolved_item(self, matches: list[tuple[str, str]]) -> Item:
        if len(matches) > 1:
            raise GoodnotesError("That selector is ambiguous; use a full path or exact UUID.")
        key = matches[0]
        if self._paths.get(key) is None:
            raise GoodnotesError("That active item has no resolvable path in the library tree.")
        return self._item_for_key(key)

    def _folder_key(
        self,
        folder: str | Item,
        root_allowed: bool,
    ) -> tuple[str, str] | None:
        if isinstance(folder, Item):
            folder = folder.id
        if isinstance(folder, str) and folder in {"", "/"}:
            if root_allowed:
                return None
            raise GoodnotesError("The library root is not an item.")
        item = self.resolve(folder, kind="folder")
        return ("folder", item.id)

    def _item_for_key(self, key: tuple[str, str]) -> Item:
        entry = self._entries[key]
        path = self._paths.get(key)
        if path is None:
            raise GoodnotesError("That active item has no resolvable path in the library tree.")
        return Item(
            id=entry.id,
            name=entry.name,
            kind=entry.kind,
            path=path,
            parent_id=entry.parent_id,
            page_count=entry.page_count,
            updated_at=entry.updated_at,
        )

    @staticmethod
    def _normalize_path_selector(selector: str) -> str:
        parts = selector.split("/")
        if any(part in {".", ".."} for part in parts):
            raise GoodnotesError(
                "Relative `.` and `..` path segments are not valid item selectors."
            )
        return selector.strip("/")

    def _hydrate_page_rows(
        self, rows: list[sqlite3.Row], *, numbers: list[int] | None = None
    ) -> list[Page]:
        """Hydrate a bounded page set with batched template, note, and attachment reads."""
        if not rows:
            return []
        assert self._projection is not None
        if numbers is None:
            numbers = list(range(1, len(rows) + 1))
        if len(numbers) != len(rows):
            raise GoodnotesError("Page hydration received inconsistent page numbers.")

        missing: list[sqlite3.Row] = []
        number_by_page: dict[tuple[str, str], int] = {}
        for row, number in zip(rows, numbers, strict=True):
            document_id = self._required_text(row["document_id"], "pages.document_id")
            page_id = self._required_text(row["id"], "pages.id")
            number_by_page[(document_id, page_id)] = number
            if (document_id, page_id) not in self._page_cache:
                missing.append(row)

        templates: dict[tuple[str, str], sqlite3.Row] = {}
        template_ids = sorted(
            {
                row["template_id"]
                for row in missing
                if isinstance(row["template_id"], str) and row["template_id"]
            }
        )
        if template_ids and self._table_exists(self._projection, "templates"):
            for batch in text_index.id_batches(template_ids):
                placeholders = ", ".join("?" for _ in batch)
                for template in self._projection.execute(
                    f"""
                    SELECT id, document_id, attachment_id, page_number, size_width, size_height
                    FROM templates WHERE id IN ({placeholders}) ORDER BY id
                    """,
                    batch,
                ):
                    key = (template["document_id"], template["id"])
                    templates.setdefault(key, template)

        attachment_ids = sorted(
            {
                template["attachment_id"]
                for template in templates.values()
                if isinstance(template["attachment_id"], str) and template["attachment_id"]
            }
        )
        attachments: dict[str, sqlite3.Row] = {}
        if attachment_ids and self._table_exists(self._projection, "attachments"):
            for batch in text_index.id_batches(attachment_ids):
                placeholders = ", ".join("?" for _ in batch)
                for attachment in self._projection.execute(
                    f"SELECT id, path FROM attachments WHERE id IN ({placeholders}) ORDER BY id",
                    batch,
                ):
                    attachments.setdefault(attachment["id"], attachment)

        group_ids = sorted(
            {
                row["group_id"]
                for row in missing
                if isinstance(row["group_id"], str) and row["group_id"]
            }
        )
        annotation_counts: dict[str, int] = {}
        if group_ids and self._table_exists(self._projection, "notes_items"):
            for batch in text_index.id_batches(group_ids):
                placeholders = ", ".join("?" for _ in batch)
                annotation_counts.update(
                    {
                        result["group_id"]: int(result["annotation_count"])
                        for result in self._projection.execute(
                            f"""
                            SELECT group_id, count(*) AS annotation_count
                            FROM notes_items
                            WHERE group_id IN ({placeholders})
                              AND COALESCE(deleted, 0) = 0
                            GROUP BY group_id
                            """,
                            batch,
                        )
                    }
                )

        for row in missing:
            page_id = self._required_text(row["id"], "pages.id")
            document_id = self._required_text(row["document_id"], "pages.document_id")
            position = self._required_text(row["position"], "pages.position", allow_empty=True)
            template_id = row["template_id"]
            template = (
                templates.get((document_id, template_id))
                if isinstance(template_id, str) and template_id
                else None
            )
            attachment_id = template["attachment_id"] if template is not None else None
            attachment_row = (
                attachments.get(attachment_id)
                if isinstance(attachment_id, str) and attachment_id
                else None
            )
            if not attachment_id:
                attachment_path, attachment_state = None, "unmapped"
            elif attachment_row is None:
                attachment_path, attachment_state = None, "missing"
            else:
                raw_attachment_path = attachment_row["path"]
                attachment_path, attachment_state = self._resolve_attachment_path(
                    raw_attachment_path
                )
                if (
                    attachment_path is None
                    and attachment_state == "missing"
                    and isinstance(raw_attachment_path, str)
                    and raw_attachment_path
                    and not Path(raw_attachment_path).is_absolute()
                    and ".." not in Path(raw_attachment_path).parts
                ):
                    # Keep a safe relative reference so document rendering can report that
                    # its mapped file is missing, while re-validating it at the read boundary.
                    attachment_path = Path(raw_attachment_path)
            self._attachment_state_by_page[(document_id, page_id)] = (
                attachment_state,
                attachment_id if isinstance(attachment_id, str) else None,
            )

            raw_page = template["page_number"] if template is not None else None
            pdf_page = (
                raw_page if isinstance(raw_page, int) and not isinstance(raw_page, bool) else None
            )
            if pdf_page is not None and pdf_page < 1:
                pdf_page = None
            width = self._optional_dimension(
                template["size_width"] if template is not None else None
            )
            height = self._optional_dimension(
                template["size_height"] if template is not None else None
            )
            group_id = row["group_id"]
            if group_id is not None and not isinstance(group_id, str):
                raise GoodnotesError("Projection database has an invalid pages.group_id value.")
            page = Page(
                id=page_id,
                document_id=document_id,
                number=number_by_page[(document_id, page_id)],
                position=position,
                attachment_path=str(attachment_path) if attachment_path is not None else None,
                pdf_page=pdf_page,
                width=width,
                height=height,
                annotation_count=annotation_counts.get(group_id, 0) if group_id else 0,
            )
            self._page_cache[(document_id, page_id)] = page

        result: list[Page] = []
        for row, number in zip(rows, numbers, strict=True):
            document_id = self._required_text(row["document_id"], "pages.document_id")
            page_id = self._required_text(row["id"], "pages.id")
            page = self._page_cache[(document_id, page_id)]
            if page.number != number:
                page = Page(
                    id=page.id,
                    document_id=page.document_id,
                    number=number,
                    position=page.position,
                    attachment_path=page.attachment_path,
                    pdf_page=page.pdf_page,
                    width=page.width,
                    height=page.height,
                    annotation_count=page.annotation_count,
                    rotation=page.rotation,
                )
                self._page_cache[(document_id, page_id)] = page
            result.append(page)
        return result

    @staticmethod
    def _required_text(value: Any, field: str, *, allow_empty: bool = False) -> str:
        if not isinstance(value, str) or (not allow_empty and not value):
            suffix = "text" if allow_empty else "non-empty text"
            raise GoodnotesError(f"Projection database has an invalid {field}; expected {suffix}.")
        return value

    @staticmethod
    def _finite_number(value: Any, field: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected a finite number."
            )
        result = float(value)
        if not math.isfinite(result):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected a finite number."
            )
        return result

    @staticmethod
    def _deleted_flag(value: Any, field: str) -> bool:
        if value is None:
            return False
        if not isinstance(value, int) or isinstance(value, bool):
            raise GoodnotesError(
                f"Projection database has an invalid {field}; expected an integer flag or NULL."
            )
        return value != 0

    @staticmethod
    def _optional_dimension(value: Any) -> float | None:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        result = float(value)
        return result if math.isfinite(result) else None

    def _resolve_attachment_path(
        self, raw_path: Any, *, for_read: bool = False
    ) -> tuple[Path | None, str]:
        """Resolve one stored reference, memoized within its pinned snapshot view.

        Metadata hydration reuses results for repeated attachment references. A PDF
        read uses a separate operation cache so the path is revalidated before it opens.
        """
        cache = (
            self._pdf_attachment_resolution_cache if for_read else self._attachment_resolution_cache
        )
        if isinstance(raw_path, str) and cache is not None:
            cached = cache.get(raw_path)
            if cached is not None:
                return cached

        result = self._resolve_attachment_path_uncached(raw_path)
        if isinstance(raw_path, str) and cache is not None:
            cache[raw_path] = result
        return result

    def _resolve_attachment_path_uncached(self, raw_path: Any) -> tuple[Path | None, str]:
        """Resolve a stored attachment without losing missing or unsafe diagnostics."""
        if not isinstance(raw_path, str) or not raw_path:
            return None, "missing"
        try:
            root = Path(self.sandbox.attachments_dir).resolve()
            stored_path = Path(raw_path)
        except (OSError, RuntimeError, ValueError):
            return None, "unavailable"
        if not stored_path.is_absolute() and ".." in stored_path.parts:
            return None, "unsafe"
        candidate = stored_path if stored_path.is_absolute() else root / stored_path
        try:
            resolved = resolve_confined_file(root, candidate)
        except GoodnotesError:
            return None, "unsafe"
        except OSError:
            return None, "unavailable"
        if resolved is not None:
            return resolved, "available"
        if stored_path.is_absolute() or not stored_path.name:
            return None, "missing"
        matches = self._attachment_paths_by_basename().get(stored_path.name, [])
        if len(matches) == 1:
            return matches[0], "available"
        return None, "ambiguous" if matches else "missing"

    def _attachment_paths_by_basename(self) -> dict[str, list[Path]]:
        if self._attachment_basename_cache is not None:
            return self._attachment_basename_cache
        root = Path(self.sandbox.attachments_dir).resolve()
        paths: dict[str, list[Path]] = {}
        for candidate in root.rglob("*"):
            try:
                resolved = resolve_confined_file(root, candidate)
            except (GoodnotesError, OSError, RuntimeError, ValueError):
                continue
            if resolved is not None:
                paths.setdefault(candidate.name, []).append(resolved)
        self._attachment_basename_cache = paths
        return paths

    def _extract_pdf_fallback(
        self, page: Page, *, max_chars: int | None = None
    ) -> tuple[str | None, str | None, bool]:
        """Extract one mapped PDF page from a safe copied attachment, with size bounds."""
        if not page.attachment_path:
            status, _ = self._attachment_state_by_page.get(
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
        safe_path, status = self._resolve_attachment_path(page.attachment_path, for_read=True)
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
            with self._pdf_document(safe_path) as pdf:
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

    @contextmanager
    def _pdf_session(self) -> Generator[None, None, None]:
        if self._pdf_documents is None:
            self._pdf_documents = _PDFDocumentCache()
        if self._pdf_attachment_resolution_cache is None:
            self._pdf_attachment_resolution_cache = {}
        yield

    @contextmanager
    def _pdf_document(self, path: Path) -> Generator[Any, None, None]:
        if self._pdf_documents is not None:
            yield self._pdf_documents.get(path)
            return
        document = _pymupdf().open(path)
        try:
            yield document
        finally:
            document.close()

    def _read_pdf_text_for_page(
        self, page_ref: PageRef, budget: int | None
    ) -> tuple[list[TextPart], int | None, bool, bool, str | None]:
        """Read PDF index rows that match the projection-derived page identity."""
        self._ensure_fts()
        if self._fts is None or not self._table_exists(self._fts, "pdf_text_content"):
            return [], budget, False, False, None
        binding_result = self._pdf_bindings_for([page_ref])
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

        assert self._fts is not None
        parts: list[TextPart] = []
        found = False

        def validate_metadata(row: sqlite3.Row) -> tuple[int, int]:
            text_id = row["text_id"]
            if (
                not isinstance(text_id, int)
                or isinstance(text_id, bool)
                or row["text_id_type"] != "integer"
            ):
                raise GoodnotesError(
                    "Search index has an invalid `pdf_text_content.text_id`; expected an integer."
                )
            if row["text_type"] != "text":
                raise GoodnotesError(
                    "Search index has an invalid `pdf_text_content.text` value; expected text."
                )
            text_length = row["text_length"]
            if not isinstance(text_length, int) or isinstance(text_length, bool) or text_length < 0:
                raise GoodnotesError(
                    "Search index has an invalid `pdf_text_content.text` length; "
                    "expected a non-negative integer."
                )
            return text_id, text_length

        for batch in text_index.batches(text_ids):
            placeholders = ", ".join("?" for _ in batch)
            if budget == 0:
                row = self._fts.execute(
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
                    validate_metadata(row)
                    return parts, budget, True, True, mapping_warning
                continue

            rows = self._fts.execute(
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
                _, text_length = validate_metadata(row)
                if budget is not None and row["contains_nul"]:
                    raise GoodnotesError(
                        "Search index has embedded NUL in `pdf_text_content.text`; "
                        "a bounded read cannot preserve this value."
                    )
                text = row["bounded_text"]
                if not isinstance(text, str):
                    raise GoodnotesError(
                        "Search index has an invalid `pdf_text_content.text` value; expected text."
                    )
                available = budget
                part_truncated = False
                if available is not None:
                    part_truncated = text_length > available or len(text) > available
                    if part_truncated:
                        text = text[:available]
                if text:
                    parts.append(TextPart(source="pdf", text=text, truncated=part_truncated))
                if budget is not None:
                    budget = max(0, budget - len(text))
                if part_truncated:
                    return parts, budget, True, found, mapping_warning
                if budget == 0 and row["row_number"] < row["row_count"]:
                    return parts, budget, True, found, mapping_warning
        return parts, budget, False, found, mapping_warning

    def _notebook_ids_under(self, parent_key: tuple[str, str] | None) -> set[str]:
        ids: set[str] = set()

        def visit(key: tuple[str, str] | None) -> None:
            for child_key in self._children.get(key, []):
                if child_key[0] == "notebook":
                    if self._paths.get(child_key) is not None:
                        ids.add(child_key[1])
                elif child_key[0] == "folder":
                    visit(child_key)

        visit(parent_key)
        return ids

    def _search_rows(
        self, source: str, query: str, document_ids: list[str] | None = None
    ) -> Iterator[sqlite3.Row]:
        """Search handwriting or textbox rows for a bounded notebook-id set."""
        assert self._fts is not None
        if document_ids is None:
            document_ids = sorted(self._notebook_ids_under(None))
        if len(document_ids) > text_index.SQL_BIND_LIMIT:
            for batch in text_index.id_batches(document_ids):
                yield from self._search_rows(source, query, batch)
            return
        if not document_ids or not text_index.page_meta_supports_search(
            self._fts, schema=self._fts_schema
        ):
            return
        if source not in {"handwriting", "textbox"}:
            return
        table = text_index.TEXT_TABLES[source]
        if not self._table_exists(self._fts, table) or not {
            "page_id",
            "text_id",
            "text",
        } <= self._table_columns(self._fts, table):
            return
        alias = "a" if source == "handwriting" else "t"
        scope = ", ".join("?" for _ in document_ids)
        page_meta_columns = self._table_columns(self._fts, "page_meta")
        mapping_filter = text_index.page_meta_live_filter(page_meta_columns, alias="p")
        yield from self._fts.execute(
            f"""
            SELECT p.document_id, {alias}.page_id, {alias}.text,
                   {alias}.text_id AS sort_id
            FROM {table} AS {alias}
            JOIN page_meta AS p ON p.page_id = {alias}.page_id
            WHERE p.document_id IN ({scope}) {mapping_filter}
              AND instr(casefold({alias}.text), ?) > 0
            ORDER BY {alias}.text_id
            """,
            (*document_ids, query),
        )

    def _page_number_for_id(self, document_id: str, page_id: str) -> int | None:
        self._ensure_page_numbers()
        assert self._page_numbers is not None
        return self._page_numbers.get((document_id, page_id))

    def _page_numbers_by_id(self) -> dict[str, int]:
        """Return active page ids for checking page-index metadata consistency."""
        self._ensure_page_numbers()
        assert self._page_numbers is not None
        return {page_id: number for (_, page_id), number in self._page_numbers.items()}

    def _ensure_page_numbers(self) -> None:
        if self._page_numbers is not None:
            return
        assert self._projection is not None
        page_numbers: dict[tuple[str, str], int] = {}
        counts: dict[str, int] = {}
        notebook_ids = self._active_items_by_kind("notebook")
        for row in self._projection.execute(
            """
            SELECT id, document_id
            FROM pages
            WHERE COALESCE(deleted, 0) = 0
            ORDER BY document_id, position, id
            """
        ):
            page_id = self._required_text(row["id"], "pages.id")
            document_id = self._required_text(row["document_id"], "pages.document_id")
            if document_id not in notebook_ids:
                continue
            number = counts.get(document_id, 0) + 1
            counts[document_id] = number
            page_numbers[(document_id, page_id)] = number
        self._page_numbers = page_numbers

    def _active_items_by_kind(self, kind: str) -> set[str]:
        return {item_id for item_kind, item_id in self._active_items if item_kind == kind}

    def _count(self, connection: sqlite3.Connection, table: str, where: str) -> int:
        if not self._table_exists(connection, table):
            return 0
        return int(connection.execute(f"SELECT count(*) FROM {table} WHERE {where}").fetchone()[0])


def parent_id_key(parent_id: str | None, is_root: bool) -> tuple[str, str] | None:
    return None if is_root or parent_id is None else ("folder", parent_id)


def _join_path(parent: str, child: str) -> str:
    if not parent:
        return child
    if not child:
        return parent
    return f"{parent}/{child}"
